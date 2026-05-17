# Browser Automation Guide

## Overview

OpenClose ships a single `browser_automation` tool that drives Chromium over CDP. The tool runs in one of two internal modes, selected at call time from the user's `config.toml`:

| Mode | How it sees the page | When it activates |
|---|---|---|
| `dom` | Chrome accessibility tree — text only, no pixels | **Default.** Active when `[browser_vision_grounding]` is absent from `config.toml`. |
| `rich` | Accessibility tree **+** screenshot, with a visual grounding model as the fallback resolver for `target` descriptions | Active when `[browser_vision_grounding]` is present in `config.toml`. |

Both modes run inside a bounded sub-loop (`MAX_STEPS = 5`, `TIME_LIMIT_S = 300`), share the same action schema, and use the same viewport (1440 × 900). Only one `browser_automation` call runs at a time — it acquires `BROWSER_AUTOMATION_LOCK` for its whole execution.

## Prerequisites

### Chrome with CDP

Both modes connect to `http://127.0.0.1:9222`.

**Linux / macOS:**

```bash
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/cdp-profile \
  --disable-blink-features=AutomationControlled
```

(On macOS the binary lives at `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.)

**Windows (PowerShell):**

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:TEMP\cdp-profile" `
  --disable-blink-features=AutomationControlled
```

The profile directory is dedicated (don't reuse your main browser profile). `--disable-blink-features=AutomationControlled` hides the "Chrome is being controlled by automated test software" banner some sites use to gate access.

### Grounding model (rich mode only)

Rich mode expects an OpenAI-compatible endpoint serving a visual-grounding model that accepts a screenshot + a natural-language element description and returns pixel coordinates. Any model following the `FN_CALL_TEMPLATE` schema in `browser_automation.py::_build_tool_schema` works; the project is developed against a local Qwen-VL-class grounding model.

The endpoint, model, and API key all come from the `[browser_vision_grounding]` section of `config.toml`. The number of grounding retries per `target` is `_GROUNDING_MAX_ATTEMPTS = 3` (in `browser_automation.py`).

If the grounding server is down, `target`-only actions whose fuzzy a11y match also misses will fail; an observation is fed back into the planner so it can rephrase or fall back to `element_index`. DOM mode does not depend on the grounding server.

## Per-call flow

```
Agent main loop
 └─ tool_call: browser_automation({intent, url?, task?, query?})
     │
     ├─ intent=visit_url  → navigate, settle, dump page content
     ├─ intent=web_search → build Bing URL, then same as visit_url
     │
     └─ intent=act_on_page
         ├─ optional url → navigate_initial_url + settle_after_navigate
         └─ planner loop (max MAX_STEPS = 5 steps, TIME_LIMIT_S = 300s)
             ├─ snapshot_a11y (+ screenshot in rich mode)  ── parallel
             ├─ describe_outcome (silent-fail detector)
             ├─ planner LLM → free-form thinking + ONE <tool_call>
             ├─ parse_model_response → action args
             ├─ loop detection (3 identical or A-B-A-B oscillation)
             ├─ resolve_action_target (pixel actions only):
             │     element_index → fuzzy a11y → grounding LLM (rich) → fail
             ├─ execute_action (Playwright, by coords)
             ├─ handle_tab_switch + recover_page_if_dead
             └─ wait_after_action tuned to action family
```

### Why DOM first

- **Fast.** Text snapshots are ~1 KB; screenshots are hundreds of KB even after smart-resize.
- **Cheap.** No vision tokens, no grounding model round-trip.
- **Deterministic.** Index-based element targeting is stable across turns in a way that "click the blue button" is not.
- **Readable logs.** The planner's snapshot is human-auditable.

### Why rich mode exists

- **Iframes with cross-origin docs** don't appear in the top-level accessibility tree.
- **Canvas apps** (maps, editors, games) have no a11y tree to speak of.
- **Shadow DOM without role exposure** (custom elements that forget to set ARIA attributes).
- **Ambiguous labels** — several elements called "Submit" and no unique disambiguator.

### Failure surface

When element resolution fails, `failure_reason` is surfaced in the tool result metadata for observability. Valid values: `element_not_in_tree`, `element_ambiguous`, `page_load_timeout`, `navigation_loop_detected`, `step_budget_exhausted`, `task_infeasible`. The agent-facing output is a human-readable `Status` / `Reason` / `Hint` block — `failure_reason` is not a routing signal.

## Tool intents (main-agent surface)

`browser_automation` exposes three intents to the main agent. Page loading and web search live here, _not_ in the planner — the planner only acts on a page that is already loaded.

```json
{"intent": "visit_url",    "url": "https://example.com"}
{"intent": "web_search",   "query": "latest openclose release notes"}
{"intent": "act_on_page",  "task": "click the first search result", "url": "https://example.com"}
```

- `visit_url` and `web_search` use the same post-load pipeline (settle → dump → persist `Page content saved at:`); `web_search` simply builds a Bing search URL from `query` first.
- `act_on_page` hands `task` to the planner sub-agent below. `url` is optional — when supplied, it is loaded first.

## Action reference (planner sub-agent, inside `act_on_page`)

Both modes (DOM / rich) accept the same planner actions. DOM mode supports the `element_index` form of targeted actions. Rich mode additionally accepts `target` (a natural-language description resolved via fuzzy a11y match with visual grounding as fallback) — and the rich-mode planner is steered to **default to `target`**, falling back to `element_index` only when a snapshot row is unambiguous. Rich mode is the visual-first mode; if you're in it, expect the planner to describe what it sees.

Every planner action (except `terminate`) must include an `intent` field — a short one-sentence rationale for the action. Missing `intent` is logged and replaced with `"(missing)"` so a turn is not wasted, but the planner system prompt requires it. `terminate` uses `summary` instead.

### Element-targeted actions

```json
{"action": "left_click", "element_index": 7, "intent": "Open the first search result"}
{"action": "mouse_move", "element_index": 3, "intent": "Hover the menu to expand it"}
{"action": "type", "element_index": 2, "text": "hello",
 "press_enter": true, "delete_existing_text": false,
 "intent": "Submit a hello query in the search box"}
{"action": "scroll", "element_index": 12, "pixels": -500, "intent": "Reveal the list below the fold"}
```

### Target-by-description actions (rich mode — preferred form)

In rich mode, the planner is prompted to default to `target` and only reach for `element_index` when a snapshot row is unambiguously the right one. The resolver fuzzy-matches `target` against the a11y snapshot first (free); only on a miss does it call the grounding model.

```json
{"action": "left_click", "target": "the blue Submit button at the bottom of the form", "intent": "Submit the form"}
{"action": "type", "target": "the search box in the top bar", "text": "openclose", "intent": "Search for openclose"}
```

### Direct actions

```json
{"action": "history_back", "intent": "Go back to the previous results page"}
{"action": "scroll", "pixels": -500, "intent": "Scroll down to see more results"}     // whole page
{"action": "type", "text": "no element_index — whatever is focused", "intent": "Type into the currently focused input"}
{"action": "key", "keys": ["Enter", "ArrowDown"], "intent": "Confirm and move to the next item"}
{"action": "wait", "time": 5, "intent": "Wait for results to load"}                   // max 10
{"action": "terminate", "status": "success", "summary": "..."}
```

The planner _cannot_ load a new URL or run a web search on its own. If it needs a different page, it `terminate`s and the main agent re-issues `visit_url` / `web_search`.

### Post-action waits

`browser_automation_shared.wait_after_action` adjusts the settle time to the action family:

| Family | Actions | Wait |
|---|---|---|
| Full-navigation | `history_back` | `load` (timeout 10 s) + `networkidle` (timeout 3 s), both swallowed on timeout |
| Possibly-nav | `left_click`, `key`, `type` | `networkidle` (timeout 1.5 s), swallowed |
| Local | `mouse_move`, `scroll`, `wait` | 300 ms sleep |

`type` sits in the possibly-nav family because typing typically fires autocomplete / search XHR, and `press_enter=True` frequently submits a form — both deserve the same settle window as a click.

Long-polling / websocket sites never reach `networkidle`; that's why the waits are all time-bounded — the loop never stalls.

After `navigate_initial_url` (used by both `visit_url` and the optional `url=` argument of `act_on_page`), the shared `settle_after_navigate` helper waits for `load` + `networkidle` + a short sleep so SPA hydration is mostly done before the first snapshot.

## Grounding endpoint activation (config-driven)

`browser_automation` runs in one of two internal modes selected at execute time from your `config.toml` (in the openclose config directory — see the README's "Where openclose stores files"):

- **`dom` (default)** — accessibility-tree only, no grounding model required. Active when `[browser_vision_grounding]` is **absent** from the config.
- **`rich`** — accessibility-tree + screenshot, with a grounding LLM as the fallback resolver for `target` descriptions that the a11y tree can't disambiguate. Active when `[browser_vision_grounding]` is **present** in the config (any populated section enables it).

Example — enable rich mode by adding the section:

```toml
[browser_vision_grounding]
base_url = "http://localhost:5002/v1"
api_key  = ""
model    = "local"
```

To go back to DOM-only mode, comment out or delete the section and restart the server.

> The session-level **Video Compatible Model** toggle (slash command `/video_compatible`, `POST /api/sessions/{session_id}/video-compatible`) is **unrelated** to grounding. It only gates the **Record** button — set it ON when your main LLM accepts video input so that recorded captures can be annotated.

Enabling rich mode flips the planner's default targeting mode: it will describe elements via `target` and lean on the screenshot + grounding pipeline, instead of picking `element_index` rows from the a11y tree. Turn it on when you want the planner driven by what's on screen rather than by snapshot text. Common cases:

- The target site is DOM-hostile (heavy canvas, deep iframes) and snapshot rows are unreliable.
- You're running a skill built from a recording where the task builder used visual targets.
- You're debugging why a DOM run keeps escalating.
- You generally trust your grounding model and want visual-first behavior across all sites.

## Limits

| Constant | Value | File |
|---|---|---|
| `MAX_STEPS` | 5 (hard cap 15) | `browser_automation_shared.py` |
| `TIME_LIMIT_S` | 300 (5 min) | `browser_automation_shared.py` |
| `VIEWPORT_WIDTH` × `HEIGHT` | 1440 × 900 | `browser_automation_shared.py` |
| `_MAX_SNAPSHOT_ELEMENTS` | 150 | `browser_automation.py` |
| `_SNAPSHOT_PAGE_TEXT_CHARS` | 2000 | `browser_automation.py` |
| `_RECENT_ACTIONS_N` | 5 | `browser_automation.py` |
| `_GROUNDING_MAX_ATTEMPTS` (rich) | 3 | `browser_automation.py` |

Five steps per invocation is the default. If a task needs more, either bump `max_steps` (capped at 15) on the call or design it as several agent turns — the main agent can call the tool again.

## Debugging

### See what the planner saw

With `OPENCLOSE_DEBUG_LLM=1`, every LLM call the tool makes (planner, grounding) is appended to `<config_dir>/<project_name>/llm_debug.jsonl`. The `source` field distinguishes `browser_automation.planner.dom`, `browser_automation.planner.rich`, and `browser_automation.grounding`.

### Replay an action manually

The tool's `execute_action` is the single funnel. For a one-off debug, start a Python REPL with an active `openclose serve`, connect to CDP, and call the function directly — no need to re-stream an LLM.

### Common failure reasons

- `element_not_in_tree` — `element_index` not in current snapshot or `target` not found in the a11y tree (and, in rich mode, the grounding model couldn't locate it either). In dom mode, enable rich mode if the element is visible on screen but not in the a11y tree.
- `element_ambiguous` — multiple a11y rows are similarly good matches for the `target`; rephrase with more specificity (color, position, exact label).
- `page_load_timeout` — `navigate_initial_url` couldn't load the URL within 15 s. Likely a site problem; retry with a clearer URL or after a manual `wait`.
- `navigation_loop_detected` — the planner repeated the same `(action, element_index_or_target)` three times, or oscillated A-B-A-B with no DOM change. The next call should pick a different strategy.
- `step_budget_exhausted` — ran out of `MAX_STEPS` or hit `TIME_LIMIT_S`. Bump `max_steps` (≤15) or split the task across multiple calls.
- `task_infeasible` — the planner itself terminated with `status:"failure"` and a summary; check the summary for what it tried.

## API reference

The tools are called via the normal tool-calling flow; there's no HTTP endpoint for "run a browser action" directly. To invoke them:

- **From chat**: the agent decides. Write an intent like "log in to example.com with the credentials in `$creds` and submit the form"; the agent will pick a tool.
- **From a skill**: list `browser_automation` under `required_tools` in the skill's frontmatter. The skill runner pre-grants ALLOW permission for that tool name; the agent calls it in the normal way during the headless run.
- **From a job**: include such a skill in the job's `skills` list.

Related routes (session-level):

| Endpoint | Purpose |
|---|---|
| `GET /api/sessions/{session_id}/video-compatible` | Read the Video Compatible Model flag (gates the Record button only — independent of grounding). |
| `POST /api/sessions/{session_id}/video-compatible` | Toggle the flag. |
