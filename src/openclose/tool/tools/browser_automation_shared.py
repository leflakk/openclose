"""Shared infrastructure for the unified browser_automation tool.

Houses the single-call lock, CDP connection, accessibility snapshots,
action executor, page dump, event helpers, output formatter, the
FailureReason enum, and the singleton browser holder.  The unified
tool module imports from here; this module imports from no tool
modules (no circular deps).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote_plus, urlparse

from openclose.config.paths import ConfigPaths
from openclose.id import generate_id
from openclose.tool.tool import ToolResult
from openclose.tool.truncation import truncate_output
from openclose.log import get_logger

log = get_logger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Module-global lock — only one browser automation call runs at a time.
# ---------------------------------------------------------------------------

BROWSER_AUTOMATION_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Shared configuration
# ---------------------------------------------------------------------------

CDP_URL = "http://127.0.0.1:9222"
VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 900
MAX_STEPS = 5
TIME_LIMIT_S = 300  # 5 minutes
POST_ACTION_WAIT_S = 0.5  # post-loop settle before dump_page_content

# Action families for adaptive post-action wait.
_NAV_ACTIONS = frozenset({"history_back"})
_MAYBE_NAV_ACTIONS = frozenset({"left_click", "key", "type"})

MAX_SNAPSHOT_ELEMENTS = 300
SNAPSHOT_PAGE_TEXT_CHARS = 4000

INTERACTIVE_ROLES = frozenset({
    "link", "button", "textbox", "combobox", "listbox", "menuitem",
    "checkbox", "radio", "switch", "tab", "spinbutton", "slider",
    "searchbox", "option", "treeitem", "gridcell",
})

MAX_IFRAME_ELEMENTS = 50
MAX_IFRAME_ELEMENTS_TOTAL = 100

SNAPSHOT_DIFF_NEW_PAGE_THRESHOLD = 0.5

# Grounding-model / planner key-name variants → Playwright key names.
# Defensive layer: Playwright already accepts the canonical names
# ("Enter", "Escape", "Backspace", ...), but planners and grounding
# models drift toward shorthand or X11-style aliases. Every entry here
# turns a real-world miss into a correct press.
KEY_MAP: dict[str, str] = {
    # X11 / VLM-grounding variants.
    "Return": "Enter",
    "BackSpace": "Backspace",
    "space": " ",
    # Common shorthand the planner sometimes emits.
    "ESC": "Escape", "Esc": "Escape", "esc": "Escape",
    "ENTER": "Enter", "enter": "Enter",
    "TAB": "Tab", "tab": "Tab",
    "BACKSPACE": "Backspace", "backspace": "Backspace",
    "DEL": "Delete", "Del": "Delete", "del": "Delete",
    "CTRL": "Control", "Ctrl": "Control", "ctrl": "Control",
    "CMD": "Meta", "Cmd": "Meta", "cmd": "Meta",
    "ALT": "Alt", "alt": "Alt",
    "SHIFT": "Shift", "shift": "Shift",
    "SPACE": " ", "Space": " ",
    "UP": "ArrowUp", "Up": "ArrowUp",
    "DOWN": "ArrowDown", "Down": "ArrowDown",
    "LEFT": "ArrowLeft", "Left": "ArrowLeft",
    "RIGHT": "ArrowRight", "Right": "ArrowRight",
    "PAGEUP": "PageUp", "PageUP": "PageUp", "pageup": "PageUp",
    "PAGEDOWN": "PageDown", "PageDOWN": "PageDown", "pagedown": "PageDown",
    "HOME": "Home", "home": "Home",
    "END": "End", "end": "End",
}


async def wait_after_action(page: Any, action: str) -> None:
    """Adaptive post-action wait tuned to the action's navigation profile.

    Full-nav actions (``history_back``) wait for ``load`` and a short
    ``networkidle`` window so the next snapshot sees the new page fully
    mounted. Possibly-nav actions (left_click, key, type) get a short
    ``networkidle`` window to catch SPA route changes and XHR — typing
    typically fires autocomplete / search XHR (and frequently submits a
    form via the optional inner Enter), so it benefits from the same
    settle window as click. Local actions just settle briefly. All
    load-state waits have their timeouts swallowed — long-poll /
    websocket sites never reach ``networkidle`` and must not stall the
    loop.
    """
    if action in _NAV_ACTIONS:
        try:
            await page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
    elif action in _MAYBE_NAV_ACTIONS:
        try:
            await page.wait_for_load_state("networkidle", timeout=1500)
        except Exception:
            pass
    else:
        await asyncio.sleep(0.3)


async def settle_after_navigate(page: Any) -> None:
    """Post-navigation settle: ``load`` + ``networkidle`` + brief sleep.

    Mirrors the ``_NAV_ACTIONS`` branch of ``wait_after_action`` plus a
    final ``POST_ACTION_WAIT_S`` sleep, so callers that just navigated
    can hand the page to the planner / dumper with SPA hydration mostly
    done. Both load-state waits swallow their timeouts so long-poll /
    websocket sites never stall this helper.
    """
    try:
        await page.wait_for_load_state("load", timeout=10000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass
    await asyncio.sleep(POST_ACTION_WAIT_S)


# ---------------------------------------------------------------------------
# Failure reason enum
# ---------------------------------------------------------------------------

class FailureReason(str, Enum):
    """Structured failure diagnostics for browser_automation.

    Surfaced in tool metadata for logging and observability. The main
    agent reads only the human-readable Status / Reason / Hint lines in
    the output text — these enum values are not a routing signal.
    """
    ELEMENT_NOT_IN_TREE = "element_not_in_tree"
    ELEMENT_AMBIGUOUS = "element_ambiguous"
    PAGE_LOAD_TIMEOUT = "page_load_timeout"
    NAVIGATION_LOOP_DETECTED = "navigation_loop_detected"
    STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
    TASK_INFEASIBLE = "task_infeasible"


# ---------------------------------------------------------------------------
# Layer detection JS (injected via CDP Runtime.callFunctionOn)
# ---------------------------------------------------------------------------

GET_LAYER_INFO_JS = """\
function() {
    function getZ(el) {
        try { return parseInt(getComputedStyle(el).zIndex, 10) || 0; }
        catch(e) { return 0; }
    }
    function coversArea(el) {
        var r = el.getBoundingClientRect();
        return r.width > 100 && r.height > 50;
    }
    function getLabel(el) {
        try {
            return el.getAttribute('aria-label')
                || (el.querySelector('h1,h2,h3,legend') || {}).innerText
                    ?.substring(0, 60)
                || '';
        } catch(e) { return ''; }
    }
    var node = this;
    while (node && node !== document.body) {
        if (node.tagName === 'DIALOG' && node.hasAttribute('open'))
            return {id:'dialog:'+(node.id||'anon'), zIndex:getZ(node),
                    label:getLabel(node)};
        var role = node.getAttribute && node.getAttribute('role');
        if (role === 'dialog' || role === 'alertdialog')
            return {id:'dialog:'+(node.id||'anon'), zIndex:getZ(node),
                    label:getLabel(node)};
        if (node.getAttribute && node.getAttribute('aria-modal') === 'true')
            return {id:'dialog:'+(node.id||'anon'), zIndex:getZ(node),
                    label:getLabel(node)};
        try {
            var style = getComputedStyle(node);
            var pos = style.position;
            var z = parseInt(style.zIndex, 10);
            if ((pos === 'fixed' || pos === 'absolute') && z > 0
                    && coversArea(node)) {
                var c = node.className && typeof node.className === 'string'
                    ? node.className.split(' ')[0] : '';
                return {id:'overlay:'+(node.id||c||'anon'), zIndex:z,
                        label:getLabel(node)};
            }
        } catch(e) {}
        var cls = (node.className && typeof node.className === 'string')
            ? node.className.toLowerCase() : '';
        if (/modal|overlay|popup|popover|dropdown-menu/.test(cls)) {
            var z2 = getZ(node);
            return {id:'overlay:'+(node.id||cls.split(' ')[0]||'anon'),
                    zIndex:z2||1, label:getLabel(node)};
        }
        node = node.parentElement;
    }
    return {id:'main', zIndex:0, label:'Main page'};
}
"""


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def parse_model_response(text: str) -> tuple[str, dict[str, Any] | None]:
    """Parse model output into (thinking_text, action_arguments_or_None).

    Model outputs plain thinking text followed by:
        <tool_call>
        {"name": "browser_automation", "arguments": {...}}
        </tool_call>

    Everything before ``<tool_call>`` is the thinking.
    """
    parts = text.split("<tool_call>\n")
    if len(parts) < 2:
        parts = text.split("<tool_call>")
        if len(parts) < 2:
            return text.strip(), None

    thinking = parts[0].strip()
    action_text = parts[1].split("\n</tool_call>")[0].strip()
    if not action_text:
        action_text = parts[1].split("</tool_call>")[0].strip()

    try:
        parsed = json.loads(action_text)
    except json.JSONDecodeError:
        log.warning("Failed to parse tool_call JSON: %s", action_text[:200])
        return thinking, None

    args: dict[str, Any] | None = None
    if isinstance(parsed, dict) and "arguments" in parsed:
        if isinstance(parsed["arguments"], dict):
            args = parsed["arguments"]
    elif isinstance(parsed, dict) and "action" in parsed:
        args = parsed

    if args is None:
        return thinking, None

    # Lenient intent enforcement: required for every action except
    # ``terminate`` (which uses ``summary``). Missing intent is logged
    # and replaced with a placeholder so the planner doesn't lose a turn.
    action = args.get("action", "")
    if action and action != "terminate":
        intent = args.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            log.warning(
                "Planner action %r missing required 'intent' field", action,
            )
            args["intent"] = "(missing)"

    return thinking, args


# ---------------------------------------------------------------------------
# Action executor
# ---------------------------------------------------------------------------

async def execute_action(page: Any, action_args: dict[str, Any]) -> str:
    """Execute a browser action via Playwright and return a description."""
    action = action_args.get("action", "")

    if action == "left_click":
        coord = action_args.get("coordinate", [0, 0])
        x, y = coord[0], coord[1]
        await page.mouse.move(x, y, steps=random.randint(5, 15))
        await page.mouse.click(x, y, delay=random.randint(30, 120))
        return f"left_click({x}, {y})"

    elif action == "type":
        text = action_args.get("text", "")
        coord = action_args.get("coordinate")
        press_enter = action_args.get("press_enter", True)
        delete_existing = action_args.get("delete_existing_text", False)
        if coord and isinstance(coord, list) and len(coord) >= 2:
            await page.mouse.move(coord[0], coord[1], steps=random.randint(5, 15))
            await page.mouse.click(coord[0], coord[1], delay=random.randint(30, 120))
            # Let React/contenteditable wrappers settle focus on the
            # inner input before keystrokes start arriving.
            await asyncio.sleep(0.2)
        if delete_existing:
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("Backspace")
        await page.keyboard.type(text, delay=random.randint(20, 80))
        if press_enter:
            await page.keyboard.press("Enter")
        return f"type({text!r}, press_enter={press_enter}, delete_existing={delete_existing})"

    elif action == "key":
        keys = action_args.get("keys", [])
        for k in keys:
            mapped = KEY_MAP.get(k, k)
            await page.keyboard.press(mapped)
        return f"key({keys})"

    elif action == "mouse_move":
        coord = action_args.get("coordinate", [0, 0])
        x, y = coord[0], coord[1]
        await page.mouse.move(x, y, steps=random.randint(5, 15))
        return f"mouse_move({x}, {y})"

    elif action == "scroll":
        coord = action_args.get("coordinate", [0, 0])
        pixels = action_args.get("pixels", 0)
        if coord and isinstance(coord, list) and len(coord) >= 2:
            await page.mouse.move(coord[0], coord[1], steps=random.randint(3, 8))
        # Grounding model: positive = up. Playwright mouse.wheel: positive delta_y = down.
        await page.mouse.wheel(0, -pixels)
        return f"scroll(pixels={pixels})"

    elif action == "history_back":
        await page.go_back(wait_until="domcontentloaded", timeout=10000)
        return "history_back()"

    elif action == "wait":
        seconds = min(action_args.get("time", 1), 10)
        await asyncio.sleep(seconds)
        return f"wait({seconds}s)"

    else:
        return f"unknown_action({action})"


# ---------------------------------------------------------------------------
# Batched layer detection for snapshot_a11y
# ---------------------------------------------------------------------------

async def _detect_layers_batched(
    elements: list[dict[str, Any]],
    context: Any,
    page: Any,
) -> None:
    """Annotate each element in *elements* with ``layer_id`` / ``layer_z``
    / ``layer_label`` in-place. Pipelined CDP calls in two concurrent
    waves — preserves the exact same per-element JS (``GET_LAYER_INFO_JS``)
    as the prior serial loop, only the CDP-call concurrency changes."""

    # Iframe nodes and nodes without a backend_node_id default to main.
    main_only: list[dict[str, Any]] = []
    for el in elements:
        if el["frame_id"] != "main" or el.get("backend_node_id") is None:
            el["layer_id"] = "main"
            el["layer_z"] = 0
            el["layer_label"] = (
                "Main page" if el["frame_id"] != "main" else "Main page"
            )
        else:
            main_only.append(el)

    if not main_only:
        return

    try:
        session = await context.new_cdp_session(page)
    except Exception:
        for el in main_only:
            el.setdefault("layer_id", "main")
            el.setdefault("layer_z", 0)
            el.setdefault("layer_label", "")
        return

    try:
        # Wave 1: resolve all backendNodeIds concurrently.
        resolve_results = await asyncio.gather(
            *[
                session.send(
                    "DOM.resolveNode",
                    {"backendNodeId": el["backend_node_id"]},
                )
                for el in main_only
            ],
            return_exceptions=True,
        )

        # Build a list of (element, objectId) for the elements that resolved.
        wave2_pairs: list[tuple[dict[str, Any], str]] = []
        for el, res in zip(main_only, resolve_results):
            if isinstance(res, BaseException):
                el["layer_id"] = "main"
                el["layer_z"] = 0
                el["layer_label"] = ""
                continue
            obj = res.get("object", {}) if isinstance(res, dict) else {}
            obj_id = obj.get("objectId")
            if not obj_id:
                el["layer_id"] = "main"
                el["layer_z"] = 0
                el["layer_label"] = ""
                continue
            wave2_pairs.append((el, obj_id))

        if wave2_pairs:
            # Wave 2: callFunctionOn concurrently.
            layer_results = await asyncio.gather(
                *[
                    session.send(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": obj_id,
                            "functionDeclaration": GET_LAYER_INFO_JS,
                            "returnByValue": True,
                        },
                    )
                    for _el, obj_id in wave2_pairs
                ],
                return_exceptions=True,
            )
            for (el, _obj_id), lr in zip(wave2_pairs, layer_results):
                if isinstance(lr, BaseException):
                    el["layer_id"] = "main"
                    el["layer_z"] = 0
                    el["layer_label"] = ""
                    continue
                info = lr.get("result", {}).get("value") if isinstance(lr, dict) else None
                info = info or {}
                el["layer_id"] = info.get("id", "main")
                el["layer_z"] = info.get("zIndex", 0)
                el["layer_label"] = info.get("label", "")
    except Exception:
        # Belt-and-suspenders: if anything else explodes, default everyone.
        for el in main_only:
            el.setdefault("layer_id", "main")
            el.setdefault("layer_z", 0)
            el.setdefault("layer_label", "")
    finally:
        try:
            await session.detach()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DOM snapshot — legacy JS-based fallback
# ---------------------------------------------------------------------------

_SNAPSHOT_JS = """\
() => {
    const SELECTORS = 'a[href], button, input, select, textarea, ' +
        '[role="button"], [role="link"], [role="tab"], [role="menuitem"], ' +
        '[role="checkbox"], [role="radio"], [role="switch"], [role="option"], ' +
        '[tabindex]:not([tabindex="-1"]), details > summary, ' +
        '[contenteditable]:not([contenteditable="false"]), ' +
        '[role="textbox"], [role="combobox"], [role="searchbox"], ' +
        '[role="spinbutton"]';
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const seen = new Set();
    const results = [];
    for (const el of document.querySelectorAll(SELECTORS)) {
        if (seen.has(el)) continue;
        seen.add(el);
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' ||
            style.opacity === '0') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        let text = el.getAttribute('aria-label') ||
                   el.innerText || el.value || el.placeholder ||
                   el.alt || el.title || '';
        text = text.trim().replace(/\\s+/g, ' ').substring(0, 80);
        results.push({
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute('role') || '',
            text: text,
            href: el.href || '',
            type: el.type || '',
            center_x: rect.left + rect.width / 2,
            center_y: rect.top + rect.height / 2,
            in_viewport: rect.bottom > 0 && rect.top < vh &&
                         rect.right > 0 && rect.left < vw,
            disabled: el.disabled || false,
            checked: el.checked || false,
        });
    }
    return results;
}
"""


async def snapshot_dom_legacy(
    page: Any,
    max_elements: int = MAX_SNAPSHOT_ELEMENTS,
    page_text_chars: int = SNAPSHOT_PAGE_TEXT_CHARS,
) -> tuple[str, dict[int, dict[str, Any]]]:
    """Legacy DOM scraper — fallback when CDP accessibility snapshot fails.

    Returns ``(snapshot_text, element_map)`` where *element_map* maps each
    ``[index]`` to ``{center_x, center_y, ...}`` in viewport pixels.
    """
    try:
        raw_elements: list[dict[str, Any]] = await page.evaluate(_SNAPSHOT_JS)
    except Exception as e:
        log.warning("DOM snapshot JS failed: %s", e)
        raw_elements = []

    in_vp = [el for el in raw_elements if el.get("in_viewport")]
    off_vp = [el for el in raw_elements if not el.get("in_viewport")]
    ordered = in_vp + off_vp
    capped = ordered[:max_elements]
    overflow = len(ordered) - len(capped)

    element_map: dict[int, dict[str, Any]] = {}
    lines: list[str] = []
    for idx, el in enumerate(capped):
        element_map[idx] = el
        tag = el.get("tag", "")
        role = el.get("role", "")
        text = el.get("text", "")
        href = el.get("href", "")
        etype = el.get("type", "")
        disabled = el.get("disabled", False)

        if role:
            label = role
        elif tag == "a":
            label = "link"
        elif tag == "button":
            label = "button"
        elif tag == "input":
            label = f"input[{etype}]" if etype else "input"
        elif tag == "select":
            label = "select"
        elif tag == "textarea":
            label = "textarea"
        else:
            label = tag

        parts = [f"[{idx}] {label}"]
        if text:
            parts.append(f'"{text}"')
        if href:
            short_href = href if len(href) <= 60 else href[:57] + "..."
            parts.append(f"(href={short_href})")
        if disabled:
            parts.append("[disabled]")
        lines.append(" ".join(parts))

    if overflow > 0:
        lines.append(
            f"... and {overflow} more elements off-screen (scroll to reveal)"
        )

    try:
        page_url = page.url
    except Exception:
        page_url = ""
    try:
        page_title = await page.title()
    except Exception:
        page_title = ""

    try:
        body_text = await page.inner_text("body")
        if len(body_text) > page_text_chars:
            body_text = (
                body_text[:page_text_chars]
                + "\n... [page text truncated]"
            )
    except Exception:
        body_text = ""

    header = (
        f"Page URL: {page_url}\n"
        f"Page title: {page_title}\n"
    )
    elements_section = (
        "Interactive elements:\n" + "\n".join(lines)
        if lines
        else "Interactive elements: (none found — page may still be loading)"
    )
    text_section = (
        f"\nPage text (abbreviated):\n{body_text}" if body_text else ""
    )
    snapshot_text = f"{header}\n{elements_section}{text_section}"

    return snapshot_text, element_map


# ---------------------------------------------------------------------------
# Accessibility-tree snapshot via CDP
# ---------------------------------------------------------------------------

async def snapshot_a11y(
    page: Any,
    context: Any,
    max_elements: int = MAX_SNAPSHOT_ELEMENTS,
    page_text_chars: int = SNAPSHOT_PAGE_TEXT_CHARS,
) -> tuple[str, dict[int, dict[str, Any]]]:
    """Snapshot interactive elements via Chrome's accessibility tree.

    On a ``RuntimeError`` from the sanity check (sparse a11y tree on a
    content-rich page — common with SPAs still mounting), waits 1.5s and
    retries once. If the retry also raises, falls back to the legacy DOM
    scraper. Any other exception falls back immediately.
    """
    try:
        return await _snapshot_a11y_impl(
            page, context, max_elements, page_text_chars,
        )
    except RuntimeError as e:
        log.info("A11y snapshot sparse, retrying once after 1.5s: %s", e)
        await asyncio.sleep(1.5)
        try:
            return await _snapshot_a11y_impl(
                page, context, max_elements, page_text_chars,
            )
        except Exception as e2:
            log.warning(
                "A11y snapshot still failing after retry, "
                "falling back to legacy DOM scraper: %s", e2,
            )
            return await snapshot_dom_legacy(
                page, max_elements, page_text_chars,
            )
    except Exception as e:
        log.warning(
            "A11y snapshot failed, falling back to legacy DOM scraper: %s", e,
        )
        return await snapshot_dom_legacy(page, max_elements, page_text_chars)


async def _snapshot_a11y_impl(
    page: Any,
    context: Any,
    max_elements: int,
    page_text_chars: int,
) -> tuple[str, dict[int, dict[str, Any]]]:
    """Inner implementation — raises on failure so the wrapper can fall back."""

    session = await context.new_cdp_session(page)
    try:
        ax_result = await session.send("Accessibility.getFullAXTree")
        ax_nodes: list[dict[str, Any]] = ax_result.get("nodes", [])

        snap_result = await session.send(
            "DOMSnapshot.captureSnapshot",
            {"computedStyles": [], "includeDOMRects": True},
        )
        documents: list[dict[str, Any]] = snap_result.get("documents", [])

        scroll_info: dict[str, float] = await page.evaluate(
            "() => ({scrollX: window.scrollX, scrollY: window.scrollY, "
            "innerWidth: window.innerWidth, innerHeight: window.innerHeight})"
        )
    finally:
        await session.detach()

    scroll_x = scroll_info.get("scrollX", 0)
    scroll_y = scroll_info.get("scrollY", 0)
    vw = scroll_info.get("innerWidth", VIEWPORT_WIDTH)
    vh = scroll_info.get("innerHeight", VIEWPORT_HEIGHT)

    # Build backendNodeId → (doc_x, doc_y, width, height) lookup.
    bounds_map: dict[int, tuple[float, float, float, float]] = {}
    node_doc_idx: dict[int, int] = {}
    doc_frame_ids: list[str] = []

    for d_idx, doc in enumerate(documents):
        doc_frame_ids.append(doc.get("frameId", ""))
        nodes_block = doc.get("nodes", {})
        backend_ids: list[int] = nodes_block.get("backendNodeId", [])
        layout = doc.get("layout", {})
        layout_node_indices: list[int] = layout.get("nodeIndex", [])
        layout_bounds: list[list[float]] = layout.get("bounds", [])

        ni_to_bounds: dict[int, list[float]] = {}
        for j, ni in enumerate(layout_node_indices):
            if j < len(layout_bounds):
                ni_to_bounds[ni] = layout_bounds[j]

        for i, b_id in enumerate(backend_ids):
            if i in ni_to_bounds:
                bnd = ni_to_bounds[i]
                if len(bnd) >= 4:
                    bounds_map[b_id] = (bnd[0], bnd[1], bnd[2], bnd[3])
                    node_doc_idx[b_id] = d_idx

    # Iframe offset map.
    iframe_offsets: dict[int, tuple[float, float]] = {}
    iframe_offsets[0] = (0.0, 0.0)

    if len(documents) > 1:
        parent_doc = documents[0]
        cdi = parent_doc.get("nodes", {}).get("contentDocumentIndex", {})
        cdi_indices: list[int] = cdi.get("index", [])
        cdi_values: list[int] = cdi.get("value", [])

        p_layout = parent_doc.get("layout", {})
        p_layout_ni = p_layout.get("nodeIndex", [])
        p_layout_bounds = p_layout.get("bounds", [])
        p_ni_to_bounds: dict[int, list[float]] = {}
        for j, ni in enumerate(p_layout_ni):
            if j < len(p_layout_bounds):
                p_ni_to_bounds[ni] = p_layout_bounds[j]

        for k, parent_ni in enumerate(cdi_indices):
            if k < len(cdi_values):
                child_doc_idx = cdi_values[k]
                if parent_ni in p_ni_to_bounds:
                    pbnd = p_ni_to_bounds[parent_ni]
                    iframe_offsets[child_doc_idx] = (pbnd[0], pbnd[1])
                else:
                    iframe_offsets[child_doc_idx] = (0.0, 0.0)

    main_frame_id = doc_frame_ids[0] if doc_frame_ids else ""

    # Filter a11y nodes to interactive roles and resolve coordinates.
    elements: list[dict[str, Any]] = []

    for ax_node in ax_nodes:
        if ax_node.get("ignored"):
            continue
        role_obj = ax_node.get("role")
        if not isinstance(role_obj, dict):
            continue
        role_value: str = role_obj.get("value", "")
        if role_value not in INTERACTIVE_ROLES:
            continue

        bid: int | None = ax_node.get("backendDOMNodeId")
        if bid is None or bid not in bounds_map:
            continue

        doc_x, doc_y, w, h = bounds_map[bid]
        if w == 0 or h == 0:
            continue

        d_idx = node_doc_idx.get(bid, 0)
        off_x, off_y = iframe_offsets.get(d_idx, (0.0, 0.0))
        abs_x = doc_x + off_x
        abs_y = doc_y + off_y

        vp_x = abs_x - scroll_x
        vp_y = abs_y - scroll_y
        center_x = vp_x + w / 2
        center_y = vp_y + h / 2

        name_obj = ax_node.get("name")
        name_value: str = ""
        if isinstance(name_obj, dict):
            name_value = str(name_obj.get("value", ""))
        name_value = name_value.strip().replace("\n", " ")[:80]

        props: dict[str, Any] = {}
        for prop in ax_node.get("properties", []):
            pname = prop.get("name", "")
            pval = prop.get("value", {})
            props[pname] = pval.get("value") if isinstance(pval, dict) else pval

        frame_id_raw = ax_node.get("frameId", main_frame_id)
        frame_id = "main" if frame_id_raw == main_frame_id else frame_id_raw

        in_viewport = (
            vp_x + w > 0 and vp_x < vw
            and vp_y + h > 0 and vp_y < vh
        )

        val_obj = ax_node.get("value")
        element_value = ""
        if isinstance(val_obj, dict):
            element_value = str(val_obj.get("value", ""))

        elements.append({
            "tag": "",
            "role": role_value,
            "text": name_value,
            "href": "",
            "type": "",
            "center_x": center_x,
            "center_y": center_y,
            "in_viewport": in_viewport,
            "disabled": bool(props.get("disabled", False)),
            "checked": bool(props.get("checked", False)),
            "focused": bool(props.get("focused", False)),
            "expanded": props.get("expanded"),
            "value": element_value,
            "backend_node_id": bid,
            "frame_id": frame_id,
        })

    # Sanity check.
    if len(elements) < 3:
        try:
            body_len = len(await page.inner_text("body"))
        except Exception:
            body_len = 0
        if body_len > 500:
            raise RuntimeError(
                f"A11y tree returned only {len(elements)} interactive "
                f"nodes on a page with {body_len} chars — likely broken"
            )

    # Detect visual layers via CDP DOM.resolveNode + JS getLayerId.
    # Pipelined inside one CDP session: two concurrent waves
    # (resolveNode then callFunctionOn) so a 150-element page costs
    # ~2 round-trips instead of 300 sequential ones.
    await _detect_layers_batched(elements, context, page)

    # Prioritise in-viewport, then off-viewport; apply caps.
    in_vp = [el for el in elements if el["in_viewport"]]
    off_vp = [el for el in elements if not el["in_viewport"]]
    ordered = in_vp + off_vp
    capped = ordered[:max_elements]
    overflow = len(ordered) - len(capped)

    # Build element_map and snapshot_text.
    element_map: dict[int, dict[str, Any]] = {}

    layer_groups: dict[str, list[str]] = {}
    layer_meta: dict[str, tuple[int, str]] = {}
    iframe_sections: dict[str, list[str]] = {}

    for idx, el in enumerate(capped):
        element_map[idx] = el
        role = el["role"]
        text = el["text"]
        frame_id = el["frame_id"]

        parts: list[str] = [f"[{idx}] {role}"]
        if text:
            parts.append(f'"{text}"')
        if el.get("focused"):
            parts.append("[focused]")
        if el.get("disabled"):
            parts.append("[disabled]")
        if el.get("checked"):
            parts.append("[checked]")
        exp = el.get("expanded")
        if exp is not None:
            parts.append("[expanded]" if exp else "[collapsed]")
        val = el.get("value", "")
        if val and val != text:
            if len(val) > 40:
                parts.append(f'(value="{val[:40]}..." [{len(val)} chars])')
            else:
                parts.append(f'(value="{val}")')

        line = " ".join(parts)
        if frame_id != "main":
            iframe_sections.setdefault(frame_id, []).append(line)
        else:
            layer_id = el.get("layer_id", "main")
            layer_z = el.get("layer_z", 0)
            layer_label = el.get("layer_label", "")
            layer_groups.setdefault(layer_id, []).append(line)
            prev_z, prev_label = layer_meta.get(layer_id, (0, ""))
            layer_meta[layer_id] = (
                max(prev_z, layer_z),
                layer_label or prev_label,
            )

    if overflow > 0:
        base = layer_groups.get("main")
        if base is not None:
            base.append(
                f"... and {overflow} more elements off-screen "
                f"(scroll to reveal)"
            )
        else:
            last_key = list(layer_groups.keys())[-1] if layer_groups else None
            if last_key is not None:
                layer_groups[last_key].append(
                    f"... and {overflow} more elements off-screen "
                    f"(scroll to reveal)"
                )

    try:
        page_url = page.url
    except Exception:
        page_url = ""
    try:
        page_title = await page.title()
    except Exception:
        page_title = ""

    try:
        body_text = await page.inner_text("body")
        if len(body_text) > page_text_chars:
            body_text = (
                body_text[:page_text_chars] + "\n... [page text truncated]"
            )
    except Exception:
        body_text = ""

    header = f"Page URL: {page_url}\nPage title: {page_title}\n"
    has_overlay = any(lid != "main" for lid in layer_groups)

    if not layer_groups:
        elements_section = (
            "Interactive elements: (none found — page may still be loading)"
        )
    elif has_overlay:
        sorted_layers = sorted(
            layer_groups.keys(),
            key=lambda lid: layer_meta.get(lid, (0, ""))[0],
            reverse=True,
        )
        section_parts: list[str] = []
        for i, lid in enumerate(sorted_layers):
            lines = layer_groups[lid]
            z, label = layer_meta.get(lid, (0, ""))
            if lid != "main" and i == 0:
                label_str = f'"{label}" ' if label else ""
                hdr = (
                    f"=== ACTIVE LAYER: {label_str}"
                    f"({lid}, z-index: {z}) ==="
                )
            elif lid == "main":
                hdr = "=== BACKGROUND: Main page ==="
            else:
                label_str = f'"{label}" ' if label else ""
                hdr = (
                    f"=== BACKGROUND: {label_str}"
                    f"({lid}, z-index: {z}) ==="
                )
            section_parts.append(hdr + "\n" + "\n".join(lines))
        elements_section = "\n\n".join(section_parts)
    else:
        main_lines = layer_groups.get("main", [])
        elements_section = (
            "Interactive elements:\n" + "\n".join(main_lines)
        )

    iframe_text = ""
    for fid, flines in iframe_sections.items():
        short_fid = fid if len(fid) <= 60 else fid[:57] + "..."
        iframe_text += f"\n\nInteractive elements (iframe: {short_fid}):\n"
        iframe_text += "\n".join(flines)

    text_section = (
        f"\nPage text (abbreviated):\n{body_text}" if body_text else ""
    )

    snapshot_text = f"{header}\n{elements_section}{iframe_text}{text_section}"

    return snapshot_text, element_map


# ---------------------------------------------------------------------------
# Recent actions summary
# ---------------------------------------------------------------------------

def summarise_recent_actions(
    steps_log: list[dict[str, Any]],
    n: int = 8,
) -> str:
    """Build a plain-text summary of the last *n* planner actions.

    The primary cross-turn memory carrier — survives image pruning and
    allows the planner to detect loops.
    """
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    pending_result_for: dict[str, dict[str, Any]] = {}
    for step in reversed(steps_log):
        if step.get("subagent_label") != "Planner":
            continue
        stype = step.get("type")
        if stype == "tool_result":
            tcid = step.get("tool_call_id", "")
            if tcid:
                pending_result_for[tcid] = step
        elif stype == "tool_call":
            tcid = step.get("tool_call_id", "")
            result_step = pending_result_for.pop(tcid, None)
            pairs.append((step, result_step))
            if len(pairs) >= n:
                break

    if not pairs:
        return "(no actions yet)"

    pairs.reverse()
    lines: list[str] = []
    for i, (call_step, result_step) in enumerate(pairs, 1):
        raw = call_step.get("content", "") or "{}"
        try:
            call_args = json.loads(raw)
        except json.JSONDecodeError:
            call_args = {}
        action = call_args.get("action", "?")
        if "element_index" in call_args and call_args["element_index"] is not None:
            desc = f"element_index={call_args['element_index']}"
        elif "target" in call_args and call_args["target"]:
            desc = f"target={call_args['target']!r}"
        elif "url" in call_args:
            desc = f"url={call_args['url']!r}"
        elif "query" in call_args:
            desc = f"query={call_args['query']!r}"
        elif "text" in call_args:
            text_preview = str(call_args["text"])
            if len(text_preview) > 40:
                text_preview = text_preview[:40] + "…"
            desc = f"text={text_preview!r}"
        elif "keys" in call_args:
            desc = f"keys={call_args['keys']}"
        elif "pixels" in call_args:
            desc = f"pixels={call_args['pixels']}"
        else:
            desc = ""
        intent = call_args.get("intent", "")
        if isinstance(intent, str) and intent:
            intent_preview = intent if len(intent) <= 60 else intent[:60] + "…"
            intent_suffix = f" [intent: {intent_preview!r}]"
        else:
            intent_suffix = ""
        result_text = (result_step or {}).get("content", "") or "(no result)"
        if len(result_text) > 120:
            result_text = result_text[:250] + "…"
        lines.append(f"{i}. {action}({desc}){intent_suffix} → {result_text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Snapshot diff
# ---------------------------------------------------------------------------

def strip_element_indices(text: str) -> str:
    """Remove ``[N] `` prefixes so index renumbering doesn't cause false diffs."""
    return re.sub(r"^\[\d+\]\s*", "", text, flags=re.MULTILINE)


def compute_snapshot_diff(prev_snapshot: str, curr_snapshot: str) -> str:
    """Order-insensitive bag-of-lines diff between two snapshots.

    Returns an empty string when *prev_snapshot* is empty (first step).
    """
    if not prev_snapshot:
        return ""

    prev_stripped = strip_element_indices(prev_snapshot)
    curr_stripped = strip_element_indices(curr_snapshot)

    prev_lines = [line for line in prev_stripped.splitlines() if line.strip()]
    curr_lines = [line for line in curr_stripped.splitlines() if line.strip()]

    prev_counter: Counter[str] = Counter(prev_lines)
    curr_counter: Counter[str] = Counter(curr_lines)

    added = curr_counter - prev_counter
    removed = prev_counter - curr_counter

    if not added and not removed:
        return "No changes detected."

    churn = (len(added) + len(removed)) / max(
        len(prev_lines) + len(curr_lines), 1
    )
    if churn > SNAPSHOT_DIFF_NEW_PAGE_THRESHOLD:
        return "Page changed significantly (likely a new page)."

    parts: list[str] = []
    added_lines = sorted(added.elements())
    removed_lines = sorted(removed.elements())
    if added_lines:
        parts.append("New:")
        for line in added_lines[:20]:
            parts.append(f"  + {line}")
        if len(added_lines) > 20:
            parts.append(f"  ... and {len(added_lines) - 20} more")
    if removed_lines:
        parts.append("Gone:")
        for line in removed_lines[:20]:
            parts.append(f"  - {line}")
        if len(removed_lines) > 20:
            parts.append(f"  ... and {len(removed_lines) - 20} more")
    return "\n".join(parts)


def describe_outcome(
    prev_snapshot: str, curr_snapshot: str,
    prev_url: str, curr_url: str,
) -> str:
    """Structured description of what the previous action changed.

    Empty string when there is no prior snapshot (step 0). Otherwise a
    URL line plus a DOM summary, derived from ``compute_snapshot_diff``.
    Silent-failure detection ("no changes detected") is the main thing
    the planner needs from this block.
    """
    if not prev_snapshot:
        return ""

    lines: list[str] = []
    if curr_url != prev_url:
        lines.append(f"URL: changed ({prev_url} → {curr_url})")
    else:
        lines.append("URL: unchanged")

    diff = compute_snapshot_diff(prev_snapshot, curr_snapshot)
    if diff == "No changes detected.":
        lines.append(
            "DOM: no changes detected — previous action likely had no "
            "effect (blocked by overlay, disabled, or wrong target)"
        )
    elif diff.startswith("Page changed significantly"):
        lines.append(
            "DOM: large-scale change (likely new page or full re-render)"
        )
    else:
        lines.append("DOM changes:")
        lines.append(diff)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Element resolution (DOM element_index → viewport coordinates)
# ---------------------------------------------------------------------------

async def resolve_element_index(
    action_args: dict[str, Any],
    element_map: dict[int, dict[str, Any]],
    page: Any = None,
    context: Any = None,
) -> tuple[dict[str, Any] | None, str]:
    """Convert an ``element_index`` into viewport coordinates.

    Same return contract as visual_ground: ``(result_dict, status_text)``
    on success, ``(None, error_text)`` on failure.

    When *page* and *context* are provided and the element carries a
    ``backend_node_id``, a JIT re-resolve via CDP fetches the element's
    current bounding rect to compensate for layout shifts.
    """
    idx = action_args.get("element_index")
    if idx is None or not isinstance(idx, (int, float)):
        return None, "no valid element_index in planner response"

    idx = int(idx)
    if idx not in element_map:
        max_idx = max(element_map.keys()) if element_map else -1
        return None, (
            f"element_index {idx} not in current snapshot "
            f"(valid range: 0–{max_idx})"
        )

    elem = element_map[idx]
    cx = elem["center_x"]
    cy = elem["center_y"]
    jit_used = False

    backend_node_id = elem.get("backend_node_id")
    if page is not None and context is not None and backend_node_id is not None:
        try:
            session = await context.new_cdp_session(page)
            try:
                frame_id = elem.get("frame_id", "main")
                if frame_id != "main":
                    for frame in page.frames:
                        if frame.url == frame_id or frame.name == frame_id:
                            await session.detach()
                            session = await context.new_cdp_session(frame)
                            break

                box_result = await session.send(
                    "DOM.getBoxModel",
                    {"backendNodeId": backend_node_id},
                )
                model = box_result.get("model", {})
                content_quad = model.get("content", [])
                if len(content_quad) >= 8:
                    xs = [content_quad[i] for i in (0, 2, 4, 6)]
                    ys = [content_quad[i] for i in (1, 3, 5, 7)]
                    cx = sum(xs) / 4
                    cy = sum(ys) / 4
                    jit_used = True
            finally:
                await session.detach()
        except Exception as e:
            log.debug(
                "JIT re-resolve failed for element %d (backendNodeId=%s): %s",
                idx, backend_node_id, e,
            )

    result = {
        k: v for k, v in action_args.items()
        if k not in ("element_index", "target")
    }
    result["coordinate"] = [cx, cy]

    role = elem.get("role", "") or elem.get("tag", "")
    text_preview = elem.get("text", "")[:40]
    jit_tag = " (JIT)" if jit_used else ""
    return result, (
        f"resolved [{idx}] {role} \"{text_preview}\" "
        f"→ ({cx:.0f}, {cy:.0f}){jit_tag}"
    )


def element_resolution_failure_message(
    action_args: dict[str, Any],
    reason: str,
) -> str:
    """Observation for the planner when element resolution fails."""
    idx = action_args.get("element_index", "?")
    action = action_args.get("action", "?")
    return (
        f"Element resolution failed for element_index {idx} "
        f"(action \"{action}\"): {reason}. "
        f"The page may have changed since the snapshot was taken. "
        f"Look at the refreshed DOM snapshot on the next turn and pick "
        f"a valid index, or try scrolling/waiting to reveal the element."
    )


# ---------------------------------------------------------------------------
# Fuzzy a11y matcher — turn a planner ``target`` description into an
# element_index when it cleanly matches a row in the current snapshot.
#
# The single biggest cost saver in the unified tool: most pixel actions
# the planner emits in rich mode have an obvious match in the a11y
# tree, so we resolve them for free instead of paying for a grounding
# LLM call.
# ---------------------------------------------------------------------------

FUZZY_MATCH_THRESHOLD = 60
_FUZZY_PUNCT_RE = re.compile(r"[,.;:!?\"]")
_FUZZY_WS_RE = re.compile(r"\s+")


def _fuzzy_normalize(text: str) -> str:
    if not text:
        return ""
    cleaned = _FUZZY_PUNCT_RE.sub(" ", text.lower())
    return _FUZZY_WS_RE.sub(" ", cleaned).strip()


def _fuzzy_score(target_norm: str, haystack_norm: str) -> int:
    """Score how well *target_norm* matches *haystack_norm* (both normalized).

    100 = exact equality, 80 = whole-word equality of a single token,
    60 = contiguous substring, 40 = ≥80% target tokens present as
    whole words in haystack, else 0.
    """
    if not target_norm or not haystack_norm:
        return 0
    if target_norm == haystack_norm:
        return 100
    haystack_tokens = haystack_norm.split()
    if target_norm in haystack_tokens:
        return 80
    if target_norm in haystack_norm:
        return 60
    target_tokens = target_norm.split()
    if not target_tokens:
        return 0
    haystack_set = set(haystack_tokens)
    matched = sum(1 for t in target_tokens if t in haystack_set)
    if matched / len(target_tokens) >= 0.8:
        return 40
    return 0


def fuzzy_match_a11y(
    target: str,
    element_map: dict[int, dict[str, Any]],
) -> tuple[int | None, str]:
    """Return ``(idx | None, status_text)``.

    Idx is the element_map key for the best fuzzy match against
    ``role + text`` of each entry, or ``None`` when nothing scores
    at or above ``FUZZY_MATCH_THRESHOLD`` or when the top two are
    too close to call (genuine ambiguity).
    """
    target_norm = _fuzzy_normalize(target)
    if not target_norm or not element_map:
        return None, f"no a11y match for {target!r} (empty input)"

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, el in element_map.items():
        if el.get("disabled"):
            continue
        haystack = f"{el.get('role', '')} {el.get('text', '')}"
        haystack_norm = _fuzzy_normalize(haystack)
        score = _fuzzy_score(target_norm, haystack_norm)
        if el.get("in_viewport"):
            score += 10
        if score > 0:
            scored.append((score, idx, el))

    if not scored:
        return None, f"no a11y match for {target!r} (no candidates)"

    # Sort: higher score first, then shorter haystack (more specific),
    # then in-viewport, then lower idx.
    def _sort_key(t: tuple[int, int, dict[str, Any]]) -> tuple[int, int, int, int]:
        score, idx, el = t
        haystack_len = len(_fuzzy_normalize(
            f"{el.get('role', '')} {el.get('text', '')}"
        ))
        in_vp_priority = 0 if el.get("in_viewport") else 1
        return (-score, haystack_len, in_vp_priority, idx)

    scored.sort(key=_sort_key)
    top_score, top_idx, top_el = scored[0]

    if top_score < FUZZY_MATCH_THRESHOLD:
        return None, (
            f"no a11y match for {target!r} "
            f"(best score {top_score} < {FUZZY_MATCH_THRESHOLD})"
        )

    # Ambiguity guard: reject when the top two non-exact matches are
    # within 5 points and both below the strict (80) cutoff. Falls
    # through to grounding (rich) or fail (dom).
    if len(scored) >= 2:
        runner_score = scored[1][0]
        if (
            top_score < 80
            and runner_score >= top_score - 5
        ):
            return None, (
                f"a11y match ambiguous for {target!r} "
                f"(scores {top_score} vs {runner_score})"
            )

    text_preview = (top_el.get("text", "") or "")[:40]
    role = top_el.get("role", "") or top_el.get("tag", "")
    return top_idx, (
        f'fuzzy a11y match: {target!r} → [{top_idx}] {role} "{text_preview}" '
        f"(score {top_score})"
    )


# ---------------------------------------------------------------------------
# Page text walker — visible text with inline `[text](href)` for anchors.
# ---------------------------------------------------------------------------
# Mimics `inner_text` semantics (visibility filtering, block-level newlines,
# whitespace collapse) but substitutes markdown link syntax wherever a
# `<a href>` is encountered in reading order. Result feeds both the
# `.md` navigation dump and the `--- Page content ---` agent block, so
# the agent can correlate post titles with their URLs without falling
# back to the deduped `--- Links on page ---` snippet.

_PAGE_TEXT_WALKER_JS = r"""
() => {
  const SKIP_TAGS = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD']);
  const BLOCK = new Set([
    'ADDRESS','ARTICLE','ASIDE','BLOCKQUOTE','DIV','DL','DT','DD',
    'FIELDSET','FIGCAPTION','FIGURE','FOOTER','FORM','H1','H2','H3','H4',
    'H5','H6','HEADER','HR','LI','MAIN','NAV','OL','P','PRE','SECTION',
    'TABLE','TR','TD','TH','THEAD','TBODY','TFOOT','UL'
  ]);

  function visible(el) {
    if (el.hidden) return false;
    const cs = getComputedStyle(el);
    if (!cs) return true;
    if (cs.display === 'none') return false;
    if (cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
    return true;
  }

  function isUsableHref(h) {
    if (!h) return false;
    const lower = h.trim().toLowerCase();
    if (lower.startsWith('javascript:')) return false;
    if (lower.startsWith('#')) return false;
    return true;
  }

  function plainText(node, sink) {
    if (node.nodeType === 3) { sink(node.data); return; }
    if (node.nodeType !== 1) return;
    if (SKIP_TAGS.has(node.tagName)) return;
    if (!visible(node)) return;
    if (node.tagName === 'BR') { sink('\n'); return; }
    for (const c of node.childNodes) plainText(c, sink);
  }

  function anchorLabel(a) {
    let t = '';
    plainText(a, s => t += s);
    t = t.replace(/\s+/g, ' ').trim();
    if (!t) {
      const img = a.querySelector('img[alt]');
      if (img) t = (img.alt || '').trim();
    }
    return t.replace(/[\[\]]/g, '');
  }

  let out = '';
  function emit(s) { out += s; }
  function nl() { if (!out.endsWith('\n')) emit('\n'); }

  function walk(node) {
    if (node.nodeType === 3) { emit(node.data); return; }
    if (node.nodeType !== 1) return;
    const tag = node.tagName;
    if (SKIP_TAGS.has(tag)) return;
    if (!visible(node)) return;

    if (tag === 'A') {
      const hrefAttr = node.getAttribute('href');
      const label = anchorLabel(node);
      if (label && isUsableHref(hrefAttr)) {
        emit('[' + label + '](' + (node.href || hrefAttr) + ')');
      } else if (label) {
        emit(label);
      }
      return;
    }

    if (tag === 'BR') { nl(); return; }

    const block = BLOCK.has(tag);
    if (block) nl();
    for (const c of node.childNodes) walk(c);
    if (block) nl();
  }

  if (document.body) walk(document.body);
  return out.split('\n')
            .map(l => l.replace(/[ \t ]+/g, ' ').trim())
            .join('\n')
            .replace(/\n{3,}/g, '\n\n')
            .trim();
}
"""


# ---------------------------------------------------------------------------
# DOM dump — extracts structured page content after navigation
# ---------------------------------------------------------------------------

async def dump_page_content(page: Any) -> dict[str, Any]:
    """Extract structured content from the current page for the main agent."""
    result: dict[str, Any] = {
        "url": "",
        "title": "",
        "page_text": "",
        "links": [],
    }

    try:
        result["url"] = page.url
    except Exception:
        pass

    try:
        result["title"] = await page.title()
    except Exception:
        pass

    try:
        result["page_text"] = await page.evaluate(_PAGE_TEXT_WALKER_JS)
    except Exception as e:
        result["page_text"] = f"(failed to extract page text: {e})"

    try:
        result["links"] = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => {"
            " let t = e.innerText.trim();"
            " if (t.length > 120) t = t.slice(0, 120) + '…';"
            " return {text: t, href: e.href};"
            "}).filter(l => l.text).slice(0, 500)"
        )
    except Exception:
        result["links"] = []

    # Interactive form elements — shadow DOM aware, layer-grouped.
    try:
        raw_elements: list[Any] = await page.evaluate(
            """() => {
    const R = [];
    const seen = new WeakSet();

    function _getZ(el) {
        try { return parseInt(getComputedStyle(el).zIndex, 10) || 0; }
        catch(e) { return 0; }
    }
    function _coversArea(el) {
        var r = el.getBoundingClientRect();
        return r.width > 100 && r.height > 50;
    }
    function _getLayerLabel(el) {
        try {
            return el.getAttribute('aria-label')
                || (el.querySelector('h1,h2,h3,legend') || {}).innerText
                    ?.substring(0, 60)
                || '';
        } catch(e) { return ''; }
    }
    function getLayerId(el) {
        var node = el;
        while (node && node !== document.body) {
            if (node.tagName === 'DIALOG' && node.hasAttribute('open'))
                return {id:'dialog:'+(node.id||'anon'),
                        zIndex:_getZ(node), label:_getLayerLabel(node)};
            var role = node.getAttribute && node.getAttribute('role');
            if (role === 'dialog' || role === 'alertdialog')
                return {id:'dialog:'+(node.id||'anon'),
                        zIndex:_getZ(node), label:_getLayerLabel(node)};
            if (node.getAttribute
                    && node.getAttribute('aria-modal') === 'true')
                return {id:'dialog:'+(node.id||'anon'),
                        zIndex:_getZ(node), label:_getLayerLabel(node)};
            try {
                var style = getComputedStyle(node);
                var pos = style.position;
                var z = parseInt(style.zIndex, 10);
                if ((pos === 'fixed' || pos === 'absolute') && z > 0
                        && _coversArea(node)) {
                    var c = node.className
                        && typeof node.className === 'string'
                        ? node.className.split(' ')[0] : '';
                    return {id:'overlay:'+(node.id||c||'anon'),
                            zIndex:z, label:_getLayerLabel(node)};
                }
            } catch(e) {}
            var cls = (node.className
                && typeof node.className === 'string')
                ? node.className.toLowerCase() : '';
            if (/modal|overlay|popup|popover|dropdown-menu/.test(cls)) {
                var z2 = _getZ(node);
                return {id:'overlay:'+(node.id||cls.split(' ')[0]||'anon'),
                        zIndex:z2||1, label:_getLayerLabel(node)};
            }
            node = node.parentElement;
        }
        return {id:'main', zIndex:0, label:'Main page'};
    }

    function getName(el) {
        const a = el.getAttribute('aria-label');
        if (a) return a.trim();
        const lb = el.getAttribute('aria-labelledby');
        if (lb) {
            const root = el.getRootNode();
            const parts = lb.split(/\\s+/).map(id => {
                const ref = root.getElementById ? root.getElementById(id) : null;
                return ref ? (ref.innerText || ref.textContent || '').trim() : '';
            }).filter(Boolean);
            if (parts.length) return parts.join(' ');
        }
        const inner = (el.innerText || '').trim();
        if (inner && inner.length <= 100) return inner.split('\\n')[0];
        const id = el.id;
        if (id && el.getRootNode().querySelector) {
            try {
                const lab = el.getRootNode().querySelector(
                    'label[for=\"' + CSS.escape(id) + '\"]'
                );
                if (lab) return (lab.innerText || lab.textContent || '').trim();
            } catch(e) {}
        }
        const pl = el.closest && el.closest('label');
        if (pl) {
            const c = pl.cloneNode(true);
            c.querySelectorAll('input,select,textarea').forEach(x => x.remove());
            const t = (c.innerText || c.textContent || '').trim();
            if (t) return t;
        }
        return el.getAttribute('value') || el.getAttribute('title')
            || el.getAttribute('placeholder') || '';
    }

    function desc(el) {
        const tag = el.tagName.toLowerCase();
        const tp = (el.getAttribute('type') || '').toLowerCase();
        const role = (el.getAttribute('role') || '').toLowerCase();
        let n = (getName(el) || '').trim();
        if (!n) return null;
        if (n.length > 80) n = n.substring(0, 77) + '...';
        const dis = el.disabled || el.getAttribute('aria-disabled') === 'true';
        const ds = dis ? ' [disabled]' : '';
        if (tag === 'button' || role === 'button'
            || (tag === 'input' && tp === 'submit'))
            return 'button \"' + n + '\"' + ds;
        if ((tag === 'input' && tp === 'radio') || role === 'radio') {
            const c = el.checked || el.getAttribute('aria-checked') === 'true';
            return 'radio \"' + n + '\" [' + (c ? 'selected' : 'unselected') + ']' + ds;
        }
        if ((tag === 'input' && tp === 'checkbox') || role === 'checkbox') {
            const c = el.checked || el.getAttribute('aria-checked') === 'true';
            return 'checkbox \"' + n + '\" ['
                + (c ? 'checked' : 'unchecked') + ']' + ds;
        }
        if (tag === 'select' || role === 'listbox') {
            const sel = el.selectedOptions
                ? Array.from(el.selectedOptions).map(o => o.text.trim()).join(', ')
                : '';
            return 'select \"' + n + '\"'
                + (sel ? ' [value: ' + sel + ']' : '') + ds;
        }
        if (role === 'switch') {
            const on = el.getAttribute('aria-checked') === 'true';
            return 'switch \"' + n + '\" [' + (on ? 'on' : 'off') + ']' + ds;
        }
        if (role === 'tab') {
            const act = el.getAttribute('aria-selected') === 'true';
            return 'tab \"' + n + '\"' + (act ? ' [active]' : '') + ds;
        }
        return null;
    }

    function walk(root) {
        if (!root || seen.has(root)) return;
        seen.add(root);
        const SEL = 'button, input[type=\"radio\"], input[type=\"checkbox\"], '
            + 'input[type=\"submit\"], select, [role=\"button\"], [role=\"radio\"], '
            + '[role=\"checkbox\"], [role=\"switch\"], [role=\"tab\"], '
            + '[role=\"listbox\"]';
        try {
            for (const el of root.querySelectorAll(SEL)) {
                if (seen.has(el)) continue;
                seen.add(el);
                try {
                    const s = getComputedStyle(el);
                    if (s.display === 'none' || s.visibility === 'hidden') continue;
                } catch(e) { continue; }
                const d = desc(el);
                if (d) {
                    const layer = getLayerId(el);
                    R.push({desc: d, layerId: layer.id,
                            layerZ: layer.zIndex, layerLabel: layer.label});
                }
            }
        } catch(e) {}
        try {
            for (const el of root.querySelectorAll('*')) {
                if (el.shadowRoot) walk(el.shadowRoot);
            }
        } catch(e) {}
    }

    walk(document);
    return R.slice(0, 300);
}"""
        )

        _layer_groups: dict[str, dict[str, Any]] = {}
        for item in raw_elements:
            lid = item.get("layerId", "main") if isinstance(item, dict) else "main"
            d = item.get("desc", item) if isinstance(item, dict) else item
            if lid not in _layer_groups:
                _layer_groups[lid] = {
                    "z": item.get("layerZ", 0) if isinstance(item, dict) else 0,
                    "label": item.get("layerLabel", "") if isinstance(item, dict) else "",
                    "elems": [],
                }
            _layer_groups[lid]["elems"].append(d)
            if isinstance(item, dict):
                _layer_groups[lid]["z"] = max(
                    _layer_groups[lid]["z"], item.get("layerZ", 0),
                )

        _has_overlay = any(lid != "main" for lid in _layer_groups)
        if _has_overlay:
            _sorted = sorted(
                _layer_groups.keys(),
                key=lambda lid: _layer_groups[lid]["z"],
                reverse=True,
            )
            formatted: list[str] = []
            for i, lid in enumerate(_sorted):
                g = _layer_groups[lid]
                if lid != "main" and i == 0:
                    lbl = f'"{g["label"]}" ' if g["label"] else ""
                    formatted.append(f"=== ACTIVE LAYER: {lbl}===")
                elif lid == "main":
                    formatted.append("=== BACKGROUND: Main page ===")
                else:
                    lbl = f'"{g["label"]}" ' if g["label"] else ""
                    formatted.append(f"=== BACKGROUND: {lbl}===")
                formatted.extend(g["elems"])
            result["interactive_elements"] = formatted
        else:
            result["interactive_elements"] = [
                item.get("desc", item) if isinstance(item, dict) else item
                for item in raw_elements
            ]
    except Exception:
        result["interactive_elements"] = []

    # Iframe content extraction.
    result["iframes"] = []
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            frame_text = await frame.evaluate(_PAGE_TEXT_WALKER_JS)
            frame_links = await frame.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => {"
                " let t = e.innerText.trim();"
                " if (t.length > 120) t = t.slice(0, 120) + '…';"
                " return {text: t, href: e.href};"
                "}).filter(l => l.text).slice(0, 100)",
            )
            result["iframes"].append({
                "url": frame.url,
                "text": frame_text,
                "links": frame_links,
            })
        except Exception:
            result["iframes"].append({
                "url": getattr(frame, "url", "(unknown)"),
                "text": "(inaccessible — cross-origin or detached)",
                "links": [],
            })

    return result


# ---------------------------------------------------------------------------
# Browser connection helper
# ---------------------------------------------------------------------------

def _page_looks_valid(page: Any) -> bool:
    """Cheap sync check — fast filter, can lie (Playwright caches state)."""
    try:
        if page.is_closed():
            return False
        mf = page.main_frame
        return mf is not None and not mf.is_detached()
    except Exception:
        return False


async def _page_is_alive(page: Any) -> bool:
    """Active probe — forces a CDP round-trip. Bounded by a timeout so
    a stuck page can't hang the caller."""
    if not _page_looks_valid(page):
        return False
    try:
        await asyncio.wait_for(page.evaluate("1"), timeout=2.0)
        return True
    except Exception:
        return False


async def _pick_or_create_page(context: Any) -> Any:
    """Return the first live page in the context, or a fresh one."""
    for p in context.pages:
        if await _page_is_alive(p):
            return p
    return await context.new_page()


def _pick_existing_page_for_view(context: Any) -> Any | None:
    """Return the most-recent page that looks valid, or None.

    Sync-only check via _page_looks_valid — no CDP round-trip, no
    timeout, never creates a page. Safe to call from UI viewers that
    must not disturb the agent's navigation. Prefers context.pages[-1]
    to match handle_tab_switch's notion of the active tab, then falls
    back to older pages if the newest is invalid.
    """
    pages = list(context.pages)
    if not pages:
        return None
    last = pages[-1]
    if _page_looks_valid(last):
        return last
    for p in reversed(pages[:-1]):
        if _page_looks_valid(p):
            return p
    return None


_PAGE_LIFECYCLE_ERROR_MARKERS = (
    "detached", "closed", "target", "crashed", "navigating frame was detached",
)


def _is_page_lifecycle_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _PAGE_LIFECYCLE_ERROR_MARKERS)


async def connect_browser() -> tuple[Any, Any, Any, Any]:
    """Connect to Chrome via CDP or launch Chromium.

    Returns ``(pw, browser, context, page)``. Caller must
    ``await pw.stop()`` in a finally block.
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = (
            browser.contexts[0]
            if browser.contexts
            else await browser.new_context(locale="en-US")
        )
        page = await _pick_or_create_page(context)
        log.info("Connected to Chrome via CDP at %s", CDP_URL)
    except Exception as cdp_err:
        log.info(
            "CDP connection failed (%s), launching Chromium with CDP",
            cdp_err,
        )
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--lang=en-US",
                "--start-maximized",
                f"--remote-debugging-port={CDP_URL.rsplit(':', 1)[-1]}",
            ],
        )
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()
    await page.set_viewport_size(
        {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
    )
    return pw, browser, context, page


# ---------------------------------------------------------------------------
# Singleton browser session — reused across tool invocations.
# The global BROWSER_AUTOMATION_LOCK already serializes calls, so a
# process-wide singleton is safe. Saves the ~200–500 ms CDP handshake
# on every call after the first.
# ---------------------------------------------------------------------------

_singleton_pw: Any = None
_singleton_browser: Any = None
_singleton_context: Any = None
_singleton_init_lock: asyncio.Lock = asyncio.Lock()


def _connection_is_alive(browser: Any) -> bool:
    if browser is None:
        return False
    try:
        return bool(browser.is_connected())
    except Exception:
        return False


async def _reset_singleton_locked() -> None:
    """Tear down singleton state. Caller must hold ``_singleton_init_lock``."""
    global _singleton_pw, _singleton_browser, _singleton_context
    if _singleton_pw is not None:
        try:
            await _singleton_pw.stop()
        except Exception:
            pass
    _singleton_pw = None
    _singleton_browser = None
    _singleton_context = None


async def acquire_singleton_browser() -> tuple[Any, Any, Any]:
    """Return ``(pw, browser, context)``, creating the singleton on first
    call and reusing it on subsequent calls. If the cached browser has
    died, reset and reconnect.
    """
    global _singleton_pw, _singleton_browser, _singleton_context
    async with _singleton_init_lock:
        if (
            _singleton_pw is not None
            and _connection_is_alive(_singleton_browser)
            and _singleton_context is not None
        ):
            return _singleton_pw, _singleton_browser, _singleton_context
        await _reset_singleton_locked()
        pw, browser, context, _page = await connect_browser()
        _singleton_pw = pw
        _singleton_browser = browser
        _singleton_context = context
        _attach_disconnect_handler(browser)
        return pw, browser, context


async def reset_singleton_browser() -> None:
    """Public reset hook — drops the cached browser. Next call to
    ``acquire_singleton_browser`` reconnects from scratch."""
    async with _singleton_init_lock:
        await _reset_singleton_locked()


async def acquire_singleton_browser_readonly() -> tuple[Any, Any, Any] | None:
    """Read-only counterpart to ``acquire_singleton_browser``.

    Returns ``(pw, browser, context)`` if a live singleton already exists
    OR if a fresh CDP connection succeeds against an already-running
    Chrome. Returns ``None`` if no Chrome is reachable — never launches a
    new browser, never creates a context or page.

    Intended for UI viewers (e.g. the screenshot endpoint) that must
    observe the agent's browser without disturbing it. Connecting via
    CDP to an already-running Chrome is non-disruptive; launching a new
    Chrome or creating new contexts/pages would be.
    """
    global _singleton_pw, _singleton_browser, _singleton_context
    async with _singleton_init_lock:
        if (
            _singleton_pw is not None
            and _connection_is_alive(_singleton_browser)
            and _singleton_context is not None
        ):
            return _singleton_pw, _singleton_browser, _singleton_context
        await _reset_singleton_locked()
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
        except Exception:
            try:
                await pw.stop()
            except Exception:
                pass
            return None
        if not browser.contexts:
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass
            return None
        context = browser.contexts[0]
        _singleton_pw = pw
        _singleton_browser = browser
        _singleton_context = context
        _attach_disconnect_handler(browser)
        return pw, browser, context


def _attach_disconnect_handler(browser: Any) -> None:
    """Clear singleton globals when *this* browser disconnects, so the next
    acquire reconnects from scratch instead of using a stale handle.

    The identity check guards against a stale handler firing after a
    successor singleton has already been installed.
    """
    def _on_disconnect(*_args: Any) -> None:
        global _singleton_pw, _singleton_browser, _singleton_context
        if _singleton_browser is browser:
            _singleton_pw = None
            _singleton_browser = None
            _singleton_context = None
            log.info("Browser disconnected — singleton cleared")
    try:
        browser.on("disconnected", _on_disconnect)
    except Exception:
        pass


async def navigate_initial_url(context: Any, page: Any, url: str) -> Any:
    """Navigate ``page`` to ``url`` with detached-frame recovery.

    Returns the page actually used — callers MUST rebind ``page`` to
    the return value, because recovery may open a fresh tab.
    """
    async def _try_goto(p: Any) -> None:
        try:
            current = (p.url or "").rstrip("/")
        except Exception:
            current = ""
        if current == url.rstrip("/"):
            log.info("Skipping navigation — browser is already at %s", url)
            return
        await p.goto(url, wait_until="domcontentloaded", timeout=15000)
        try:
            await p.wait_for_timeout(500)
        except Exception:
            pass

    try:
        await _try_goto(page)
        return page
    except Exception as e:
        # Retry on any lifecycle error (message match) OR if our probe now
        # says the page is dead. Real failures (DNS, timeout on live page)
        # don't match the markers and the probe still passes → re-raise.
        if not _is_page_lifecycle_error(e) and await _page_is_alive(page):
            raise
        log.warning("Initial goto failed (%s); retrying on a fresh page", e)
        page = await context.new_page()
        await page.set_viewport_size(
            {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
        )
        await _try_goto(page)
        return page


async def handle_tab_switch(context: Any, current_page: Any) -> Any:
    """Switch to a newly-opened tab if one appeared and is alive.

    Guards against switching to transient popups that closed themselves
    or tabs whose frames detached between open and switch — a blind
    switch to ``context.pages[-1]`` caused ``page.goto`` to fail with
    "Frame has been detached" on the next step.

    Returns the page to use going forward; callers MUST rebind.
    """
    all_pages = context.pages
    if len(all_pages) <= 1 or all_pages[-1] == current_page:
        return current_page

    new_page = all_pages[-1]
    if not await _page_is_alive(new_page):
        log.info(
            "New tab detected (%d total) but not alive, keeping current page",
            len(all_pages),
        )
        return current_page

    log.info("New tab detected (%d total), switching", len(all_pages))
    try:
        await current_page.close()
    except Exception as e:
        log.debug("Ignoring error closing old page: %s", e)
    try:
        await new_page.set_viewport_size(
            {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
        )
    except Exception as e:
        log.warning("Failed to set viewport on new tab: %s", e)
    return new_page


async def recover_page_if_dead(context: Any, page: Any) -> Any:
    """If ``page`` is dead (frame detached, target closed, crashed),
    return a fresh alive page from the context. Otherwise return the
    same page unchanged.

    Used after an action error in the planner loop so the next snapshot
    and action don't run against a detached frame. Callers MUST rebind.
    """
    if await _page_is_alive(page):
        return page
    log.warning("Page is no longer alive, recovering a fresh page")
    new_page = await _pick_or_create_page(context)
    try:
        await new_page.set_viewport_size(
            {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
        )
    except Exception as e:
        log.debug("Failed to set viewport on recovered page: %s", e)
    return new_page


# ---------------------------------------------------------------------------
# Event context and emitter helpers
# ---------------------------------------------------------------------------

@dataclass
class EventContext:
    """Bundles event-streaming state so emitter functions don't need
    three separate parameters at every call site."""

    sink: Any  # asyncio.Queue[StreamEvent | None] | None
    parent_tc_id: str
    steps_log: list[dict[str, Any]] = field(default_factory=list)

    def new_tc(self, label_action: str, args: dict[str, Any]) -> Any:
        from openclose.agent.loop import ToolCall
        from openclose.id import generate_id
        tc = ToolCall()
        tc.id = generate_id()
        tc.name = f"browser:{label_action}"
        tc._arguments = json.dumps(args, ensure_ascii=False)
        return tc

    async def emit_text(self, text: str, label: str) -> None:
        if self.sink and self.parent_tc_id and text:
            from openclose.agent.loop import StreamEvent
            await self.sink.put(StreamEvent(
                "subagent_text",
                content=text,
                parent_tool_call_id=self.parent_tc_id,
                metadata={"subagent_label": label},
            ))

    async def emit_call(self, tc: Any, label: str) -> None:
        if self.sink and self.parent_tc_id:
            from openclose.agent.loop import StreamEvent
            await self.sink.put(StreamEvent(
                "subagent_tool_call",
                tool_call=tc,
                parent_tool_call_id=self.parent_tc_id,
                metadata={"subagent_label": label},
            ))

    async def emit_result(self, tc: Any, text: str, label: str) -> None:
        if self.sink and self.parent_tc_id:
            from openclose.agent.loop import StreamEvent
            await self.sink.put(StreamEvent(
                "subagent_tool_result",
                tool_call=tc,
                tool_result=text,
                parent_tool_call_id=self.parent_tc_id,
                metadata={"subagent_label": label},
            ))

    def log_call(self, tc: Any, label: str) -> None:
        if len(self.steps_log) < 100:
            self.steps_log.append({
                "type": "tool_call",
                "tool_name": tc.name,
                "tool_call_id": tc.id,
                "content": tc._arguments,
                "subagent_label": label,
            })

    def log_result(self, tc: Any, text: str, label: str) -> None:
        if len(self.steps_log) < 100:
            self.steps_log.append({
                "type": "tool_result",
                "tool_name": tc.name,
                "tool_call_id": tc.id,
                "content": text[:500],
                "subagent_label": label,
            })

    async def emit_grounding_skip(
        self,
        action_name: str,
        args: dict[str, Any],
        reason: str,
    ) -> None:
        """Emit a placeholder Grounding row for steps that didn't invoke
        real pixel-grounding, keeping the UI columns in sync."""
        skip_tc = self.new_tc(action_name, args)
        await self.emit_call(skip_tc, "Grounding")
        self.log_call(skip_tc, "Grounding")
        result_text = f"Not required — {reason}."
        await self.emit_result(skip_tc, result_text, "Grounding")
        self.log_result(skip_tc, result_text, "Grounding")


# ---------------------------------------------------------------------------
# Intent validation
# ---------------------------------------------------------------------------

_VALID_INTENTS = frozenset({"visit_url", "act_on_page", "web_search"})


def validate_intent(
    intent: str, task: str, url: str, query: str = ""
) -> str | None:
    """Return an error message if the intent / task / url / query combo
    is invalid, else None. Called at tool entry before any browser work."""
    if not intent:
        return (
            "intent parameter is required. Use 'visit_url' to load a "
            "URL and read its body, 'web_search' to run a Bing search "
            "and read the results page, or 'act_on_page' with a task "
            "to reason about reaching a goal on the current page."
        )
    if intent not in _VALID_INTENTS:
        return (
            f"Unknown intent '{intent}'. Valid values: "
            f"{', '.join(sorted(_VALID_INTENTS))}."
        )
    if intent == "visit_url":
        if not url:
            return "intent='visit_url' requires a url."
        if task:
            return (
                "intent='visit_url' doesn't accept a task. "
                "Use intent='act_on_page' if reasoning is needed to "
                "reach the goal."
            )
        if query:
            return (
                "intent='visit_url' doesn't accept a query. "
                "Use intent='web_search' to run a Bing search."
            )
    elif intent == "web_search":
        if not query:
            return "intent='web_search' requires a query."
        if task:
            return (
                "intent='web_search' doesn't accept a task. "
                "Use intent='act_on_page' if reasoning is needed to "
                "reach the goal."
            )
        if url:
            return (
                "intent='web_search' doesn't accept a url. "
                "Use intent='visit_url' to open a specific URL."
            )
    else:  # act_on_page
        if not task:
            return (
                "intent='act_on_page' requires a task describing the goal."
            )
        if query:
            return (
                "intent='act_on_page' doesn't accept a query. "
                "Use intent='web_search' to run a Bing search."
            )
    return None


# ---------------------------------------------------------------------------
# Navigation dump persistence
# ---------------------------------------------------------------------------
# `dump_page_content` captures the full page. The agent-facing report
# returns the URL, title, interactive elements, links, and iframes (with
# AGENT_*_CAP applied at format time). The full visible page text is the
# heaviest section, so it is NEVER returned in-line — it is persisted to
# a markdown file under `~/.config/openclose/<project>/navigation/` and
# the agent recovers specific text by running `Grep`/`Read` on that
# file. Files are pruned to NAVIGATION_DUMP_KEEP most recent entries.

AGENT_LINKS_CAP = 30
AGENT_INTERACTIVE_CAP = 100
AGENT_IFRAME_TEXT_CAP = 6000
AGENT_IFRAME_LINKS_CAP = 20
NAVIGATION_DUMP_KEEP = 200


def _dedupe_with_counts(
    items: Iterable[T],
    key_fn: Callable[[T], Hashable],
) -> list[tuple[T, int]]:
    """Return [(item, count), ...] in first-occurrence order."""
    out: dict[Hashable, list[Any]] = {}
    for item in items:
        k = key_fn(item)
        if k in out:
            out[k][1] += 1
        else:
            out[k] = [item, 1]
    return [(v[0], v[1]) for v in out.values()]


def _with_count_suffix(s: str, n: int) -> str:
    return s if n == 1 else f"{s} (×{n})"


def _is_layer_separator(s: Any) -> bool:
    """True for the layer-separator strings interspersed in the
    interactive-elements list (``=== ACTIVE LAYER: ... ===`` /
    ``=== BACKGROUND: ... ===``). Tightened from a generic ``===`` check
    to avoid colliding with element descriptions that happen to contain
    ``===`` runs from page content."""
    return (
        isinstance(s, str)
        and (
            s.startswith("=== ACTIVE LAYER:")
            or s.startswith("=== BACKGROUND:")
        )
        and s.endswith("===")
    )

_DUMP_HEADER_PAGE_TEXT = "## Page content"


def _build_navigation_dump(
    page_content: dict[str, Any],
    captured_at: datetime,
) -> str:
    """Build the markdown body for the navigation dump file.

    Persists the visible page text with anchors inlined as
    ``[text](href)`` (produced by ``_PAGE_TEXT_WALKER_JS``). Interactive
    elements and iframes are not duplicated here — they live in the
    agent report. A small header carries URL/title/timestamp so a
    directory-wide grep can still locate dumps by source page.
    """
    url = page_content.get("url", "")
    title = page_content.get("title", "")
    page_text = page_content.get("page_text", "") or ""

    lines: list[str] = [
        "# Browser navigation dump",
        "",
        f"URL: {url}",
        f"Page title: {title}",
        f"Captured: {captured_at.isoformat()}",
        "",
        _DUMP_HEADER_PAGE_TEXT,
    ]
    lines.extend(page_text.splitlines() or [""])
    lines.append("")
    return "\n".join(lines)


_FILENAME_DOMAIN_RE = re.compile(r"[^a-zA-Z0-9.-]+")


def _make_navigation_filename(url: str, captured_at: datetime) -> str:
    """Return a sortable, cross-platform-safe filename.

    Format: ``<ISO-with-dashes>_<domain>_<id6>.md``. Colons are replaced
    with dashes so the name is valid on Windows. Domain is the URL's
    netloc (already punycode for IDN) reduced to ``[a-zA-Z0-9.-]`` and
    capped at 64 chars. Suffix is the first 6 chars of a fresh ULID, so
    two calls inside the same second don't collide.
    """
    try:
        domain = urlparse(url).netloc or "unknown"
    except Exception:
        domain = "unknown"
    domain = _FILENAME_DOMAIN_RE.sub("-", domain).strip("-") or "unknown"
    if len(domain) > 64:
        domain = domain[:64]
    ts = captured_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    suffix = generate_id()[:6]
    return f"{ts}_{domain}_{suffix}.md"


def _prune_navigation_dir(nav_dir: Path, keep_n: int) -> None:
    """Best-effort prune to the ``keep_n`` most recent ``*.md`` files.

    Sort is by filename (ISO-timestamp prefix sorts lexicographically by
    recency); avoids the per-file ``stat`` cost of mtime sorting on
    network/encrypted home dirs. Best-effort — individual unlink errors
    are swallowed; concurrent callers may each delete one extra file
    under load (no correctness impact).
    """
    try:
        files = sorted(
            (p for p in nav_dir.iterdir()
             if p.is_file() and p.suffix == ".md"),
            key=lambda p: p.name,
            reverse=True,
        )
    except FileNotFoundError:
        return
    except Exception as e:
        log.debug("nav-dir scan failed: %s", e)
        return
    for old in files[keep_n:]:
        try:
            old.unlink()
        except OSError:
            pass


def write_navigation_dump(
    project_dir: str,
    page_content: dict[str, Any],
) -> Path | None:
    """Persist the page text to a markdown file and return its path.

    Returns ``None`` when ``page_text`` is empty (nothing to persist) or
    on filesystem error. The caller is expected to surface the path in
    the agent-facing report so the agent can ``Grep``/``Read`` it.
    """
    if not (page_content.get("page_text") or "").strip():
        return None
    captured_at = datetime.now(tz=timezone.utc)
    try:
        nav_dir = ConfigPaths.project_runtime_dir(project_dir) / "navigation"
        nav_dir.mkdir(parents=True, exist_ok=True)
        _prune_navigation_dir(nav_dir, NAVIGATION_DUMP_KEEP)
        body = _build_navigation_dump(page_content, captured_at)
        filename = _make_navigation_filename(
            page_content.get("url", ""), captured_at,
        )
        final_path = nav_dir / filename
        # Atomic write: temp file in same dir, then os.replace.
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=nav_dir, prefix=".tmp-nav-", suffix=".md",
            delete=False,
        )
        try:
            tmp.write(body)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, final_path)
        return final_path
    except Exception as e:
        log.warning("navigation dump write failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Output formatter
# ---------------------------------------------------------------------------

def format_tool_output(
    final_status: str,
    last_thinking: str,
    steps_log: list[dict[str, Any]],
    page_content: dict[str, Any],
    failure_reason: FailureReason | None = None,
    short_mode: bool = False,
    dump_path: Path | None = None,
) -> str:
    """Build the output string returned to the main agent.

    When *failure_reason* is provided, it is surfaced as a
    ``failure_reason:`` line for diagnostics — it is not a routing
    signal. The accompanying ``Hint:`` line is plain English and
    never names another tool.

    When *short_mode* is True, suppress the content sections
    (interactive elements, links, iframes). The top-level fields
    (Status, Steps taken, URL, Page title, Reason/Hint, Actions
    attempted, Navigator observations) are always emitted so the
    output shape is consistent across intents.

    When *dump_path* is provided, the visible page text has been
    persisted to that file by ``write_navigation_dump``. A
    ``Page content saved at:`` line is emitted near the top with a
    Grep/Read recovery hint. The page text itself is never inlined —
    it is the heaviest section and lives on disk only.
    """
    planner_steps = len([
        s for s in steps_log
        if s["type"] == "tool_call"
        and s.get("subagent_label") == "Planner"
    ])

    if "status: success" in final_status:
        status_label = "success"
    elif "status: failure" in final_status:
        status_label = "failure"
    else:
        status_label = "failure"

    output_parts: list[str] = [
        f"Status: {status_label}",
        f"Steps taken: {planner_steps}",
        f"URL: {page_content.get('url', '(unknown)')}",
        f"Page title: {page_content.get('title', '(unknown)')}",
    ]
    if dump_path is not None:
        output_parts.append(
            f"Page content saved at: {dump_path} — use Grep and Read "
            "on this file to find specific text on the page."
        )

    if status_label == "failure":
        output_parts.append(f"Reason: {final_status}")
        if failure_reason is not None:
            output_parts.append(f"failure_reason: {failure_reason.value}")
        reached_url = page_content.get("url", "")
        if reached_url:
            if failure_reason in (
                FailureReason.ELEMENT_NOT_IN_TREE,
                FailureReason.ELEMENT_AMBIGUOUS,
            ):
                output_parts.append(
                    'Hint: try scrolling to reveal more elements, '
                    'rephrasing the target description, or starting '
                    'from a different URL. If vision mode is OFF and '
                    'the element is visible on screen but not in the '
                    'accessibility tree, the user can enable vision '
                    'mode from the UI to retry with screenshot + '
                    'grounding.'
                )
            else:
                output_parts.append(
                    'Hint: try a different approach — use a direct URL '
                    'from the links below, extract page text via Grep '
                    'on the saved file, or try a different page.'
                )

    # Compact summary of what the planner tried.
    action_summary: list[str] = []
    for s in steps_log:
        if s.get("type") == "tool_call" and s.get("subagent_label") == "Planner":
            try:
                args = json.loads(s.get("content", "{}"))
                act = args.get("action", "?")
                tgt = args.get("target", args.get("url", args.get("query", "")))
                if tgt:
                    action_summary.append(f"  {act}: {str(tgt)[:80]}")
                else:
                    action_summary.append(f"  {act}")
            except json.JSONDecodeError:
                pass
    if action_summary:
        output_parts.append("")
        output_parts.append("Actions attempted:")
        output_parts.extend(action_summary)

    if last_thinking:
        output_parts.append("")
        output_parts.append(
            f"Navigator observations: {last_thinking}"
        )

    if not short_mode:
        interactive = page_content.get("interactive_elements", [])
        if interactive:
            output_parts.append("")
            output_parts.append("--- Interactive elements on page ---")

            # Split into per-layer segments at separators, dedupe each
            # segment, then flatten back into one list. Two identical
            # rows in the same layer collapse to one row with a (×N)
            # suffix; identical rows in different layers stay distinct.
            segments: list[tuple[str | None, list[str]]] = []
            cur_sep: str | None = None
            cur_items: list[str] = []
            for elem_desc in interactive:
                if _is_layer_separator(elem_desc):
                    segments.append((cur_sep, cur_items))
                    cur_sep = elem_desc
                    cur_items = []
                else:
                    cur_items.append(elem_desc)
            segments.append((cur_sep, cur_items))

            flat_lines: list[str] = []
            for sep, items in segments:
                if sep is not None:
                    flat_lines.append(sep)
                for desc, count in _dedupe_with_counts(items, lambda s: s):
                    flat_lines.append(_with_count_suffix(desc, count))

            total_unique = sum(
                1 for line in flat_lines if not _is_layer_separator(line)
            )

            # Cap on unique element rows; keep separator headers.
            shown = 0
            for line in flat_lines:
                is_separator = _is_layer_separator(line)
                if not is_separator and shown >= AGENT_INTERACTIVE_CAP:
                    output_parts.append(
                        f"  ... [{total_unique - shown} more elements truncated]"
                    )
                    break
                output_parts.append(f"  {line}")
                if not is_separator:
                    shown += 1

        links = page_content.get("links", [])
        if links:
            output_parts.append("")
            output_parts.append("--- Links on page ---")
            deduped_links = _dedupe_with_counts(
                links,
                lambda link: (link.get("text", ""), link.get("href", "")),
            )
            for link, count in deduped_links[:AGENT_LINKS_CAP]:
                text = link.get("text", "")
                href = link.get("href", "")
                if text and href:
                    output_parts.append(
                        _with_count_suffix(f"  [{text}]({href})", count)
                    )
            if len(deduped_links) > AGENT_LINKS_CAP:
                output_parts.append(
                    f"  ... [{len(deduped_links) - AGENT_LINKS_CAP} more links truncated]"
                )

        iframes = page_content.get("iframes", [])
        for iframe_data in iframes:
            iframe_url = iframe_data.get("url", "")
            iframe_text = iframe_data.get("text", "")
            iframe_links = iframe_data.get("links", [])
            if iframe_text or iframe_links:
                output_parts.append("")
                output_parts.append(
                    f"--- Iframe: {iframe_url} ---"
                )
                if iframe_text:
                    if len(iframe_text) > AGENT_IFRAME_TEXT_CAP:
                        output_parts.append(
                            iframe_text[:AGENT_IFRAME_TEXT_CAP]
                            + "\n... [iframe text truncated]"
                        )
                    else:
                        output_parts.append(iframe_text)
                deduped_iframe_links = _dedupe_with_counts(
                    iframe_links,
                    lambda link: (link.get("text", ""), link.get("href", "")),
                )
                for link, count in deduped_iframe_links[:AGENT_IFRAME_LINKS_CAP]:
                    lt = link.get("text", "")
                    lh = link.get("href", "")
                    if lt and lh:
                        output_parts.append(
                            _with_count_suffix(f"  [{lt}]({lh})", count)
                        )
                if len(deduped_iframe_links) > AGENT_IFRAME_LINKS_CAP:
                    output_parts.append(
                        f"  ... [{len(deduped_iframe_links) - AGENT_IFRAME_LINKS_CAP} "
                        "more iframe links truncated]"
                    )

    return truncate_output(
        "\n".join(output_parts),
        max_lines=1000,
        max_bytes=100000,
    )


# ---------------------------------------------------------------------------
# visit_url / web_search executor
# ---------------------------------------------------------------------------

async def run_goto_intent(
    context: Any,
    page: Any,
    url: str,
    ctx: "EventContext",
    project_dir: str = ".",
) -> tuple[Any, ToolResult]:
    """Execute the ``visit_url`` intent.

    Bypasses the planner entirely: navigate to *url*, wait for settle
    (including a short ``networkidle`` window so SPA hydration finishes
    before extraction), dump the page content, and persist the visible
    page text to a markdown file under
    ``ConfigPaths.project_runtime_dir(project_dir) / "navigation"`` so
    the agent can ``Grep``/``Read`` it on demand. Returns
    ``(page, ToolResult)`` — callers must rebind ``page`` because
    ``navigate_initial_url`` may open a fresh tab on detached-frame
    recovery. The agent-facing output always includes interactive
    elements, links and iframes (capped); the page text never appears
    in-line.

    Also used by ``run_web_search_intent`` (which builds a Bing search
    URL and delegates here) so the search-result page goes through the
    same settle / dump / persist pipeline as a direct URL visit.
    """
    # 1. Navigate (reuses helper with detached-frame recovery).
    try:
        page = await navigate_initial_url(context, page, url)
    except Exception as e:
        # Navigation failure: stay short, surface the error.
        return page, ToolResult(
            output=format_tool_output(
                final_status=(
                    f"Task terminated with status: failure — "
                    f"navigation failed: {e}"
                ),
                last_thinking=f"Failed to load {url}",
                steps_log=ctx.steps_log,
                page_content={"url": url, "title": "(unknown)"},
                failure_reason=FailureReason.PAGE_LOAD_TIMEOUT,
                short_mode=True,
            ),
            metadata={
                "subagent_steps": ctx.steps_log,
                "failure_reason": FailureReason.PAGE_LOAD_TIMEOUT.value,
            },
        )

    # 2. Settle so SPA hydration completes before extraction.
    await settle_after_navigate(page)

    # 3. Build page_content. Always fill url + title (cheap).
    page_content: dict[str, Any] = {}
    try:
        page_content = await dump_page_content(page)
    except Exception as e:
        log.warning("Page content dump failed: %s", e)
        page_content = {}
    if not page_content.get("url"):
        try:
            page_content["url"] = page.url
        except Exception:
            page_content["url"] = url
    if not page_content.get("title"):
        try:
            page_content["title"] = await page.title()
        except Exception:
            page_content["title"] = "(unknown)"

    dump_path = (
        write_navigation_dump(project_dir, page_content)
        if page_content else None
    )

    return page, ToolResult(
        output=format_tool_output(
            final_status="Task terminated with status: success",
            last_thinking=f"Page loaded directly via url parameter: {url}",
            steps_log=ctx.steps_log,
            page_content=page_content,
            failure_reason=None,
            short_mode=False,
            dump_path=dump_path,
        ),
        metadata={
            "subagent_steps": ctx.steps_log,
            "failure_reason": None,
        },
    )


async def run_web_search_intent(
    context: Any,
    page: Any,
    query: str,
    ctx: "EventContext",
    project_dir: str = ".",
) -> tuple[Any, ToolResult]:
    """Execute the ``web_search`` intent.

    URL-encodes *query*, builds a Bing search URL, and delegates to
    ``run_goto_intent`` so the search-result page goes through the same
    settle / dump / persist pipeline as a direct URL visit. Returns the
    same ``(page, ToolResult)`` shape.
    """
    bing_url = (
        f"https://www.bing.com/search?q={quote_plus(query)}&FORM=QBLH"
    )
    return await run_goto_intent(
        context, page, bing_url, ctx=ctx, project_dir=project_dir,
    )
