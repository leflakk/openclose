"""Intelligent task builder — text-only LLM pass over the raw procedure.

Takes the VLM's literal procedural transcript plus the raw events log and
user-entered name/description, and produces a structured task definition
the scheduled-agent runtime can execute unattended. The LLM is guided,
via its prompt, to distinguish CONSTANTS (values baked in from the
recording) from RUNTIME OBSERVATIONS (data read live from the page at
execution time). Whatever the LLM returns is written to the task file
verbatim — no post-processing, no format repair.
"""

from __future__ import annotations

import json
from typing import Any

from openclose.config.config import get_config
from openclose.log import get_logger
from openclose.provider.provider import get_provider
from openclose.recorder.storage import recording_dir

log = get_logger(__name__)


class TaskBuilderError(RuntimeError):
    """Raised when the second-pass LLM call returns nothing usable."""


_SYSTEM_PROMPT = """\
You convert a browser-session recording into a reusable task definition
that will run unattended on a schedule.

You will receive:
- NAME: the recurring task's short name (chosen by the user).
- DESCRIPTION: a freeform description of the intent (chosen by the user).
- RECORDED_AT: ISO timestamp of the recording.
- PROCEDURE: a numbered literal procedure written by a vision model that
  watched the recording.
- EVENTS: the raw event log (JSON; timestamps in seconds from start).
  Authoritative for exact text typed, URLs navigated, and element
  labels. When PROCEDURE and EVENTS disagree, prefer EVENTS.

Distinguish two kinds of values:
- CONSTANTS: values the user wants fixed on every future run, baked into
  the task. Examples: their email address, the target subreddit URL, a
  prompt template they always use.
- RUNTIME OBSERVATIONS: data that changes every run and must be read live
  from the page at execution time. Examples: today's top posts, the
  current price, the latest headlines. If NAME or DESCRIPTION says
  "daily", "weekly", "each morning", time-sensitive page data is a
  RUNTIME OBSERVATION.

The actual values seen during the recording for each runtime observation
go under "Task example observations" as frozen-time evidence, not values
to replay.

Rules:
- Never invent steps, URLs, selectors, values, or destinations absent
  from PROCEDURE or EVENTS.
- Prefer the shortest reliable workflow. Drop obvious noise: idle page
  loads, accidental clicks, a tab that was immediately closed, a bounce
  through a default new-tab or search page on the way to the real
  destination.
- Describe elements by visible label and role, not coordinates or CSS
  selectors. Keep exact text for constants.
- In Task workflow, reference constants by name (e.g. `source_url`,
  `destination_email`).
- `navigate` with `origin: external` means the user went to a URL
  directly (address bar, bookmark, back/forward). `origin:
  in_page_click` means a link on the page was clicked.
- `paste` events carry `target_label` naming the destination field —
  use it.

Output contract:
- Reply with the filled task template below and nothing else.
- The very first characters of your reply must be `## Task goal`.
- No preamble, no reasoning, no commentary, no recap, no code fences.
- Use these exact section headings in this exact order. If a section
  has no content, keep its heading and leave the body empty.

## Task goal
<one sentence, active voice, the recurring task>

## Task constants
- <snake_case_name>: <exact value from recording>

## Task runtime observations
- <what must be gathered live each run — be specific about where to
  read from and how many items>

## Task example observations
- <the actual values seen during the recording for each runtime
  observation, verbatim from EVENTS where available>

## Task preconditions
- <starting-state facts that must be true before the task runs — e.g.
  "User is signed in to Gmail". Infer only from PROCEDURE/EVENTS.>

## Task workflow
1. <step 1, referencing constants by name, describing elements by
   visible label and role>
2. <step 2>

## Task success criteria
- <observable signal the run succeeded — a toast, a URL, an element>
"""


_USER_TEMPLATE = """\
NAME: {name}

DESCRIPTION: {description}

RECORDED_AT: {recorded_at}

PROCEDURE:
{procedure}

EVENTS (JSON):
```json
{events_json}
```
"""


def _resolve_model() -> str:
    """Pick a model id for the builder call (mirrors annotator._resolve_model)."""
    config = get_config()
    if config.providers and config.providers[0].default_model:
        return config.providers[0].default_model
    return "local"


def _dump_raw_response(recording_id: str, content: str) -> None:
    """Persist the raw second-pass LLM response alongside the recording."""
    try:
        path = recording_dir(recording_id) / f"{recording_id}.task_builder_raw.md"
        path.write_text(content, encoding="utf-8")
        log.info("task_builder: raw response saved to %s", path)
    except OSError as e:
        log.warning("task_builder: failed to dump raw response: %s", e)


async def build_intelligent_task(
    *,
    recording_id: str,
    name: str,
    description: str,
    recorded_at: str,
    raw_procedure: str,
    events: list[dict[str, Any]],
) -> str:
    """Run the second LLM pass and return its response as the task body.

    The LLM is guided entirely through the prompt; whatever it returns
    (minus surrounding whitespace) is the task body. The caller wraps
    it in YAML frontmatter. Raises TaskBuilderError on empty response;
    the raw response is always dumped to
    recordings/artifacts/{recording_id}/{recording_id}.task_builder_raw.md
    for debugging.
    """
    provider = get_provider()
    model = _resolve_model()

    events_text = json.dumps(events, ensure_ascii=False, indent=2)
    user_message = _USER_TEMPLATE.format(
        name=name,
        description=description,
        recorded_at=recorded_at,
        procedure=raw_procedure.strip(),
        events_json=events_text,
    )

    log.info(
        "task_builder: sending procedure (%d chars) + %d events to %s",
        len(raw_procedure), len(events), model,
    )

    response = await provider.client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=get_config().temperatures.recorder_task_builder,
        max_tokens=4096,
    )

    choice = response.choices[0]
    message = choice.message
    content = message.content or ""

    if not content.strip():
        reasoning = (
            getattr(message, "reasoning", None)
            or getattr(message, "reasoning_content", None)
            or ""
        )
        if reasoning.strip():
            log.warning(
                "task_builder: content empty, using reasoning field "
                "(finish_reason=%s, %d chars)",
                choice.finish_reason, len(reasoning),
            )
            content = reasoning

    _dump_raw_response(recording_id, content)

    if not content.strip():
        raise TaskBuilderError(
            f"empty LLM response (finish_reason={choice.finish_reason})"
        )

    return content.strip() + "\n"
