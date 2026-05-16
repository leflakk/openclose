# Browser Automation Guide

## Overview

OpenClose ships two browser-automation tools that share one Chromium over CDP, one process-wide lock, and one set of post-action wait heuristics:

| Tool | How it sees the page | When it runs |
|---|---|---|
| `browser_automation_dom` | Chrome accessibility tree — text only, no pixels | **Default.** The agent reaches for this first. |
| `browser_automation_vision` | Screenshots + a visual grounding model that returns pixel coordinates | Escalation path when DOM fails with `element_not_in_tree` / `element_ambiguous`, or when the session's **Vision Mode** toggle is on. |

Both tools speak the same action schema and run inside a bounded sub-loop (`MAX_STEPS = 5`, `TIME_LIMIT_S = 300`). Both use the same viewport (1440 × 900). Only one browser automation call — of either kind — runs at a time, because both acquire `BROWSER_AUTOMATION_LOCK` for their whole execution.

## Prerequisites

### Chrome with CDP

Both tools connect to `http://127.0.0.1:9222`.

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

### Grounding model (vision tool only)

The vision tool expects an OpenAI-compatible endpoint at `http://localhost:5002/v1` serving a visual-grounding model that accepts a screenshot + a natural-language element description and returns pixel coordinates. Any model following the `FN_CALL_TEMPLATE` schema in `browser_automation_vision.py::_build_tool_schema` works; the project is developed against a local Qwen-VL-class grounding model.

Key constants in `browser_automation_vision.py`:

```python
_GROUNDING_BASE_URL = "http://localhost:5002/v1"
_GROUNDING_MODEL = "local"
_GROUNDING_MAX_ATTEMPTS = 3
```

If the grounding server is down, vision calls raise and the agent falls back to whatever the next action is. The DOM tool does not depend on the grounding server.

## Two-layer design

```
Agent main loop
 └─ tool_call: browser_automation_dom({intent, url?})
     ├─ snapshot_a11y → planner LLM (text-only, sees element indices [N])
     ├─ parse_model_response → single action
     ├─ resolve_element_index (DOM) → execute_action (Playwright)
     ├─ wait_after_action tuned to action type
     └─ terminate or loop (max 5 steps)

Agent main loop, on DOM failure
 └─ tool_call: browser_automation_vision({intent, url?})
     ├─ screenshot → planner LLM (vision, sees the pixels)
     ├─ parse_model_response → action + target description
     ├─ grounding model → pixel coordinates (resize-aware)
     ├─ execute_action (Playwright, by coords)
     ├─ wait_after_action tuned to action type
     └─ terminate or loop (max 5 steps)
```

### Why DOM first

- **Fast.** Text snapshots are ~1 KB; screenshots are hundreds of KB.
- **Cheap.** No vision tokens, no grounding model round-trip.
- **Deterministic.** Index-based element targeting is stable across turns in a way that "click the blue button" is not.
- **Readable logs.** The planner's snapshot is human-auditable.

### Why vision exists

- **Iframes with cross-origin docs** don't appear in the top-level accessibility tree.
- **Canvas apps** (maps, editors, games) have no a11y tree to speak of.
- **Shadow DOM without role exposure** (custom elements that forget to set ARIA attributes).
- **Ambiguous labels** — several elements called "Submit" and no unique disambiguator.

### Failure escalation

When the DOM tool can't resolve an element it emits a structured failure:

```json
{
  "ok": false,
  "failure_reason": "element_not_in_tree",
  "summary": "Target 'Open chat window' not present in current accessibility snapshot; may be inside a canvas or cross-origin iframe."
}
```

Agents are prompted (in their system prompt) to inspect `failure_reason` and, if it's `element_not_in_tree` or `element_ambiguous`, re-issue the request against `browser_automation_vision`. Other failures (`timeout`, `navigation_blocked`, `invalid_intent`) are not escalated — they indicate a bug in the intent, not a tool mismatch.

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

Both modes (DOM / rich) accept the same planner actions. DOM mode supports the `element_index` form of targeted actions; rich mode additionally accepts `target` (a natural-language description the grounding model resolves).

### Element-targeted actions

```json
{"action": "left_click", "element_index": 7}
{"action": "mouse_move", "element_index": 3}
{"action": "type", "element_index": 2, "text": "hello",
 "press_enter": true, "delete_existing_text": false}
{"action": "scroll", "element_index": 12, "pixels": -500}
```

### Target-by-description actions (rich mode only)

```json
{"action": "left_click", "target": "the blue Submit button at the bottom of the form"}
{"action": "type", "target": "the search box in the top bar", "text": "openclose"}
```

### Direct actions

```json
{"action": "history_back"}
{"action": "scroll", "pixels": -500}                                   // whole page
{"action": "type", "text": "no element_index — whatever is focused"}
{"action": "key", "keys": ["Enter", "ArrowDown"]}
{"action": "wait", "time": 5}                                          // max 10
{"action": "pause_and_memorize_fact", "fact": "User id is 12345"}
{"action": "terminate", "status": "success", "summary": "..."}
```

The planner _cannot_ load a new URL or run a web search on its own. If it needs a different page, it `terminate`s and the main agent re-issues `visit_url` / `web_search`.

### Post-action waits

`browser_automation_shared.wait_after_action` adjusts the settle time to the action family:

| Family | Actions | Wait |
|---|---|---|
| Full-navigation | `history_back` | `load` (timeout 10 s) + `networkidle` (timeout 3 s), both swallowed on timeout |
| Possibly-nav | `left_click`, `key` | `networkidle` (timeout 1.5 s), swallowed |
| Local | everything else | 300 ms sleep |

Long-polling / websocket sites never reach `networkidle`; that's why the waits are all time-bounded — the loop never stalls.

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

Use cases for enabling rich mode:

- The target site is DOM-hostile (heavy canvas, deep iframes).
- You're running a skill built from a recording where the task builder used visual targets.
- You're debugging why a DOM run keeps escalating.

## Limits

| Constant | Value | File |
|---|---|---|
| `MAX_STEPS` | 5 | `browser_automation_shared.py` |
| `TIME_LIMIT_S` | 300 (5 min) | `browser_automation_shared.py` |
| `VIEWPORT_WIDTH` × `HEIGHT` | 1440 × 900 | `browser_automation_shared.py` |
| `MAX_SNAPSHOT_ELEMENTS` (DOM) | 150 | `browser_automation_dom.py` |
| `_SNAPSHOT_PAGE_TEXT_CHARS` (DOM) | 2000 | `browser_automation_dom.py` |
| `_RECENT_ACTIONS_N` (DOM) | 5 | `browser_automation_dom.py` |
| `_RECENT_ACTIONS_N` (Vision) | 8 | `browser_automation_vision.py` |
| `_VLM_CONCURRENCY` (recorder chunks) | 4 | `recorder.py` |

Five steps per invocation is strict. If a task needs more, design it as several agent turns — the main agent can call the tool again.

## Debugging

### See what the planner saw

With `OPENCLOSE_DEBUG_LLM=1`, every LLM call the tool makes (planner, grounding, merger) is appended to `<config_dir>/<project_name>/llm_debug.jsonl`. The `source` field distinguishes `browser_automation_dom.planner`, `browser_automation_vision.planner`, `browser_automation_vision.grounding`, etc.

### Replay an action manually

The tool's `execute_action` is the single funnel. For a one-off debug, start a Python REPL with an active `openclose serve`, connect to CDP, and call the function directly — no need to re-stream an LLM.

### Common failure reasons

- `element_not_in_tree` — target isn't in the a11y snapshot. Escalate to vision.
- `element_ambiguous` — multiple matches with the same label; disambiguate the intent or escalate to vision.
- `timeout` — the action itself timed out (navigation never completed within 10 s). Likely a site problem; retry with a clearer intent or manual `wait`.
- `navigation_blocked` — Chromium refused the navigation (e.g. downloadable file, `javascript:` URL).
- `invalid_intent` — the planner's first turn returned non-JSON or a schema-violating action.

## API reference

The tools are called via the normal tool-calling flow; there's no HTTP endpoint for "run a browser action" directly. To invoke them:

- **From chat**: the agent decides. Write an intent like "log in to example.com with the credentials in `$creds` and submit the form"; the agent will pick a tool.
- **From a skill**: list `browser_automation_dom` (and optionally `browser_automation_vision`) under `required_tools` in the skill's frontmatter. The skill runner pre-grants ALLOW permission for those tool names; the agent calls them in the normal way during the headless run.
- **From a job**: include such a skill in the job's `skills` list.

Related routes (session-level):

| Endpoint | Purpose |
|---|---|
| `GET /api/sessions/{session_id}/video-compatible` | Read the Video Compatible Model flag (gates the Record button only — independent of grounding). |
| `POST /api/sessions/{session_id}/video-compatible` | Toggle the flag. |
