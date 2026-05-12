# Skills Guide

## Overview

A **skill** is a distilled, re-runnable procedure authored from a real chat session. The flow is always the same:

1. Solve the task once interactively — by yourself in the chat, with the agent, or by letting the agent redo a recorded task.
2. Open the session's **Skills → Save as skill** panel. OpenClose sends the full conversation to an LLM builder, which returns an editable form.
3. Review the form (the human is always in the loop here), save, and from that point the skill can be run manually or scheduled by a [Job](guide-jobs.md).

Skills are plain markdown files with YAML frontmatter. Nothing is hidden inside a database — you can open the file, hand-edit the procedure, and save.

```
<config_dir>/<project>/skills/             # config_dir varies per OS (see README)
├── <slug>.md                              # the skill itself
└── <slug>/
    ├── <iso-ts>-<run_id>.jsonl            # per-run event log
    └── <iso-ts>-<run_id>.out.md           # per-run final assistant text
```

## Creating a skill

### From a chat session

In the session sidebar, open the Skills panel and click **Save as skill**. The builder:

1. Loads every user message, assistant text, and tool call + result from the session.
2. Sends it to the configured provider with a strict prompt that asks for a single JSON object — no prose, no code fences.
3. Separates three kinds of values:
   - **Constants** — values the user wants frozen on every run (a hardcoded email, a target URL, a prompt template). Baked into the Procedure.
   - **Runtime parameters** — values that might change per run. Surfaced as typed `parameters[]` entries with a `default` seen in the history and referenced in the Procedure as `$param_name`.
   - **Live observations** — data that must be read fresh at run time (today's PRs, current prices). Described in the Procedure as "gather from X, N items".
4. Returns a `SkillForm` with the fields below. Review, edit, and save.

### Builder rules

The builder is told explicitly:

- Never invent tools, URLs, selectors, or steps absent from the conversation. `required_tools` must be a subset of tools actually called.
- Prefer the shortest reliable workflow. Drop one-off clarifications and tangents.
- Flag `sensitive: true` for `bash`, `write`, `edit`, `multiedit`, `browser_automation_dom`, `browser_automation_vision`, `deliver_message`. The UI highlights these tools in red so you don't grant them by accident.
- The procedure must read as unattended — no "ask the user", no "check with me". If it needs input, it should fail.

If the LLM returns a malformed response, the builder raises `SkillBuilderError`. The most common cause is a model that can't follow the bare-JSON contract — try a larger or better-aligned model, or enable `OPENCLOSE_DEBUG_LLM=1` to see the raw reply.

## Skill file format

Example: `my-project/skills/daily-pr-digest.md`

```markdown
---
name: Daily PR digest
slug: daily-pr-digest
version: 1
created_at: 2026-04-22T09:00:00+00:00
source_session: 01JK5F...
parameters:
  - name: repo
    type: string
    required: true
    default: "leflakk/openclose"
  - name: since_hours
    type: int
    required: false
    default: "24"
required_tools:
  - name: bash
    sensitive: true
  - name: deliver_message
    sensitive: true
---

# Goal
Summarise PRs opened in the last $since_hours hours on $repo and send the summary to the team chat.

# Required tools
- `bash` — to run `gh pr list` (authenticated via the ambient gh token).
- `deliver_message` — to post to the `teamchat` channel.

# Procedure
1. Run `gh pr list --repo $repo --search "created:>$(date -u -d '-$since_hours hours' +%Y-%m-%dT%H:%M:%SZ)" --json number,title,author,url,labels`.
2. Parse the JSON; skip PRs labeled `dependabot` or `skip-digest`.
3. For each remaining PR, build a one-line entry: `#<number> <title> — @<author>`.
4. Call `deliver_message(channels=["teamchat"], message=<assembled markdown>)`.

# Pitfalls
- If `gh` exits non-zero, the skill should fail loudly rather than sending a partial digest.
- The default lookback of 24h assumes a daily run; when the Job's cron is weekly, override `since_hours` in the Job's `skill_parameters`.

# Verification
The Telegram post appears in `teamchat`, or `deliver_message` returns ok.
```

### Frontmatter fields

| Field | Type | Notes |
|---|---|---|
| `name` | string | Human title shown in the sidebar. |
| `slug` | string | Lowercase kebab-case; used as the filename and in URLs. |
| `version` | int | Bumped manually if you do a breaking edit; no runtime effect. |
| `created_at` | string | ISO-8601 timestamp set at save time. |
| `source_session` | string | The session ID the skill was distilled from (empty for hand-written skills). |
| `parameters[]` | list | Runtime parameters. Each has `name`, `type` (`string`/`int`/`bool`), `required`, `default`. |
| `required_tools[]` | list | Each has `name` + `sensitive: bool`. The runner uses this exact list as the agent's `allowed_tools` and pre-grants permissions. |

### Body sections

Five `# Heading` sections, in this order (parsed by `storage._parse_sections`):

- `# Goal` — one-sentence active-voice description of the outcome.
- `# Required tools` — prose describing how each tool is used.
- `# Procedure` — numbered steps; reference parameters as `$param_name`.
- `# Pitfalls` — known failure modes and how to recognise them.
- `# Verification` — observable signal that the run succeeded (currently advisory; see "Verification" in the Jobs guide).

Parameter substitution uses `config/agents.py::render_prompt_template`, which is the same engine used for agent system prompts. Unknown `$var` references are left literal — they won't crash the run, but they'll confuse the agent.

## Running a skill

### Manual run

From the UI: open the Skills panel, click the skill, fill in any required parameters, hit **Run**.

From the API:

```bash
curl -X POST http://127.0.0.1:9876/api/skills/daily-pr-digest/run \
  -H 'content-type: application/json' \
  -d '{"inputs": {"repo": "leflakk/openclose", "since_hours": "48"}, "trigger_message": ""}'
```

`trigger_message` overrides the default trigger ("Execute the Procedure above exactly. Use only the tools listed in Required tools. Do not ask me questions…"). Normally leave it empty.

The response is immediate — the run executes in the background:

```json
{"run_id": "01JK5F...", "file": "2026-04-22T09-00-00+00-00-01JK5F....jsonl", "started_at": "...", "status": "running"}
```

Poll `GET /api/skills/<slug>/runs` to check progress.

### Execution environment

Every manual run calls `skills.runner.execute_skill_to_files`, which:

1. Resolves variables (parameter defaults merged with user inputs — user wins).
2. Builds a one-off `Agent` with `allowed_tools = [t.name for t in skill.required_tools]`, `denied_tools = []`, `max_steps = 50`, temperature 0.1.
3. Builds a fresh `PermissionEngine` from the user's config, then *appends* an `ALLOW` rule for every required tool (any path). This means jobs/skills are not blocked on permission prompts — the contract is that the skill was vetted at save time.
4. Registers all built-in tools in a scratch `ToolRegistry`.
5. Drives `AgentLoop` to completion with the trigger message, writing every `StreamEvent` to the `.jsonl` file line-by-line.
6. Writes `final_text` to `.out.md`.

No `permission_broker`, no `plan_broker`, no `ask_user_broker` — if the agent tries to escalate, the call fails and the run ends.

### Run artifacts

`skills/<slug>/<iso-ts>-<run_id>.jsonl`:

```jsonl
{"type":"run_start","timestamp":"...","slug":"daily-pr-digest","skill_name":"Daily PR digest"}
{"type":"text","timestamp":"...","content":"I'll list the recent PRs..."}
{"type":"tool_call","timestamp":"...","tool_call":{"id":"...","name":"bash","arguments":"{\"command\":\"gh pr list...\"}"}}
{"type":"tool_result","timestamp":"...","tool_result":{"content":"[{...}]"}}
{"type":"text","timestamp":"...","content":"..."}
{"type":"run_end","timestamp":"...","status":"done","error":""}
```

`skills/<slug>/<iso-ts>-<run_id>.out.md` contains only the concatenated assistant text from `StreamEvent("text")`. If the run errored, the last `---\nError: ...` is appended.

`GET /api/skills/<slug>/runs` reads the first + last line of each `.jsonl` to derive status (`running` / `done` / `error`) and start/finish timestamps, plus the first line of `.out.md` as a preview.

## Tool sensitivity

The builder marks these as `sensitive: true`:

- `bash` — arbitrary shell, can exfiltrate or destroy.
- `write`, `edit`, `multiedit` — modify project files.
- `browser_automation_dom`, `browser_automation_vision` — can submit forms, navigate, log in.
- `deliver_message` — sends messages to configured channels.

The UI surfaces these with a red icon both in the Save-as-skill form and when a skill is about to be run (manual or via job). A `sensitive: false` skill is safe to run on a tight schedule; a `sensitive: true` skill means external side-effects each fire.

## Troubleshooting

**"No JSON object found in LLM response"** — the provider returned prose instead of the required JSON. Try a different model; if persistent, paste the conversation into the builder prompt at `src/openclose/skills/builder.py::_SYSTEM_PROMPT` and test manually with `curl`.

**"Extracted JSON is invalid"** — the LLM emitted broken JSON (common with small models). The builder's `_extract_json_object` is lenient about fenced blocks and leading prose, but can't fix structural errors.

**Skill hallucinates a tool that doesn't exist** — the builder is instructed to list only tools actually called in the session. If one slips through, edit the `.md` by hand and remove it from `required_tools`; the runner will fail fast if the skill calls an unregistered tool.

**Parameter default ignored** — the runner merges `inputs` *over* defaults. Check whether the Job's `skill_parameters[<slug>]` is overriding it.

**Skill is blocked on a permission prompt** — this shouldn't happen under the runner, because it pre-grants ALLOW rules for every required tool. If you see it, the tool the agent tried to call isn't in `required_tools`. Either add it (and save a new version) or have the agent stop calling it.

**Skill asks "what should I do next?"** — the trigger explicitly says "do not ask me questions — run unattended". If the agent still stalls, the procedure is probably ambiguous; tighten the Procedure section or add explicit error-handling steps.

## API reference

| Endpoint | Purpose |
|---|---|
| `GET /api/skills/tools` | List all registered tools with sensitivity flags (for the Save form). |
| `POST /api/skills/generate` | Body: `{session_id, user_prompt}`. Returns a draft `SkillForm`. |
| `POST /api/skills` | Body: SkillForm + `source_session`. Creates a new skill; slug is auto-disambiguated on collision. |
| `PUT /api/skills/{slug}` | Overwrite an existing skill. |
| `GET /api/skills` | List all skills (newest mtime first). |
| `GET /api/skills/{slug}` | Read a skill. |
| `DELETE /api/skills/{slug}` | Delete the `.md` and the runs folder. |
| `POST /api/skills/{slug}/duplicate` | Clone a skill under a new slug. |
| `POST /api/skills/{slug}/run` | Body: `{inputs, trigger_message}`. Kicks off a background run; returns `{run_id, file, started_at, status}`. |
| `GET /api/skills/{slug}/runs` | Most recent 10 runs (default), derived from the `.jsonl` files on disk. |
