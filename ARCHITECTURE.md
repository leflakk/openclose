# Architecture — OpenClose

## Overview

OpenClose is a local-first Python AI coding assistant and automation platform. It uses OpenAI-compatible APIs (vLLM, llama.cpp, Ollama, OpenAI) for LLM calls, Chrome DevTools Protocol (CDP) for browser control and recording, and stores everything — sessions, messages, skills, jobs, task recordings — locally (SQLite for conversational state; markdown + JSON on disk for skills, jobs, and tasks). The UI is a FastAPI + Jinja2 HTML app.

## Package Structure

```
src/openclose/
├── __init__.py          # Version
├── __main__.py          # python -m openclose entry
├── id.py                # ULID-based ID generation
├── flag.py              # Feature flags from environment
├── log.py               # Structured logging (rich)
├── debug.py             # LLM request debug dumper
│
├── config/              # Multi-layer config (TOML + env + defaults)
│   ├── config.py        # Load/merge/reload
│   ├── paths.py         # Platform-specific directories
│   ├── schema.py        # Pydantic v2 settings models
│   └── agents.py        # Agent definitions + inheritance resolution
│
├── storage/             # SQLite persistence
│   ├── db.py            # Engine + Database class
│   ├── schema.py        # SQLModel tables (Session, Message, MessagePart, Project)
│   └── migrations.py    # Schema versioning
│
├── bus/                 # Async event bus (pub/sub)
│   └── bus.py
│
├── provider/            # OpenAI-compatible LLM provider
│   ├── provider.py      # AsyncOpenAI wrapper, streaming
│   ├── models.py        # Model registry
│   └── auth.py          # API key resolution
│
├── agent/               # Agent definitions and orchestration
│   ├── agent.py         # Build/Plan/Delegate agents (locked tool sets) + custom agents
│   ├── prompt.py        # System prompt assembly (two-layer: common + agent-specific)
│   └── loop.py          # Main agent loop (stream → tool calls → repeat)
│
├── session/             # Conversation management
│   ├── session.py       # CRUD operations
│   ├── message.py       # Role/PartType enums
│   ├── processor.py     # Orchestrates agent loop + persistence
│   ├── compaction.py    # Context window management
│   ├── prompt.py        # Message history builder
│   └── cancel.py        # Session cancellation
│
├── tool/                # Tool system
│   ├── tool.py          # Tool base class, OpenAI schema generation
│   ├── registry.py      # Tool registry + executor
│   ├── truncation.py    # Output truncation
│   └── tools/           # Built-in tools (14)
│       ├── read.py, write.py, edit.py, multiedit.py  # File operations
│       ├── glob.py, grep.py                          # Search
│       ├── bash.py                                   # Shell execution
│       ├── webfetch.py                               # HTTP fetch
│       ├── plan.py, plan_broker.py                   # Plan tool with phase=draft (read-only reviewer sub-agent) and phase=final (async user-review broker, plan agent only)
│       ├── ask_user.py, ask_user_broker.py           # Interactive user-input tool + async broker
│       ├── delegate.py                               # Delegate sub-agent (read-only investigation with configurable budget)
│       ├── browser_automation_dom.py                 # Accessibility-tree browser tool (fast path)
│       ├── browser_automation_vision.py              # Screenshot + grounding browser tool (escalation)
│       ├── browser_automation_shared.py              # Single lock, CDP connect, snapshot, executor, utils
│       └── deliver_message/                          # Telegram + Discord messaging (sub-package)
│           ├── tool.py, config.py, splitter.py
│           └── telegram.py, discord.py
│
├── permission/          # Tool access control
│   ├── permission.py    # Evaluation engine
│   ├── rules.py         # Allow/deny/ask rules
│   ├── schema.py        # Request/response types
│   ├── broker.py        # Async permission request broker
│   └── extract.py       # Path extraction + sandbox checking
│
├── skills/              # Distilled, re-runnable procedures (markdown + YAML frontmatter)
│   ├── schema.py        # SkillForm / Skill / Parameter / RequiredTool Pydantic models
│   ├── builder.py       # LLM-backed builder: session history → SkillForm
│   ├── runner.py        # Headless AgentLoop per skill; writes .jsonl + .out.md artifacts
│   └── storage.py       # Read/write skill .md; slug reservation; run listing
│
├── jobs/                # Scheduled triggers that chain skills in series
│   ├── schema.py        # JobConfig / JobTiming / JobNotification / run-summary models
│   ├── scheduler.py     # Single background task, 20s tick, per-job asyncio.Lock, cron + one-shot
│   ├── runner.py        # Execute one job run: chain skills, write summary.json, notify
│   ├── cron_nl.py       # Natural-language → cron translation + next-occurrences helper
│   ├── notify.py        # Post-run Telegram/Discord notification using deliver_message aliases
│   └── storage.py       # Read/write job .json; list runs; summary.json read/write
│
├── recorder/            # Browser task recorder (CDP video + events → VLM-annotated task)
│   ├── recorder.py      # Start/stop/cancel session; chunked annotate → task
│   ├── events.py        # CDP event taps (navigate, click, type, paste, ...) → EventLog
│   ├── screencast.py    # CDP screencast frames → ffmpeg-encoded MP4
│   ├── chunker.py       # Plan overlapping time windows (one window for short recordings)
│   ├── chunk_annotator.py  # VLM call per chunk
│   ├── merger.py        # LLM pass that merges per-chunk procedures into one
│   ├── task_builder.py  # Second LLM pass: separate constants / runtime observations
│   └── storage.py       # Task .md write/read; artifacts subdir; slug reservation
│
├── file/                # File system utilities
│   ├── binary.py        # Binary file detection
│   ├── ignore.py        # .gitignore pattern handling
│   ├── diff.py          # Change tracking
│   └── watcher.py       # File watching (watchfiles)
│
├── format/              # Auto-formatters (ruff, black, gofmt, etc.)
│   └── formatter.py
│
├── project/             # Project/VCS management
│   ├── project.py       # Detection and metadata
│   ├── worktree.py      # Git worktree management
│   └── snapshot.py      # Git GC and snapshots
│
├── patch/               # Unified diff engine
│   └── patch.py
│
├── scheduler/           # Background task scheduling (generic — used by session/processor)
│   └── scheduler.py
│
├── server/              # FastAPI + HTML UI
│   ├── app.py           # FastAPI app creation, lifespan (starts JobScheduler)
│   ├── routes.py        # API + HTML routes (sessions, skills, jobs, recorder, tasks)
│   ├── sse.py           # Server-Sent Events streaming
│   └── templates/       # Jinja2 HTML templates + static assets
│
├── cli/                 # Command-line interface
│   └── cli.py           # serve, run -p, sessions commands
│
└── util/                # Shared utilities
    ├── process.py       # Async subprocess
    ├── git.py           # Git command wrapper
    └── fs.py            # File system helpers
```

## Key Design Decisions

### 1. OpenAI-compatible API only
**Decision:** Use the `openai` Python SDK targeting any OpenAI-compatible endpoint.
**Why:** Covers vLLM, llama.cpp, Ollama, and OpenAI with a single client. No need for 75+ provider adapters.

### 2. Local-only, no cloud
**Decision:** All data in local SQLite. No sharing, no remote auth, no cloud infra.
**Why:** User requirement for data sovereignty and simplicity.

### 3. HTML UI via FastAPI (not TUI)
**Decision:** Replace the original Bubble Tea TUI with a simple HTML UI served by FastAPI + Jinja2 + vanilla JS.
**Why:** Easier to customize and modify than a terminal UI framework. SSE provides real-time streaming.

### 4. Synchronous SQLite via SQLModel
**Decision:** Use SQLModel (SQLAlchemy under the hood) with synchronous access, not async.
**Why:** SQLite is inherently single-writer. The overhead of async wrappers adds complexity without benefit for local-only usage. The main async work is in LLM streaming and tool execution.

### 5. dict[str, Any] for LLM messages
**Decision:** Use plain dicts internally for message passing to/from the LLM, not OpenAI typed dicts.
**Why:** The OpenAI type system for `ChatCompletionMessageParam` is a complex union that creates unnecessary friction with mypy. We cast at the boundary when calling the API.

### 6. Tool system with function callbacks
**Decision:** Tools are defined as `Tool` objects with `ToolParameter` metadata and an async execute function.
**Why:** Simple, composable, and generates OpenAI function-calling schemas automatically. No metaclass magic.

### 7. Permission engine with last-match-wins
**Decision:** Permission rules are evaluated in order; last matching rule determines the action.
**Why:** Matches the OpenCode original and allows general rules early with specific overrides later.

### 8. Agent loop as iterator
**Decision:** `AgentLoop.run()` is an async iterator yielding `StreamEvent` objects.
**Why:** Enables both SSE streaming (server) and direct consumption (CLI). The caller decides how to handle events.

### 9. Skills as markdown + YAML frontmatter
**Decision:** Skills persist as hand-authored `<slug>.md` files with a flat YAML frontmatter and five fixed `# Heading` body sections; no database schema, no PyYAML dependency.
**Why:** The feature is still evolving, skills are small, and users want to open a file and see / edit the whole skill in one pane. Markdown is portable (share a skill by copying a file), greppable, and diff-friendly. A hand-rolled frontmatter parser covers our narrow schema without pulling in PyYAML.

### 10. Jobs scheduler re-reads disk every tick; never runs late
**Decision:** `JobScheduler` is one background async task with a 20 s tick. Each tick calls `list_jobs()` — no in-memory cache. Missed cron fires are skipped (`croniter.get_next(now)`, strict forward scheduling). One-shot jobs past due at startup are disarmed (`executed=true`) rather than run late. Per-job `asyncio.Lock` drops a fire if a run is still in progress.
**Why:** Editing a job on disk should "just work" with no reload call. Missed fires from a hibernation or stopped server should not cause a catch-up storm. A per-job lock is simpler than a global queue and is safe because jobs are coarse-grained.

### 11. Browser automation two-layer (DOM → Vision)
**Decision:** `browser_automation_dom` (accessibility tree, fast, text-only) is the preferred tool. It fails cleanly with a structured `failure_reason` (e.g. `element_not_in_tree`, `element_ambiguous`) when the tree can't answer. The main agent then escalates to `browser_automation_vision` (screenshots + a visual-grounding model at `localhost:5002/v1` that returns pixel coordinates). Both tools share one `BROWSER_AUTOMATION_LOCK` so they never interleave, and both connect to the same Chromium over CDP at `localhost:9222`.
**Why:** DOM is 10× faster and cheaper when it works. Vision is the escape hatch for iframes, canvas, and shadow-DOM nightmares. A single lock keeps CDP state coherent; a single CDP endpoint keeps the operator's mental model small.

## Data Flow

### Interactive session

```
User Input
  → CLI (-p flag) or HTML UI (POST /api/sessions/{id}/messages)
    → SessionProcessor
      → Persists user message to SQLite
      → Creates AgentLoop with tool schemas
      → AgentLoop streams from Provider (OpenAI-compatible)
        → Text chunks → StreamEvent("text")
        → Tool calls → ToolRegistry.execute() → StreamEvent("tool_result")
        → Loop continues until done or max_steps
      → Persists assistant response to SQLite
    → Response streamed via SSE (server) or printed (CLI)
```

### Scheduled job

```
JobScheduler tick (every 20s, inside `openclose serve`)
  → list_jobs() from disk
  → for each enabled, due job:
    → acquire per-job asyncio.Lock
    → run_job(JobConfig)
      → for each skill slug in job.skills:
        → read_skill(slug)
        → execute_skill_to_files(skill, jsonl, out)
          → headless AgentLoop, allowed_tools = skill.required_tools,
            permissions pre-ALLOW for those tools, no permission/plan/ask_user brokers
        → write per-skill .jsonl event log + .out.md final text
        → update summary.json
      → if should_notify: send_job_notification(alias, text)
    → on completion: advance state (one-shot → executed=true; recurring → next fire)
```

### Recorder → task

```
User clicks Record (POST /api/recorder/start)
  → connect_browser() via CDP @ localhost:9222
  → start Screencast (frames) + EventLog (navigate/click/type/paste/...)
  → user performs task in browser
User clicks Stop (POST /api/recorder/stop)
  → encode frames → MP4 via ffmpeg; flush events → JSONL
User fills name/description, clicks Save (POST /api/recorder/annotate)
  → plan_chunks → parallel annotate_chunk (VLM) → merge_chunk_procedures
  → write raw procedure.md
  → build_intelligent_task() — second LLM pass distinguishing constants / runtime observations
  → write Task .md with YAML frontmatter to recordings/<slug>.md
  → artifacts (mp4, events.json, procedure.md, task_builder_raw.md, chunks/) stay under recordings/artifacts/
```

## Configuration Priority

1. Environment variables (`OPENCLOSE_*`)
2. Project config (`.openclose/config.toml`)
3. User config (`config.toml` in `ConfigPaths.config_dir()` — Linux: `~/.config/openclose`, macOS: `~/Library/Application Support/openclose`, Windows: `%APPDATA%\openclose`)
4. Defaults (Pydantic model defaults)

## Dependencies

| Package | Purpose |
|---|---|
| pydantic / pydantic-settings | Config and data models |
| sqlmodel | SQLite ORM |
| openai | LLM API client |
| tiktoken | Token counting |
| fastapi / uvicorn / jinja2 | HTML UI server |
| sse-starlette | Server-Sent Events |
| httpx | HTTP client (webfetch, messaging, notifications) |
| beautifulsoup4 | HTML parsing (webfetch tool) |
| rich | Terminal output and logging |
| watchfiles | File system monitoring |
| pathspec | Gitignore pattern matching |
| python-ulid | ID generation |
| aiosqlite | Async SQLite adapter |
| aiofiles | Async file I/O |
| playwright | Browser driver for `browser_automation_*` and recorder (CDP attach) |
| Pillow | Image handling for recorder frames and vision tool screenshots |
| croniter | 5-field cron expression parsing and "next fire" computation (Jobs scheduler) |
| python-dotenv | Loads `.env` from `ConfigPaths.config_dir()` for messaging credentials and channel aliases |
