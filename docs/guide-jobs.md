# Jobs Guide

## Overview

A **job** is an ordered chain of one or more [skills](guide-skills.md) with a fire time and an optional notification. Jobs are OpenClose's "scheduler" feature: once a skill works, wrap it in a job and stop thinking about it.

Jobs live on disk as JSON (one file per job) and run inside `openclose serve`. There is no separate daemon — if the server is down, jobs don't fire.

```
<config_dir>/<project>/jobs/                  # config_dir varies per OS (see README)
├── <job-id>.json                              # job config
└── <job-id>/
    └── <iso-ts>-<run_id>/
        ├── summary.json                       # top-level run summary
        ├── <skill_slug>.jsonl                 # per-skill event log
        └── <skill_slug>.out.md                # per-skill final text
```

## Creating a job

From the UI: Jobs panel → **New job** → fill the form → Save. From the API:

```bash
curl -X POST http://127.0.0.1:9876/api/jobs \
  -H 'content-type: application/json' \
  -d '{
    "name": "Daily PR digest",
    "skills": ["daily-pr-digest"],
    "skill_parameters": {
      "daily-pr-digest": {"repo": "leflakk/openclose", "since_hours": "24"}
    },
    "timing": {"mode": "recurring", "cron": "0 9 * * 1-5", "timezone": "Europe/Paris"},
    "on_failure": "stop",
    "notification": {"channel": "teamchat", "notify_on": "always", "include_output": true},
    "enabled": true
  }'
```

## Timing

### Recurring

```json
{"mode": "recurring", "cron": "0 9 * * 1-5", "timezone": "Europe/Paris"}
```

- `cron` is a standard 5-field expression (`minute hour day-of-month month day-of-week`). No seconds, no year.
- `timezone` is any IANA name. Invalid / unknown timezone falls back to `UTC`.
- The scheduler computes the **next** fire strictly after `now`. Missed fires (server was down, laptop was asleep) are silently skipped — there's no catch-up storm.

### One-shot

```json
{"mode": "one_shot", "run_at": "2026-04-23T09:00:00+02:00", "timezone": "Europe/Paris"}
```

- `run_at` is an ISO-8601 timestamp. If naive, the job's `timezone` is attached.
- Fires exactly once, then flips `executed=true` in the config and won't fire again.
- **Past-due one-shots at startup are disarmed, not run**: on `openclose serve` boot, any one-shot with `run_at < now` and `executed=false` is silently flipped to `executed=true`. The rationale is that a job meant to fire last Tuesday is almost never something you want to run "now" on Friday.

### Natural-language cron helper

```bash
curl -X POST http://127.0.0.1:9876/api/jobs/cron/parse \
  -H 'content-type: application/json' \
  -d '{"text": "every weekday at 9am", "timezone": "Europe/Paris"}'
```

```json
{"cron": "0 9 * * 1-5", "description": "Every weekday at 9:00 (Mon-Fri)"}
```

If `text` already looks like a valid 5-field cron, the LLM is skipped and the expression is returned verbatim. Otherwise the provider is asked to translate, and the result is validated against `croniter` before being handed back — invalid outputs raise `CronTranslateError` and the UI surfaces them as an error next to the input field.

The UI form also shows "next 5 fire times" for a given cron + timezone, computed locally with `next_occurrences`.

## Chaining skills

`skills` is an ordered list of slugs. Skills run sequentially in that order, each in its own fresh `AgentLoop`. Output is not passed between skills — if B depends on A's result, design A to write to disk (a temp file, a summary under the project dir) and have B read it.

### Per-skill parameter overrides

```json
"skill_parameters": {
  "daily-pr-digest": {"repo": "leflakk/openclose", "since_hours": "24"},
  "send-summary": {"channel_override": "ops"}
}
```

Each key is a skill slug; each value is a `{param_name: string}` dict. The skill runner merges these over the skill's own parameter defaults. Parameters not mentioned fall back to the skill's defaults.

### `on_failure`

| Value | Behaviour |
|---|---|
| `stop` (default) | The first failing skill aborts the chain. Remaining skills are marked `skipped`. Job status becomes `failed` or `partial`. |
| `continue` | Failures are logged but the chain presses on. Job status is `partial` if any skill failed or was skipped. |

Status rollup (see `_derive_overall_status` in `jobs/runner.py`):

- All skills passed → `passed`
- No skills passed → `failed`
- Mixed (some passed, some failed/skipped) → `partial`

## Notifications

```json
"notification": {"channel": "teamchat", "notify_on": "always", "include_output": true}
```

- `channel` — a `deliver_message` alias from the `.env` file in your openclose config directory. Empty string = no notification.
- `notify_on` — one of `failure`, `always`, `verification_fail`. As of today `verification_fail` behaves like `failure` (there's no machine-readable PASS/FAIL signal yet; future work).
- `include_output` — if true, each skill's `output_preview` (first 200 chars of its `.out.md`) is appended to the message.

Message shape:

```
✅ Job "Daily PR digest" — PASSED
Started: 2026-04-22T09:00:00+00:00
Duration: 17.4s

  ✓ daily-pr-digest (12.3s)

Outputs:
  daily-pr-digest: Posted 4 PRs to #teamchat.
```

Notifications are sent by `jobs.notify.send_job_notification`, which reuses the `deliver_message` HTTP senders. The universal cap is 2000 chars (Discord's limit); longer outputs are truncated with `…[truncated]`.

Channel aliases are configured once in `.env` and shared by the `deliver_message` tool and by job notifications. See [Messaging in the README](../README.md#messaging-deliver_message-tool) for the `.env` format.

## Enable / disable / run now

| Endpoint | Purpose |
|---|---|
| `POST /api/jobs/{job_id}/enable` | Body: `{enabled: bool}`. Flip without deleting. |
| `POST /api/jobs/{job_id}/run` | Fire the job on demand — queued if a run is already in flight. |
| `GET /api/jobs/{job_id}/runs` | List the most recent 20 runs with compact status. |
| `GET /api/jobs/{job_id}/runs/{run_folder}` | Fetch the full `summary.json` + paths to per-skill logs. |

Disabling a job just sets `enabled=false` — the next scheduler tick will see it and skip. No state is cleared, so re-enabling resumes the next-fire computation.

## Run artifacts

Each run gets a folder named `<iso-ts>-<run_id>/`:

```
jobs/<job-id>/2026-04-22T09-00-00+00-00-01JK5F.../
├── summary.json
├── daily-pr-digest.jsonl       # event log for the first skill
├── daily-pr-digest.out.md      # final text for the first skill
├── send-summary.jsonl
└── send-summary.out.md
```

`summary.json` is overwritten *during* the run as skills start / finish, so a crashed run leaves partial but useful state:

```json
{
  "job_id": "...",
  "job_name": "Daily PR digest",
  "run_id": "01JK5F...",
  "started_at": "...",
  "finished_at": "...",
  "duration_s": 17.4,
  "status": "passed",
  "skills": [
    {"slug": "daily-pr-digest", "status": "passed", "duration_s": 12.3,
     "output_preview": "Posted 4 PRs to #teamchat.",
     "jsonl_file": "daily-pr-digest.jsonl", "output_file": "daily-pr-digest.out.md"}
  ],
  "notification_sent": true,
  "notification_error": ""
}
```

## Scheduler internals

- **Where it lives**: `src/openclose/jobs/scheduler.py::JobScheduler`. A single module-level singleton (`get_scheduler()`), started in `server/app.py`'s lifespan.
- **Tick interval**: 20 seconds. Minute-granular cron means we never miss a fire window by more than the tick.
- **Concurrency**: a per-job `asyncio.Lock`. If a fire comes in while the previous run still holds the lock, the fire is dropped with a warning (never queued).
- **Config reloads**: none. The scheduler re-reads every job config from disk on every tick, so editing a `<job-id>.json` just works.
- **Invalid cron**: logged once and skipped; the scheduler keeps running.

### Not available in `openclose run`

The scheduler starts only in `serve`. If you need a one-off job fire from CI, call `POST /api/jobs/{id}/run` against a running server, or invoke `skills.runner.execute_skill_to_files` directly from a Python script.

## Troubleshooting

**Job's next-fire timestamp looks wrong** — check the `timezone` field. An unknown tz falls back silently to UTC; make sure it's a valid IANA name like `Europe/Paris`, not `CEST`.

**Job fired but no notification** — check `summary.json`'s `notification_error`. Common causes: the `channel` alias doesn't exist in `.env`, the bot token is missing, or the Telegram allowlist blocks the chat_id.

**One-shot didn't run at startup** — that's by design. A past-due one-shot is disarmed. Set `run_at` in the future and re-save.

**Two runs overlapped** — they can't. The per-job lock drops a fire rather than queueing it. Check the log for `"previous run still holds lock"`.

**Scheduler stopped firing** — check `openclose serve` is actually running (the job scheduler lives in the server process). In logs, look for `JobScheduler tick crashed; continuing` — the tick recovers, but something is going sideways.

**Cron translator returns garbage** — the provider returned bad JSON. The validator refuses invalid crons so the job isn't saved with a broken expression, but you need a model that can follow the tight JSON contract. Check the raw response with `OPENCLOSE_DEBUG_LLM=1`.

## API reference

| Endpoint | Purpose |
|---|---|
| `GET /api/jobs/channels` | List configured `deliver_message` aliases (for the notification picker). |
| `POST /api/jobs/cron/parse` | Body: `{text, timezone}`. Returns `{cron, description}` or an error. |
| `POST /api/jobs` | Create a job. |
| `PUT /api/jobs/{job_id}` | Overwrite a job. |
| `GET /api/jobs` | List all jobs, newest-modified first. |
| `GET /api/jobs/{job_id}` | Read a job. |
| `DELETE /api/jobs/{job_id}` | Delete config + run artifacts. |
| `POST /api/jobs/{job_id}/duplicate` | Clone a job (copies skills, timing, notification). |
| `POST /api/jobs/{job_id}/enable` | Enable/disable. |
| `POST /api/jobs/{job_id}/run` | Manual fire. |
| `GET /api/jobs/{job_id}/runs` | List recent runs. |
| `GET /api/jobs/{job_id}/runs/{run_folder}` | Read one run's `summary.json` + paths. |
