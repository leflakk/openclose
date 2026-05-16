"""Unified browser_automation tool — single surface, two internal modes.

Operates in one of two internal modes selected at execute time from
the presence of the ``[browser_vision_grounding]`` section in
``~/.config/openclose/config.toml``:

- ``dom``: accessibility-tree only. Planner emits ``element_index``
  references; resolution is a free CDP lookup with JIT
  ``DOM.getBoxModel`` re-resolve. This is the default when no
  grounding endpoint is configured.
- ``rich``: accessibility-tree + screenshot. Planner may emit either
  ``element_index`` or a ``target`` description. Resolution chain:
  element_index → fuzzy a11y match → grounding LLM → fail. Most
  ``target`` strings match cleanly to a row in the a11y tree, so
  grounding is reserved for the genuine exception (canvas, custom
  widgets, opaque iframes). Activated automatically when
  ``[browser_vision_grounding]`` is present in the config.

Public schema preserved from the legacy tools: ``intent``, ``task``,
``url``, ``max_steps``. Return shape preserved: ``output`` plus
``metadata.subagent_steps`` and ``metadata.failure_reason``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from io import BytesIO
from typing import Any

from openclose.tool.tool import Tool, ToolResult, ToolParameter
from openclose.provider.provider import BaseProvider, Provider
from openclose.config.config import get_config
from openclose.log import get_logger

from openclose.tool.tools.browser_automation_shared import (
    BROWSER_AUTOMATION_LOCK,
    VIEWPORT_WIDTH, VIEWPORT_HEIGHT,
    TIME_LIMIT_S, POST_ACTION_WAIT_S,
    MAX_STEPS,
    EventContext, FailureReason,
    parse_model_response, execute_action,
    snapshot_a11y, summarise_recent_actions,
    resolve_element_index, element_resolution_failure_message,
    fuzzy_match_a11y,
    dump_page_content, format_tool_output,
    write_navigation_dump,
    validate_intent, run_goto_intent, run_web_search_intent,
    describe_outcome, wait_after_action,
    navigate_initial_url,
    handle_tab_switch, recover_page_if_dead,
    acquire_singleton_browser,
    _pick_or_create_page,
)

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_RECENT_ACTIONS_N = 5
_MAX_SNAPSHOT_ELEMENTS = 150
_SNAPSHOT_PAGE_TEXT_CHARS = 2000
_GROUNDING_MAX_ATTEMPTS = 3

# smart_resize configuration matching grounding model's MLM_PROCESSOR_IM_CFG
_IMAGE_FACTOR = 28
_MIN_PIXELS = 3136        # 4 * 28 * 28
_MAX_PIXELS = 12845056    # 16384 * 28 * 28
_MAX_RATIO = 200
_PATCH_SIZE = 14
_MERGE_SIZE = 2

_PIXEL_ACTIONS = ("left_click", "mouse_move", "type", "scroll")


# ---------------------------------------------------------------------------
# Image resize utilities (used in rich mode for grounding model).
# ---------------------------------------------------------------------------

def _round_by_factor(number: int, factor: int) -> int:
    return round(number / factor) * factor


def _ceil_by_factor(number: int, factor: int) -> int:
    return math.ceil(number / factor) * factor


def _floor_by_factor(number: int, factor: int) -> int:
    return math.floor(number / factor) * factor


def _smart_resize(
    height: int,
    width: int,
    factor: int = _IMAGE_FACTOR,
    min_pixels: int = _MIN_PIXELS,
    max_pixels: int = _MAX_PIXELS,
) -> tuple[int, int]:
    """Rescale so both dims are divisible by *factor* and total pixels in range."""
    if max(height, width) / min(height, width) > _MAX_RATIO:
        raise ValueError(
            f"absolute aspect ratio must be smaller than {_MAX_RATIO}, "
            f"got {max(height, width) / min(height, width)}"
        )
    h_bar = max(factor, _round_by_factor(height, factor))
    w_bar = max(factor, _round_by_factor(width, factor))
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = _floor_by_factor(int(height / beta), factor)
        w_bar = _floor_by_factor(int(width / beta), factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = _ceil_by_factor(int(height * beta), factor)
        w_bar = _ceil_by_factor(int(width * beta), factor)
    return h_bar, w_bar


def _scale_coords(
    coords: list[float],
    resized_w: int,
    resized_h: int,
    viewport_w: int = VIEWPORT_WIDTH,
    viewport_h: int = VIEWPORT_HEIGHT,
) -> list[float]:
    """Convert coordinates from resized-image space to viewport space."""
    return [coords[0] * viewport_w / resized_w, coords[1] * viewport_h / resized_h]


# ---------------------------------------------------------------------------
# Grounding model system prompt + tool schema (rich mode only).
# ---------------------------------------------------------------------------

def _build_tool_schema(resized_width: int, resized_height: int) -> str:
    """Build the JSON tool definition with dynamic resolution."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "browser_automation",
            "description": (
                "Use a mouse and keyboard to interact with a computer, and take screenshots.\n"
                "* This is an interface to a desktop GUI. You do not have access to a terminal or "
                "applications menu. You must click on desktop icons to start applications.\n"
                "* Some applications may take time to start or process actions, so you may need to "
                "wait and take successive screenshots to see the results of your actions. E.g. if "
                "you click on Firefox and a window doesn't open, try wait and taking another screenshot.\n"
                f"* The screen's resolution is {resized_width}x{resized_height}.\n"
                "* Whenever you intend to move the cursor to click on an element like an icon, you "
                "should consult a screenshot to determine the coordinates of the element before "
                "moving the cursor.\n"
                "* If you tried clicking on a program or link but it failed to load, even after "
                "waiting, try adjusting your cursor position so that the tip of the cursor visually "
                "falls on the element that you want to click.\n"
                "* Make sure to click any buttons, links, icons, etc with the cursor tip in the "
                "center of the element. Don't click boxes on their edges unless asked.\n"
                "* When a separate scrollable container prominently overlays the webpage, if you "
                "want to scroll within it, you typically need to mouse_move() over it first and "
                "then scroll().\n"
                "* If a popup window appears that you want to close, if left_click() on the 'X' or "
                "close button doesn't work, try key(keys=['Escape']) to close it.\n"
                "* On some search bars, when you type(), you may need to press_enter=False and "
                "instead separately call left_click() on the search button to submit the search "
                "query. This is especially true of search bars that have auto-suggest popups for "
                "e.g. locations\n"
                "* For calendar widgets, you usually need to left_click() on arrows to move between "
                "months and left_click() on dates to select them; type() is not typically used to "
                "input dates there."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "description": (
                            "The action to perform. The available actions are:\n"
                            "* `key`: Performs key down presses on the arguments passed in order, "
                            "then performs key releases in reverse order. Includes \"Enter\", \"Alt\", "
                            "\"Shift\", \"Tab\", \"Control\", \"Backspace\", \"Delete\", \"Escape\", "
                            "\"ArrowUp\", \"ArrowDown\", \"ArrowLeft\", \"ArrowRight\", \"PageDown\", "
                            "\"PageUp\", \"Shift\", etc.\n"
                            "* `type`: Type a string of text on the keyboard.\n"
                            "* `mouse_move`: Move the cursor to a specified (x, y) pixel coordinate "
                            "on the screen.\n"
                            "* `left_click`: Click the left mouse button.\n"
                            "* `scroll`: Performs a scroll of the mouse scroll wheel.\n"
                            "* `history_back`: Go back to the previous page in the browser history.\n"
                            "* `pause_and_memorize_fact`: Pause and memorize a fact for future "
                            "reference.\n"
                            "* `wait`: Wait specified seconds for the change to happen.\n"
                            "* `terminate`: Terminate the current task and report its completion status."
                        ),
                        "enum": [
                            "key", "type", "mouse_move", "left_click", "scroll",
                            "history_back",
                            "pause_and_memorize_fact", "wait", "terminate",
                        ],
                        "type": "string",
                    },
                    "keys": {
                        "description": "Required only by `action=key`.",
                        "type": "array",
                    },
                    "text": {
                        "description": "Required only by `action=type`.",
                        "type": "string",
                    },
                    "press_enter": {
                        "description": (
                            "Whether to press the Enter key after typing. "
                            "Required only by `action=type`."
                        ),
                        "type": "boolean",
                    },
                    "delete_existing_text": {
                        "description": (
                            "Whether to delete existing text before typing. "
                            "Required only by `action=type`."
                        ),
                        "type": "boolean",
                    },
                    "coordinate": {
                        "description": (
                            "(x, y): The x (pixels from the left edge) and y (pixels from the top "
                            "edge) coordinates to move the mouse to. Required only by "
                            "`action=left_click`, `action=mouse_move`, and `action=type`."
                        ),
                        "type": "array",
                    },
                    "pixels": {
                        "description": (
                            "The amount of scrolling to perform. Positive values scroll up, "
                            "negative values scroll down. Required only by `action=scroll`."
                        ),
                        "type": "number",
                    },
                    "fact": {
                        "description": (
                            "The fact to remember for the future. "
                            "Required only by `action=pause_and_memorize_fact`."
                        ),
                        "type": "string",
                    },
                    "time": {
                        "description": "The seconds to wait. Required only by `action=wait`.",
                        "type": "number",
                    },
                    "status": {
                        "description": (
                            "The status of the task. Required only by `action=terminate`."
                        ),
                        "type": "string",
                        "enum": ["success", "failure"],
                    },
                },
                "required": ["action"],
            },
        },
    }
    return json.dumps(tool_def, ensure_ascii=False)


def _build_grounding_system_prompt(resized_width: int, resized_height: int) -> str:
    """Generate the grounding system prompt matching FN_CALL_TEMPLATE."""
    tool_descs = _build_tool_schema(resized_width, resized_height)
    return (
        "You are a helpful assistant.\n\n"
        "You are a web automation agent that performs actions on websites to fulfill "
        "user requests by calling various tools.\n"
        "\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n"
        "<tools>\n"
        f"{tool_descs}\n"
        "</tools>\n"
        "\n"
        "For each function call, return a json object with function name and arguments "
        "within <tool_call></tool_call> XML tags:\n"
        "<tool_call>\n"
        '{"name": <function-name>, "arguments": <args-json-object>}\n'
        "</tool_call>"
    )


# ---------------------------------------------------------------------------
# Unified planner system prompt (mode-aware).
# ---------------------------------------------------------------------------

_PROMPT_HEAD_DOM = """\
You are the planning layer of a browser automation agent operating in \
text mode (no screenshots available).

Each turn you are given an accessibility snapshot listing all interactive \
elements on the current page, with sequential indices [0], [1], [2], etc., \
plus abbreviated page text. This snapshot is the ONLY authoritative view \
of the page — ignore any mental model from earlier turns; if it disagrees \
with the current snapshot, the snapshot is right.
"""

_PROMPT_HEAD_RICH = """\
You are the planning layer of a browser automation agent operating in \
rich mode (accessibility snapshot + screenshot).

Each turn you are given:
  1. An accessibility snapshot listing all interactive elements on the \
current page with sequential indices [0], [1], [2], etc. — your PRIMARY \
view; reference elements by ``element_index`` whenever possible.
  2. A fresh screenshot of the current viewport — your VISUAL fallback \
for elements that are visible but missing from the snapshot (canvas, \
custom widgets, opaque iframes); reference those by ``target`` description.

Both signals are authoritative. Ignore any mental model from earlier turns; \
if it disagrees with these inputs, these inputs are right.
"""

_PROMPT_BODY_COMMON = """\

Your job is to decide the NEXT SINGLE ACTION that NAVIGATES the browser \
to the page containing the requested information. You do NOT need to \
read, extract, or summarize page content — a separate system dumps the \
full page text automatically after you terminate. Focus strictly on \
getting to the right page.

# Action schema
Emit exactly ONE action per turn as a JSON object inside a <tool_call> \
block.

## Element-targeted actions
For these actions, you can EITHER reference an element by its accessibility \
``element_index`` OR (rich mode only) describe it as a ``target`` string. \
Strongly prefer ``element_index`` whenever the element appears in the \
snapshot — it is free and exact. Use ``target`` only when the element is \
visible on screen but missing from the snapshot list.

- ``{"action": "left_click", "element_index": N}`` OR \
``{"action": "left_click", "target": "<element description>"}``  \
(for buttons, links, tabs)
- ``{"action": "mouse_move", "element_index": N}`` OR \
``{"action": "mouse_move", "target": "<element description>"}``
- ``{"action": "type", "element_index": N, "text": "...", \
"press_enter": true|false, "delete_existing_text": true|false}`` OR \
``{"action": "type", "target": "<input description>", "text": "...", \
"press_enter": true|false, "delete_existing_text": true|false}``  \
STRONGLY PREFER this atomic form for typing into any input / textarea / \
contenteditable. It clicks, focuses, and types in one step. Do NOT \
decompose it into a separate ``left_click`` followed by a focusless \
``type`` — focus is not reliably preserved across turns, especially on \
modern React / shadow-DOM inputs.
- ``{"action": "scroll", "element_index": N, "pixels": <int>}`` OR \
``{"action": "scroll", "target": "<container description>", "pixels": \
<int, positive=up, negative=down>}``  (for whole-page scrolling, omit \
both ``element_index`` and ``target``)

## Direct actions (no element resolution, executed as-is)
- ``{"action": "history_back"}``
- ``{"action": "scroll", "pixels": <int>}``  (whole page)
- ``{"action": "type", "text": "...", "press_enter": true|false}``  \
(focusless type — types into whatever element currently has focus. \
LAST-RESORT fallback. For normal typing, always use ``type`` with \
``element_index`` or ``target`` above.)
- ``{"action": "key", "keys": ["Enter"|"Escape"|"Tab"|"ArrowDown"|...]}``
- ``{"action": "wait", "time": <seconds, max 10>}``
- ``{"action": "pause_and_memorize_fact", "fact": "<fact to remember>"}``
- ``{"action": "terminate", "status": "success"|"failure", "summary": \
"<short description of what page you reached and why you believe it \
contains the requested information>"}``
"""

_PROMPT_TARGET_GUIDE_DOM = """\

# Choosing element indices
Read the [N] indices from the accessibility snapshot carefully. Each index \
maps to exactly one interactive element on the page.
  - If the element you need is not in the snapshot, try scrolling the \
page first (``{"action": "scroll", "pixels": -500}``) to reveal more \
elements, then look at the refreshed snapshot on the next turn.
  - If you need to interact with a dynamically loaded element, try \
``wait`` first then check the refreshed snapshot.
  - If the element you need is not in the interactive list even after \
scrolling, terminate with status:"failure".
"""

_PROMPT_TARGET_GUIDE_RICH = """\

# Choosing element_index vs target
Prefer ``element_index`` whenever the element appears in the snapshot \
(``[N]`` shown next to it). It's resolved free, with no LLM cost.

Use ``target`` (description) only when the element is clearly visible on \
the screenshot but missing from the snapshot — typical for canvas-rendered \
UIs, custom widgets, opaque iframes. The system first tries to fuzzy-match \
your ``target`` against the snapshot; only if that fails does it call a \
separate visual grounding model.

# Target descriptions must be unambiguous
The grounding model only sees the screenshot + your ``target`` string. \
Write descriptions a stranger could follow:
  - GOOD: "the blue 'Sign in' button in the top-right header"
  - GOOD: "the search input field at the top of the page with placeholder 'Search Wikipedia'"
  - GOOD: "the third result card in the search results list"
  - BAD: "the button", "the input", "it", "there"
Include at least one of: color, label text, position (top/bottom/left/right), \
or role (button/input/link/menu item).

# When grounding fails (target not located)
If the resolver reports it could not locate your ``target``, try these \
strategies in order:
  1. REPHRASE the target more precisely — include color, position, \
and exact label text.
  2. SCROLL to reveal the element if it may be below the fold.
  3. Use Tab cycling: ``{"action": "key", "keys": ["Tab"]}`` to move \
focus through the page, then use focusless type: \
``{"action": "type", "text": "...", "press_enter": false}`` to type \
into whatever element received focus. WARNING: Tab MOVES focus — \
NEVER use it after a successful action.
  4. Click a NEARBY visible element (label, heading, sibling) first, \
then Tab into the target field.
  5. Try ``history_back`` to recover, or terminate.
  6. If nothing works after 2 total attempts, ``terminate`` with \
``status:"failure"``.

# Typing tasks
When the task asks you to type/enter/fill text into an input or \
textarea, your FIRST action should almost always be ``type`` with \
``element_index`` (preferred) or ``target`` pointing at that input — \
not ``left_click`` first and ``type`` later. The atomic form clicks, \
focuses, and types in one executor step. Separating click and \
type across turns commonly breaks on modern inputs because focus \
is lost between planner turns.
"""

_PROMPT_TAIL = """\

# Layer awareness
Elements are grouped by visual layer in the snapshot. The ACTIVE LAYER \
(topmost overlay/modal) is shown first — if present, its action buttons \
(e.g. "Add", "Submit", "Confirm") are almost always what you need to \
interact with next. BACKGROUND elements may be blocked by the overlay \
and unclickable until the overlay is dismissed.

# Rules
- Always study the current page state before acting. Check that the \
previous action had the expected effect (e.g. new page loaded, new elements \
appeared). If not, diagnose and adapt — do NOT blindly repeat.
- Check "Outcome since last snapshot" before acting. If it says "no \
changes detected", your last action silently failed — do NOT repeat it. \
Check for an overlay, try a different element, scroll to reveal the \
element, or ``terminate`` with ``status:"failure"``.
- Always scan the "Recent actions" list before deciding your next move. \
It is your ONLY memory of previous turns — use it to detect loops. If you \
see the same action repeating, OR a short cycle of 2–3 actions rotating \
without the snapshot meaningfully changing, you are stuck in a \
non-working pattern. Do NOT continue the pattern: change strategy entirely \
(different element/target, scroll, wait, ``history_back``, or \
``terminate`` with ``status:"failure"``).
- HARD LIMIT: Do not repeat the SAME action on the same target/element \
more than 2 times. After 2 failed attempts, you MUST either try a \
completely different approach or terminate with status:"failure". \
Continuing to repeat wastes the step budget.
- Use ``pause_and_memorize_fact`` ONLY for navigation-relevant notes. \
Do NOT use it to extract or transcribe page content — content extraction \
happens automatically after you terminate.
- You CANNOT load new pages or run web searches yourself — those are \
the main agent's responsibility, exposed as the ``visit_url`` and \
``web_search`` tool intents. If the requested information is not \
reachable from the current page, ``terminate`` with \
``status:"failure"`` and explain what URL or search would help.
- When you have navigated to the page that contains the requested \
information, emit ``terminate`` with ``status:"success"``. You do NOT \
need to read or summarize the page content — just confirm you arrived \
at the right page. If you are stuck and cannot reach the target page, \
emit ``terminate`` with ``status:"failure"`` and explain why.
- Output format — free-form thinking first, then ONE <tool_call> block:

<reason about what you see, what you already tried, and what to do next>
<tool_call>
{"name": "browser_automation", "arguments": {"action": "...", ...}}
</tool_call>
"""


def _build_planner_system_prompt(mode: str) -> str:
    """Build the unified planner system prompt for *mode* (``dom`` or ``rich``)."""
    if mode == "rich":
        return (
            _PROMPT_HEAD_RICH
            + _PROMPT_BODY_COMMON
            + _PROMPT_TARGET_GUIDE_RICH
            + _PROMPT_TAIL
        )
    return (
        _PROMPT_HEAD_DOM
        + _PROMPT_BODY_COMMON
        + _PROMPT_TARGET_GUIDE_DOM
        + _PROMPT_TAIL
    )


# Precomputed for tests / external inspection.
UNIFIED_PLANNER_SYSTEM_PROMPT_DOM = _build_planner_system_prompt("dom")
UNIFIED_PLANNER_SYSTEM_PROMPT_RICH = _build_planner_system_prompt("rich")


# ---------------------------------------------------------------------------
# User-turn builder (mode-aware).
# ---------------------------------------------------------------------------

def _build_planner_user_turn(
    step: int,
    task: str,
    memory: list[str],
    steps_log: list[dict[str, Any]],
    current_url: str,
    snapshot_text: str,
    img_b64: str | None,
    mode: str,
    initial_url: str = "",
    action_outcome: str = "",
) -> dict[str, Any]:
    """Build the user-role message for one planner turn.

    DOM mode: text-only payload (a11y snapshot is the authoritative view).
    Rich mode: text payload (same a11y snapshot + URL note) followed by
    an image_url block carrying the screenshot.
    """
    trimmed_url = current_url[:100]
    if len(current_url) > 100:
        trimmed_url = trimmed_url + " …"

    lines: list[str] = []
    if step == 0:
        lines.append(f"Task: {task}")
    else:
        lines.append(f"Continue the task: {task}")
    if step == 0 and initial_url:
        lines.append(
            f"NOTE: The browser was pre-navigated to {initial_url} before "
            f"your first turn. The snapshot below shows the already-loaded "
            f"page. If the task only required reaching this URL, terminate "
            f"with status:success immediately."
        )
    if memory:
        lines.append("")
        lines.append("Remembered facts:")
        lines.extend(f"- {f}" for f in memory)
    lines.append("")
    lines.append(
        "Recent actions (text history — this is the only memory of previous "
        "turns):"
    )
    lines.append(summarise_recent_actions(steps_log, n=_RECENT_ACTIONS_N))
    if action_outcome:
        lines.append("")
        lines.append("Outcome since last snapshot:")
        lines.append(action_outcome)
    lines.append("")
    lines.append(f"Current URL: {trimmed_url}")
    if mode == "rich":
        lines.append(
            "CURRENT page state below — accessibility snapshot first, then "
            "screenshot. Decide the next single action based on these."
        )
    else:
        lines.append(
            "CURRENT page state below (this is the single authoritative "
            "view — decide the next single action based ONLY on this "
            "snapshot):"
        )
    lines.append("")
    lines.append(snapshot_text)

    text_block = "\n".join(lines)
    if mode == "rich" and img_b64:
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": text_block},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                },
            ],
        }
    return {"role": "user", "content": text_block}


# ---------------------------------------------------------------------------
# Screenshot capture (rich mode only).
# ---------------------------------------------------------------------------

async def _capture_screenshot(page: Any) -> tuple[int, int, str]:
    """Screenshot the page, smart-resize for grounding model, return base64 PNG.

    Returns ``(resized_width, resized_height, img_b64)``.
    """
    from PIL import Image as PILImage

    screenshot_bytes = await page.screenshot()
    pil_img = PILImage.open(BytesIO(screenshot_bytes))
    resized_h, resized_w = _smart_resize(
        pil_img.height, pil_img.width,
        factor=_PATCH_SIZE * _MERGE_SIZE,
        min_pixels=_MIN_PIXELS,
        max_pixels=_MAX_PIXELS,
    )
    resized_img = pil_img.resize((resized_w, resized_h))
    buf = BytesIO()
    resized_img.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode()
    return resized_w, resized_h, img_b64


# ---------------------------------------------------------------------------
# Signal acquisition.
# ---------------------------------------------------------------------------

async def _acquire_signals(
    page: Any,
    context: Any,
    mode: str,
) -> tuple[str, dict[int, dict[str, Any]], str | None, int | None, int | None]:
    """Acquire the inputs the planner sees this turn.

    DOM: returns ``(snapshot_text, element_map, None, None, None)``.
    Rich: returns ``(snapshot_text, element_map, img_b64, resized_w, resized_h)``;
    snapshot and screenshot run concurrently. Either may fail
    independently — partial results are returned with ``None`` placeholders.
    """
    if mode == "dom":
        snapshot_text, element_map = await snapshot_a11y(
            page, context,
            max_elements=_MAX_SNAPSHOT_ELEMENTS,
            page_text_chars=_SNAPSHOT_PAGE_TEXT_CHARS,
        )
        return snapshot_text, element_map, None, None, None

    snapshot_task = asyncio.create_task(
        snapshot_a11y(
            page, context,
            max_elements=_MAX_SNAPSHOT_ELEMENTS,
            page_text_chars=_SNAPSHOT_PAGE_TEXT_CHARS,
        )
    )
    shot_task = asyncio.create_task(_capture_screenshot(page))

    snapshot_result, shot_result = await asyncio.gather(
        snapshot_task, shot_task, return_exceptions=True,
    )

    if isinstance(snapshot_result, BaseException):
        log.warning("A11y snapshot failed in rich mode: %s", snapshot_result)
        snapshot_text = (
            "Page URL: (unavailable)\nPage title: (unavailable)\n\n"
            "Interactive elements: (snapshot failed — rely on screenshot)"
        )
        element_map = {}
    else:
        snapshot_text, element_map = snapshot_result

    if isinstance(shot_result, BaseException):
        log.warning("Screenshot capture failed in rich mode: %s", shot_result)
        return snapshot_text, element_map, None, None, None

    resized_w, resized_h, img_b64 = shot_result
    return snapshot_text, element_map, img_b64, resized_w, resized_h


# ---------------------------------------------------------------------------
# Visual grounding (rich mode only, fallback for fuzzy a11y misses).
# ---------------------------------------------------------------------------

def _grounding_instruction_text(target: str) -> str:
    """Build the narrow user instruction sent to the grounding model."""
    return (
        f"Planner decided to perform: left_click\n"
        f"Target element: {target}\n"
        f"Other arguments: {{}}\n\n"
        f"Respond ONLY with a <tool_call> block containing "
        f'"left_click" and the coordinates of the target element in '
        f"the screenshot below. Do not plan, do not terminate, do not "
        f"ask questions — just return one <tool_call> with the "
        f"coordinates."
    )


async def _visual_ground(
    grounding_provider: BaseProvider,
    action_args: dict[str, Any],
    resized_w: int,
    resized_h: int,
    img_b64: str,
    project_dir: str,
    step: int,
) -> tuple[dict[str, Any] | None, str]:
    """Ask the grounding model to turn a ``target`` description into coordinates.

    Makes up to ``_GROUNDING_MAX_ATTEMPTS`` fresh (history-less) calls.
    Returns ``(result_dict_or_none, final_status_text)``.
    """
    original_action = action_args.get("action", "")
    target = action_args.get("target", "")
    system_prompt = _build_grounding_system_prompt(resized_w, resized_h)
    base_user_text = _grounding_instruction_text(target)

    def _nudge_for(reason: str) -> str:
        return (
            f"Your previous response was invalid: {reason}. "
            f'Return ONLY a single <tool_call> block for action '
            f'"left_click" with the coordinates of the target '
            f"element in the screenshot. Do not plan. Do not explain."
        )

    attempt_failures: list[str] = []
    last_error = "no attempts made"

    for attempt in range(1, _GROUNDING_MAX_ATTEMPTS + 1):
        user_text = base_user_text
        if attempt > 1:
            user_text = f"{base_user_text}\n\n{_nudge_for(last_error)}"

        grounding_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    },
                ],
            },
        ]

        from openclose.debug import LLMDebugContext, llm_debug_context
        llm_debug_context.set(LLMDebugContext(
            source="browser_automation.grounding",
            step=step,
            project_dir=project_dir,
        ))

        try:
            cfg = get_config()
            # Invariant: _visual_ground is rich-mode only; grounding cfg is set.
            assert cfg.browser_vision_grounding is not None
            response = await grounding_provider.chat_sync(
                messages=grounding_messages,  # type: ignore[arg-type]
                model=cfg.browser_vision_grounding.model,
                temperature=cfg.temperatures.browser_vision_grounding,
            )
            raw = response.choices[0].message.content or ""
        except Exception as e:
            log.warning("Grounding call failed on attempt %d: %s", attempt, e)
            last_error = f"inference error: {e}"
            attempt_failures.append(f"attempt {attempt}: {last_error}")
            continue

        _thinking, returned = parse_model_response(raw)

        if returned is None:
            last_error = "no parseable <tool_call> block in response"
            attempt_failures.append(f"attempt {attempt}: {last_error}")
            continue

        returned_action = returned.get("action", "")
        if returned_action not in _PIXEL_ACTIONS:
            last_error = (
                f"returned action {returned_action!r} is not a "
                f"pixel-locating action"
            )
            attempt_failures.append(f"attempt {attempt}: {last_error}")
            continue

        coord = returned.get("coordinate")
        if (
            not isinstance(coord, list)
            or len(coord) < 2
            or not all(isinstance(c, (int, float)) for c in coord[:2])
        ):
            last_error = f"missing or invalid coordinate {coord!r}"
            attempt_failures.append(f"attempt {attempt}: {last_error}")
            continue

        x, y = coord[0], coord[1]
        if not (0 <= x <= resized_w and 0 <= y <= resized_h):
            last_error = (
                f"coordinate ({x:.1f}, {y:.1f}) outside resized "
                f"image bounds ({resized_w}x{resized_h})"
            )
            attempt_failures.append(f"attempt {attempt}: {last_error}")
            continue

        result = {
            k: v for k, v in action_args.items() if k != "target"
        }
        result["coordinate"] = [x, y]
        result["__from_grounding"] = True
        suffix = (
            f" (attempt {attempt}/{_GROUNDING_MAX_ATTEMPTS})"
            if attempt > 1 else ""
        )
        final_text = (
            f"grounded {original_action} at ({x:.0f}, {y:.0f}){suffix}"
        )
        if attempt > 1:
            log.info(
                "Grounding succeeded on attempt %d after earlier failures: %s",
                attempt, "; ".join(attempt_failures),
            )
        return result, final_text

    failures_joined = (
        "; ".join(attempt_failures) if attempt_failures else last_error
    )
    final_text = (
        f"grounding failed after {_GROUNDING_MAX_ATTEMPTS} attempts: "
        f"{failures_joined}"
    )
    log.warning(
        "Grounding exhausted %d attempts for target %r: %s",
        _GROUNDING_MAX_ATTEMPTS,
        action_args.get("target", ""),
        failures_joined,
    )
    return None, final_text


def _grounding_failure_message(action_args: dict[str, Any]) -> str:
    """Observation appended to planner's history when grounding fails."""
    target = action_args.get("target", "")
    action = action_args.get("action", "")
    return (
        f"Grounding failed after {_GROUNDING_MAX_ATTEMPTS} attempts for "
        f'target "{target}" (action "{action}"). '
        f"The visual model could not locate this element. "
        f"Try these strategies:\n"
        f"1. REPHRASE the target more precisely (include color, position, "
        f"exact label text).\n"
        f"2. SCROLL to reveal the element if it may be below the fold.\n"
        f"3. Use Tab cycling: {{\"action\": \"key\", \"keys\": [\"Tab\"]}} "
        f"to move focus, then use focusless type: "
        f"{{\"action\": \"type\", \"text\": \"...\", \"press_enter\": false}} "
        f"to type into whatever received focus.\n"
        f"4. Click a NEARBY visible element (label, heading, sibling) first, "
        f"then Tab into the target.\n"
        f"5. Try history_back to recover, or terminate.\n"
        f"6. If nothing works after 2 attempts, terminate with "
        f'status:"failure".'
    )


# ---------------------------------------------------------------------------
# Unified resolver chain (element_index → fuzzy a11y → grounding → fail).
# ---------------------------------------------------------------------------

async def resolve_action_target(
    action_args: dict[str, Any],
    element_map: dict[int, dict[str, Any]],
    page: Any,
    context: Any,
    mode: str,
    grounding_provider: BaseProvider | None,
    img_b64: str | None,
    resized_w: int | None,
    resized_h: int | None,
    project_dir: str,
    step: int,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve a pixel action to viewport coordinates.

    Tries (1) element_index lookup, (2) fuzzy a11y match against
    ``target``, (3) grounding LLM (rich mode only). First success
    wins. Returns ``(result_dict_or_none, status_text)``.
    """
    idx = action_args.get("element_index")
    target = action_args.get("target", "") or ""

    if idx is not None:
        return await resolve_element_index(
            action_args, element_map, page=page, context=context,
        )

    if target:
        match_idx, match_status = fuzzy_match_a11y(target, element_map)
        if match_idx is not None:
            args_for_resolve = {
                k: v for k, v in action_args.items() if k != "target"
            }
            args_for_resolve["element_index"] = match_idx
            grounded, _ = await resolve_element_index(
                args_for_resolve, element_map, page=page, context=context,
            )
            if grounded is not None:
                return grounded, match_status
            # JIT re-resolve failed for the matched index — fall through.
        if (
            mode == "rich"
            and grounding_provider is not None
            and img_b64 is not None
            and resized_w is not None
            and resized_h is not None
        ):
            return await _visual_ground(
                grounding_provider, action_args,
                resized_w, resized_h, img_b64,
                project_dir=project_dir, step=step,
            )
        return None, (
            f"target {target!r} not found in accessibility tree "
            f"(mode={mode}, no grounding fallback available)"
        )

    return None, "no element_index or target supplied for pixel action"


# ---------------------------------------------------------------------------
# Per-step planner loop.
# ---------------------------------------------------------------------------

async def _run_navigate_loop(
    *,
    mode: str,
    page: Any,
    context: Any,
    ctx: EventContext,
    planner_provider: BaseProvider,
    planner_model: str,
    grounding_provider: BaseProvider | None,
    task: str,
    initial_url: str,
    max_steps: int,
    project_dir: str,
) -> tuple[Any, str, str, FailureReason | None]:
    """Run the navigate planner loop.

    Returns ``(page, final_status, last_thinking, failure_reason)``.
    Caller is responsible for the post-loop dump and ToolResult build.
    """
    memory: list[str] = []
    recent_action_sigs: list[tuple[str, Any]] = []
    last_thinking = ""
    final_status = "Max steps reached without termination."
    failure_reason: FailureReason | None = None
    deadline = time.monotonic() + TIME_LIMIT_S
    prev_snapshot_text = ""
    prev_url = ""

    effective_max_steps = min(int(max_steps), 15)

    for step in range(effective_max_steps):
        if time.monotonic() >= deadline:
            final_status = f"Time limit reached ({TIME_LIMIT_S}s)."
            failure_reason = FailureReason.STEP_BUDGET_EXHAUSTED
            break

        # 1. Acquire signals (a11y always; screenshot in rich mode).
        try:
            (
                snapshot_text,
                element_map,
                img_b64,
                resized_w,
                resized_h,
            ) = await _acquire_signals(page, context, mode)
        except Exception as e:
            log.error("Signal acquisition failed at step %d: %s", step, e)
            final_status = f"Signal acquisition failed at step {step}: {e}"
            failure_reason = FailureReason.STEP_BUDGET_EXHAUSTED
            break

        # 2. Build planner user turn.
        try:
            current_url = page.url
        except Exception:
            current_url = ""

        action_outcome = describe_outcome(
            prev_snapshot_text, snapshot_text, prev_url, current_url,
        )
        prev_snapshot_text = snapshot_text
        prev_url = current_url

        user_turn = _build_planner_user_turn(
            step=step,
            task=task,
            memory=memory,
            steps_log=ctx.steps_log,
            current_url=current_url,
            snapshot_text=snapshot_text,
            img_b64=img_b64,
            mode=mode,
            initial_url=initial_url,
            action_outcome=action_outcome,
        )

        # 3. Call planner — stateless, no history.
        api_messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": _build_planner_system_prompt(mode),
            },
            user_turn,
        ]
        from openclose.debug import LLMDebugContext, llm_debug_context
        llm_debug_context.set(LLMDebugContext(
            source=f"browser_automation.planner.{mode}",
            step=step + 1,
            project_dir=project_dir,
        ))

        cfg = get_config()
        temp = (
            cfg.temperatures.browser_vision_planner
            if mode == "rich"
            else cfg.temperatures.browser_dom_planner
        )
        try:
            planner_resp = await planner_provider.chat_sync(
                messages=api_messages,  # type: ignore[arg-type]
                model=planner_model,
                temperature=temp,
            )
            raw_output = planner_resp.choices[0].message.content or ""
        except Exception as e:
            log.error("Planner call failed at step %d: %s", step, e)
            final_status = f"Planner call failed at step {step}: {e}"
            failure_reason = FailureReason.STEP_BUDGET_EXHAUSTED
            break

        # 4. Parse response.
        thinking, action_args = parse_model_response(raw_output)
        if thinking:
            last_thinking = thinking
            await ctx.emit_text(thinking, "Planner")

        if action_args is None:
            ctx.steps_log.append({
                "type": "text",
                "content": f"Step {step + 1}: unparseable planner output",
                "subagent_label": "Planner",
            })
            continue

        action = action_args.get("action", "")
        tc = ctx.new_tc(action, action_args)
        await ctx.emit_call(tc, "Planner")
        ctx.log_call(tc, "Planner")

        # 4b. Loop detection — unified signature: element_index OR target.
        if action not in ("terminate", "pause_and_memorize_fact", "scroll"):
            sig_target = (
                action_args.get("element_index")
                if action_args.get("element_index") is not None
                else action_args.get("target", "")
            )
            action_sig = (action, sig_target)
            recent_action_sigs.append(action_sig)
            if (
                len(recent_action_sigs) >= 3
                and recent_action_sigs[-1]
                == recent_action_sigs[-2]
                == recent_action_sigs[-3]
            ):
                final_status = (
                    f"Loop detected: '{action}' repeated 3 times "
                    f"on same target. Terminating."
                )
                failure_reason = FailureReason.NAVIGATION_LOOP_DETECTED
                await ctx.emit_result(tc, final_status, "Planner")
                ctx.log_result(tc, final_status, "Planner")
                await ctx.emit_grounding_skip(
                    action, action_args, "terminated by loop detection",
                )
                break

        # 5. Handle terminate / memorize.
        if action == "terminate":
            status = action_args.get("status", "success")
            summary_text = action_args.get("summary", "")
            final_status = f"Task terminated with status: {status}"
            if status != "success":
                failure_reason = FailureReason.TASK_INFEASIBLE
            if summary_text:
                last_thinking = (
                    f"{summary_text}\n\n{last_thinking}".strip()
                    if last_thinking
                    else str(summary_text)
                )
            await ctx.emit_result(tc, final_status, "Planner")
            ctx.log_result(tc, final_status, "Planner")
            await ctx.emit_grounding_skip(
                action, action_args, "task terminated by planner",
            )
            break

        if action == "pause_and_memorize_fact":
            fact = action_args.get("fact", "")
            memory.append(fact)
            result_text = f"Memorized: {fact}"
            await ctx.emit_result(tc, result_text, "Planner")
            ctx.log_result(tc, result_text, "Planner")
            await ctx.emit_grounding_skip(
                action, action_args, "fact memorized, no browser action",
            )
            continue

        # 6. Resolve target if pixel action.
        needs_resolution = action in _PIXEL_ACTIONS and (
            action_args.get("element_index") is not None
            or bool(action_args.get("target"))
        )

        if needs_resolution:
            ground_args = {"action": action}
            if action_args.get("element_index") is not None:
                ground_args["element_index"] = action_args.get("element_index")
            if action_args.get("target"):
                ground_args["target"] = action_args.get("target")
            ground_tc = ctx.new_tc(action, ground_args)
            await ctx.emit_call(ground_tc, "Grounding")
            ctx.log_call(ground_tc, "Grounding")

            grounded, ground_status = await resolve_action_target(
                action_args, element_map, page, context, mode,
                grounding_provider, img_b64, resized_w, resized_h,
                project_dir, step + 1,
            )

            if grounded is not None:
                ground_tc._arguments = json.dumps(
                    {k: v for k, v in grounded.items() if k != "__from_grounding"},
                    ensure_ascii=False,
                )

            await ctx.emit_result(ground_tc, ground_status, "Grounding")
            ctx.log_result(ground_tc, ground_status, "Grounding")

            if grounded is None:
                # Decide failure_reason from status text shape.
                if "not in current snapshot" in ground_status:
                    failure_reason = FailureReason.ELEMENT_NOT_IN_TREE
                elif "ambiguous" in ground_status:
                    failure_reason = FailureReason.ELEMENT_AMBIGUOUS
                elif "grounding failed" in ground_status:
                    failure_reason = FailureReason.ELEMENT_NOT_IN_TREE
                elif "not found in accessibility tree" in ground_status:
                    failure_reason = FailureReason.ELEMENT_NOT_IN_TREE
                else:
                    failure_reason = FailureReason.ELEMENT_AMBIGUOUS

                if "grounding failed" in ground_status:
                    observation = _grounding_failure_message(action_args)
                else:
                    observation = element_resolution_failure_message(
                        action_args, ground_status,
                    )
                await ctx.emit_result(tc, observation, "Planner")
                ctx.log_result(tc, observation, "Planner")
                continue

            # Grounding-sourced coords need viewport scaling.
            if grounded.get("__from_grounding") and (
                resized_w is not None and resized_h is not None
            ):
                coord = grounded.get("coordinate")
                if (
                    isinstance(coord, list)
                    and len(coord) >= 2
                ):
                    grounded["coordinate"] = _scale_coords(
                        coord, resized_w, resized_h,
                        VIEWPORT_WIDTH, VIEWPORT_HEIGHT,
                    )
            grounded.pop("__from_grounding", None)
            action_args = grounded
            failure_reason = None
        else:
            await ctx.emit_grounding_skip(
                action, action_args,
                f"'{action}' is a direct action",
            )

        # 7. Execute via Playwright.
        action_error: Exception | None = None
        try:
            result_text = await execute_action(page, action_args)
        except Exception as e:
            result_text = f"Action error: {e}"
            action_error = e
            log.warning(
                "Browser action failed at step %d: %s", step, e,
            )

        await ctx.emit_result(tc, result_text, "Planner")
        ctx.log_result(tc, result_text, "Planner")

        # 8. Tab switch + dead-page recovery.
        page = await handle_tab_switch(context, page)
        if action_error is not None:
            page = await recover_page_if_dead(context, page)

        # 9. Settle.
        await wait_after_action(page, action)

    else:
        if failure_reason is None:
            failure_reason = FailureReason.STEP_BUDGET_EXHAUSTED

    return page, final_status, last_thinking, failure_reason


# ---------------------------------------------------------------------------
# Tool factory.
# ---------------------------------------------------------------------------

def make_browser_automation_tool(project_dir: str = ".") -> Tool:
    """Create the unified browser_automation tool."""

    async def execute(
        intent: str = "",
        task: str = "",
        url: str = "",
        query: str = "",
        max_steps: int = MAX_STEPS,
        **kwargs: object,
    ) -> ToolResult:
        err = validate_intent(intent, task, url, query)
        if err:
            return ToolResult(error=err)

        if BROWSER_AUTOMATION_LOCK.locked():
            return ToolResult(
                error="A browser automation call is already running. "
                "Only one call is allowed at a time — wait for the "
                "current call to finish before issuing another."
            )

        async with BROWSER_AUTOMATION_LOCK:
            from openclose.agent.loop import (
                subagent_event_sink,
                current_tool_call_id,
            )

            sink = subagent_event_sink.get(None)
            parent_tc_id = current_tool_call_id.get("")
            ctx = EventContext(sink=sink, parent_tc_id=parent_tc_id)

            # Mode selection: rich when a grounding endpoint is configured
            # in ~/.config/openclose/config.toml, else dom-only. Read once
            # at execute entry, fixed for the call.
            mode = "rich" if get_config().browser_vision_grounding is not None else "dom"

            # Dependency checks.
            try:
                from playwright.async_api import async_playwright  # noqa: F401
            except ImportError:
                return ToolResult(
                    error="playwright is not installed. "
                    "Run: pip install playwright && playwright install"
                )

            if mode == "rich":
                try:
                    from PIL import Image as PILImage  # noqa: F401
                except ImportError:
                    return ToolResult(
                        error="Pillow is required for vision mode. "
                        "Run: pip install Pillow"
                    )

            try:
                _pw, _browser, browser_context = await acquire_singleton_browser()
                page = await _pick_or_create_page(browser_context)
                try:
                    await page.set_viewport_size(
                        {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
                    )
                except Exception as e:
                    log.debug("set_viewport_size failed: %s", e)
            except Exception as e:
                # Singleton may be wedged; reset and bail. Next call retries.
                from openclose.tool.tools.browser_automation_shared import (
                    reset_singleton_browser,
                )
                await reset_singleton_browser()
                return ToolResult(error=f"Failed to start browser: {e}")

            try:
                if intent == "visit_url":
                    page, result = await run_goto_intent(
                        browser_context, page, url,
                        ctx=ctx,
                        project_dir=project_dir,
                    )
                    return result

                if intent == "web_search":
                    page, result = await run_web_search_intent(
                        browser_context, page, query,
                        ctx=ctx,
                        project_dir=project_dir,
                    )
                    return result

                # act_on_page intent.
                from openclose.provider.provider import get_provider
                planner_provider = get_provider()
                planner_model = (await planner_provider.detect_model()) or ""
                if not planner_model:
                    return ToolResult(
                        error="Could not auto-detect a model on the "
                        "planner provider."
                    )

                grounding_provider: Provider | None = None
                if mode == "rich":
                    # Invariant: mode is "rich" only when this section is set.
                    grounding_cfg = get_config().browser_vision_grounding
                    assert grounding_cfg is not None
                    grounding_provider = Provider(
                        base_url=grounding_cfg.base_url,
                        api_key=grounding_cfg.api_key or "no-key",
                        provider_name="grounding",
                    )

                if url:
                    page = await navigate_initial_url(browser_context, page, url)

                page, final_status, last_thinking, failure_reason = (
                    await _run_navigate_loop(
                        mode=mode,
                        page=page,
                        context=browser_context,
                        ctx=ctx,
                        planner_provider=planner_provider,
                        planner_model=planner_model,
                        grounding_provider=grounding_provider,
                        task=task,
                        initial_url=url,
                        max_steps=max_steps,
                        project_dir=project_dir,
                    )
                )

                if page:
                    try:
                        await page.wait_for_load_state("load", timeout=10000)
                    except Exception:
                        pass
                    await asyncio.sleep(POST_ACTION_WAIT_S)

                page_content: dict[str, Any] = {}
                if page:
                    try:
                        page_content = await dump_page_content(page)
                    except Exception as e:
                        log.warning("Page content dump failed: %s", e)

                is_failure = (
                    failure_reason is not None
                    or "status: success" not in final_status
                )
                # Content sections are emitted only on the failure
                # branch (short_mode=not is_failure → False when
                # failing). Persist the page text there too so the
                # agent can recover it via Grep/Read.
                dump_path = (
                    write_navigation_dump(project_dir, page_content)
                    if is_failure and page_content else None
                )
                return ToolResult(
                    output=format_tool_output(
                        final_status,
                        last_thinking,
                        ctx.steps_log,
                        page_content,
                        failure_reason=failure_reason,
                        short_mode=not is_failure,
                        dump_path=dump_path,
                    ),
                    metadata={
                        "subagent_steps": ctx.steps_log,
                        "failure_reason": (
                            failure_reason.value if failure_reason else None
                        ),
                    },
                )

            except Exception as e:
                log.error(
                    "Browser automation error: %s", e, exc_info=True,
                )
                return ToolResult(
                    error=f"Browser automation failed: {e}",
                )

            # Note: do NOT pw.stop() — singleton browser stays warm.

    return Tool(
        name="browser_automation",
        description=(
            "USE IT TO OPEN A URL, RUN A WEB SEARCH, OR DRIVE CHROME VIA CDP. "
            "`visit_url` opens a URL and returns navigation data; "
            "`web_search` runs a Bing search for `query` and returns the "
            "results page in the same shape. In both cases the visible "
            "page text is always saved to a markdown file — to read it, "
            "use `grep` and `read` on the path printed after "
            "`Page content saved at:`. "
            "`act_on_page` hands a `task` to a planner sub-agent that "
            "interacts with the already-loaded page (clicks, forms, "
            "dropdowns) via the accessibility tree. "
            "Always split tasks into smaller objectives to get better results. "
            "CANNOT BE CALLED IN PARALLEL."
        ),
        parameters=[
            ToolParameter(
                name="intent",
                description=(
                    "What this call should do. "
                    "`visit_url`: load `url`, dump the page, and return "
                    "the URL, title, interactive elements, links, and "
                    "iframes; the full page text is saved to a markdown "
                    "file (path printed in the output) — recover specific "
                    "text from it with `grep` and `read`. "
                    "`web_search`: run a Bing search for `query` and "
                    "return the same shape as `visit_url` for the "
                    "results page. "
                    "`act_on_page`: hand `task` to a planner sub-agent "
                    "that interacts with the page (clicks, forms, "
                    "dropdowns) until the goal is reached; on success "
                    "returns a short confirmation, on failure returns "
                    "the same content as `visit_url` plus a "
                    "`failure_reason:` line for diagnosis."
                ),
                enum=["visit_url", "act_on_page", "web_search"],
            ),
            ToolParameter(
                name="task",
                description=(
                    "Goal description for `intent='act_on_page'` — "
                    "required there, rejected for `visit_url` and "
                    "`web_search`. State the objective in plain language "
                    "(\"add an item to the cart\", \"find the pricing "
                    "page\"), not the UI steps; the planner decides what "
                    "to click."
                ),
                required=False,
            ),
            ToolParameter(
                name="url",
                description=(
                    "Target URL (must include scheme, e.g. https://). "
                    "Required for `visit_url`. Optional for "
                    "`act_on_page` as a starting point — when omitted, "
                    "navigation begins from the current page. If the "
                    "browser is already on this URL, the navigation step "
                    "is skipped. Rejected for `web_search`."
                ),
                required=False,
            ),
            ToolParameter(
                name="query",
                description=(
                    "Search query for `intent='web_search'` — required "
                    "there, rejected for `visit_url` and `act_on_page`. "
                    "The query is sent to Bing as a normal search "
                    "string; no operators are required."
                ),
                required=False,
            ),
            ToolParameter(
                name="max_steps",
                type="integer",
                description=(
                    "Maximum number of planner turns for "
                    "`intent='act_on_page'`. Increase for multi-step "
                    "flows (forms, multi-page wizards); leave at default "
                    "for short navigations. Ignored for `visit_url` and "
                    "`web_search`. Default 5, hard cap 15."
                ),
                required=False,
            ),
        ],
        execute_fn=execute,
    )
