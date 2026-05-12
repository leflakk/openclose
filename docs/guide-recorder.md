# Recorder & Tasks Guide

## Overview

The recorder captures a **manual** browser session — you driving the keyboard and mouse, no agent — and distils it into a structured task definition the agent can later re-perform. It does **not** directly produce a skill: the intended flow is

```
record → task (VLM + task-builder LLM) → open a new session → agent performs the task → save as skill → schedule as job
```

Why two passes? The raw VLM procedure describes *what happened* literally. The second pass (task_builder) reshapes that into *what should happen on every future run*, cleanly separating:

- **Constants** baked in from the recording (your email address, the destination URL, a prompt template you use verbatim)
- **Runtime observations** that change every run (today's top posts, the latest prices, the current headlines)
- **Example observations** — the actual values seen during recording, kept as frozen-time evidence

A task file is a markdown document with YAML frontmatter. It's a *reference* — it isn't executed directly. You hand it to the agent in a new session; the agent uses it as the specification.

## Prerequisite: CDP browser

The recorder, the DOM tool, and the vision tool all connect to a single Chromium (or Chrome) instance over Chrome DevTools Protocol at `http://localhost:9222`. Launch one before starting `openclose serve`.

**Linux**

```bash
chromium \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.cache/cdp-profile" \
  --disable-blink-features=AutomationControlled
```

Replace `chromium` with `google-chrome` if that's your Chrome binary.

**macOS**

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Caches/openclose-cdp-profile" \
  --disable-blink-features=AutomationControlled
```

**Windows (PowerShell)**

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\openclose-cdp-profile" `
  --disable-blink-features=AutomationControlled
```

Notes:

- The `--user-data-dir` should be a dedicated profile — don't reuse your default profile or you'll be asked to close it. Any stable path you reuse keeps logins across restarts while staying decoupled from your real Chrome data.
- On Debian/Ubuntu the binary is usually `chromium` or `google-chrome`.
- On Windows, if `chrome.exe` is on PATH you can drop the full path and call `chrome` directly.

The recorder acquires the `BROWSER_AUTOMATION_LOCK` for the whole session, so the agent's `browser_automation_*` tools can't run concurrently.

## Recording workflow

### 1. Start

Sidebar **Recorder** panel → **Record**. `POST /api/recorder/start` does:

1. `connect_browser()` — attaches to `localhost:9222` via Playwright's `connect_over_cdp`.
2. Opens a new CDP session on the current page.
3. Starts a `Screencast` (frames) and an `EventLog` (navigation, clicks, typing, paste events).
4. Captures the screencast's `started_at` monotonic clock — everything else is timestamped relative to this.

### 2. Perform the task

Do the thing. Everything the browser exposes through CDP is captured:

- Page navigations (`navigate` events with `origin: external | in_page_click`)
- Clicks with visible labels and ARIA roles
- Text input (keystrokes aggregated into `type` events)
- Paste events with the target field's label
- Screencast frames at ~display frame rate, throttled

### 3. Stop

Click **Stop**. `POST /api/recorder/stop` does:

1. Detaches CDP listeners.
2. Flushes the screencast through `ffmpeg` into an MP4 (`<recording_id>.mp4`).
3. Writes the events list as `<recording_id>.events.json`.
4. Releases the browser automation lock — annotation uses the provider, not the browser, so there's no reason to hold it while the VLM thinks.
5. Returns `{recording_id, events_count, frames_count, duration_s, video_path, events_path, video_size_bytes}`.

The recording is kept in memory until annotated or cancelled. `POST /api/recorder/cancel` discards it without writing a task.

### 4. Annotate

The UI prompts for **name** and **description**, then calls `POST /api/recorder/annotate`. The pipeline:

#### Pass 1: VLM annotator

Every recording — short or long — runs through the same chunked pipeline:

- `chunker.plan_chunks(duration, window=12s, overlap=2s)` partitions the timeline into overlapping windows. Recordings shorter than one window produce a single window covering the whole recording.
- For each window, `chunker.slice_chunk` produces a per-chunk event list with timestamps re-based to the chunk's `t_start`, and `screencast.encode_frames_to_mp4` encodes the frames in that window as a per-chunk MP4 under `recordings/artifacts/<rec-id>/chunks/<index>.mp4`.
- `chunk_annotator.annotate_chunk` is called on each chunk in parallel (bounded by a `_VLM_CONCURRENCY=4` semaphore). The VLM is prompted to output a numbered literal procedure with each step prefixed by its global timestamp (e.g. `[14.3s]`).
- `merger.merge_chunk_procedures` asks the provider (text-only) to merge the per-chunk procedures into a single coherent procedure, using the global events log as the ground truth for timing. For a single-chunk recording this pass is near a passthrough, but the flow is kept uniform so artifacts and prompts are the same regardless of duration.

The raw (merged) procedure is persisted to `recordings/artifacts/<rec-id>/<rec-id>.procedure.md`.

#### Pass 2: Task builder (text-only)

`task_builder.build_intelligent_task` takes the raw procedure + the events JSON + the user-entered name/description + `recorded_at` and produces the final task body. The system prompt is the canonical place to look for the exact behaviour, but the highlights:

- **Events are authoritative** for exact typed text, URLs navigated, and element labels. When procedure and events disagree, the LLM is told to trust events.
- The LLM is forbidden from inventing steps, URLs, selectors, or destinations. Noise (idle page loads, accidental clicks, bounce-through tabs) is dropped.
- Output is a strict markdown template with six sections, in order:

  - `## Task goal`
  - `## Task constants`
  - `## Task runtime observations`
  - `## Task example observations`
  - `## Task preconditions`
  - `## Task workflow`
  - `## Task success criteria`

Whatever the LLM returns is saved verbatim as the task body — no post-processing, no format repair. The raw response is always dumped to `recordings/artifacts/<rec-id>/<rec-id>.task_builder_raw.md` for debugging.

If the task builder returns an empty response, a fallback body is written using the raw procedure + the user's description, so the recording is never lost.

### 5. Save

`write_task` produces `recordings/<slug>.md`:

```markdown
---
name: Daily Reddit digest
description: Grab the top 5 posts from r/MachineLearning
recorded_at: 2026-04-22T09:00:00+00:00
recording_id: daily-reddit-digest_20260422_090000
---

## Task goal
Summarise the top 5 posts from r/MachineLearning and email them to $destination_email.

## Task constants
- source_url: https://www.reddit.com/r/MachineLearning/top/?t=day
- destination_email: me@example.com

## Task runtime observations
- The top 5 posts on source_url — titles, authors, and scores — as seen at run time.

## Task example observations
- "Attention is all you need, again" — u/alice — 342 upvotes
- "Benchmarking Llama 4 on M3 Max" — u/bob — 289 upvotes
- ...

## Task preconditions
- The browser is signed in to Reddit (optional).

## Task workflow
1. Navigate to source_url.
2. Read the top 5 post cards (heading, author, score).
3. Compose an email to destination_email with one bullet per post.
4. Send.

## Task success criteria
- The email is sent (toast "Message sent" is visible).
```

The slug is reserved at save time via `reserve_task_slug(name)` — collisions are auto-numbered (`daily-reddit-digest`, `daily-reddit-digest-2`, …).

All artifacts for the recording are renamed to share the slug prefix: `<slug>_<iso_date_time>.{mp4,events.json,procedure.md,task_builder_raw.md}` under `recordings/artifacts/<slug>_<iso_date_time>/`. The `chunks/` subdirectory (if any) keeps its files in that new folder.

## On-disk layout

```
<config_dir>/<project>/recordings/               # config_dir varies per OS (see README)
├── <slug>.md                                    # the task
└── artifacts/
    └── <slug>_<iso_date_time>/
        ├── <slug>_<iso_date_time>.mp4           # raw screencast
        ├── <slug>_<iso_date_time>.events.json   # raw CDP event log
        ├── <slug>_<iso_date_time>.procedure.md  # raw VLM procedure
        ├── <slug>_<iso_date_time>.task_builder_raw.md  # raw second-pass LLM reply
        └── chunks/                              # one subfile set per chunk; short recordings produce a single 000.*
            ├── 000.mp4
            ├── 000.events.json
            ├── 000.procedure.md
            ├── 001.mp4
            └── ...
```

The artifacts are kept deliberately — if the annotator misreads the video, you can re-run the second pass by hand against a different model, or tweak the chunking and re-annotate.

## From a task to a skill

A task is reference material. To actually run it on a schedule:

1. Open a new chat session.
2. Load the task into context (copy the `recordings/<slug>.md` content into the prompt, or ask the agent to `read` it — the file is readable by the `read` tool).
3. Ask the agent to execute the task once. It'll use `browser_automation_dom` / `browser_automation_vision` as needed, plus any other tools.
4. Once it completes correctly, open the Skills panel and **Save as skill**. The skill builder distils the agent's conversation — which now has the task as context plus the actual tool calls — into a parameterised skill.
5. Schedule the skill as a [Job](guide-jobs.md).

This two-step "task then skill" exists so the skill is built from a successful agent run, not from a human demo. The VLM may hallucinate micro-steps; the agent run produces actual tool calls with actual arguments, which is what the skill builder needs.

## Tunables

In `src/openclose/recorder/recorder.py`:

```python
_CHUNK_WINDOW_S = 12.0   # seconds of footage per VLM chunk
_CHUNK_OVERLAP_S = 2.0   # seconds of overlap between adjacent chunks
_VLM_CONCURRENCY = 4     # max parallel VLM calls during chunked annotation
```

Lower the concurrency on slow LLM backends. Increase the window for a cheaper (but less fine-grained) annotation.

## Troubleshooting

**`failed to attach to browser`** — Chromium isn't running on `localhost:9222`, or another Playwright/Puppeteer client grabbed the port. Verify with `curl http://localhost:9222/json/version`.

**"a recording is already in progress"** — `POST /api/recorder/cancel` and try again. There's exactly one recording slot.

**MP4 encoding fails** — `ffmpeg` must be in PATH. The encoder uses it directly; there's no Python fallback.

**Annotator returned empty procedure** — check the raw file at `recordings/artifacts/<rec-id>/<rec-id>.procedure.md`. A tiny or censored response usually means the model didn't accept the video (unsupported codec, too large) or the provider doesn't do vision. Try a different model.

**Task builder returned empty body** — `task_builder_raw.md` will be empty too. `annotate_recording` writes the fallback body (raw procedure + description) so you don't lose the recording; edit the `.md` by hand.

**Events and procedure disagree** — the builder is told to trust events. If the result is still wrong, look at `events.json` — the CDP event taps may have missed something (rare, but happens with custom keyboard handlers).

**The task file contains UI noise ("Loading…", "Click to accept cookies")** — the prompt tries to drop this; if it doesn't, edit the `.md` directly.

## API reference

| Endpoint | Purpose |
|---|---|
| `GET /api/recorder/status` | `{active: {recording_id, started_at, events_count, frames_count}}` or `{active: null}`. |
| `POST /api/recorder/start` | Begin a recording. 409 if one is already in progress. |
| `POST /api/recorder/stop` | Stop and encode. Returns recording metadata. |
| `POST /api/recorder/cancel` | Discard the active recording. |
| `POST /api/recorder/annotate` | Body: `{recording_id, name, description}`. Runs the full annotate pipeline and returns the saved task. |
| `GET /api/tasks` | List all tasks. |
| `GET /api/tasks/{slug}` | Read a task (full body). |
| `DELETE /api/tasks/{slug}` | Delete the task and its artifacts folder. |
