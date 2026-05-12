"""Merge per-chunk VLM procedures into one coherent recording procedure.

Single text-only LLM pass. Receives N chunk procedures (each already labeled
with its global time window and containing timestamp-prefixed steps) plus
the full events JSON, and emits one numbered procedure covering the whole
recording. Output feeds straight into `build_intelligent_task` unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from openclose.config.config import get_config
from openclose.log import get_logger
from openclose.provider.provider import get_provider

log = get_logger(__name__)


class MergerError(RuntimeError):
    """Raised when the merger LLM returns nothing usable."""


_SYSTEM_PROMPT = """\
You are given N per-chunk procedures derived from a single browser
recording split into overlapping time windows. Each step in each chunk
carries a global timestamp in square brackets, e.g. `[14.3s]`.

Produce ONE numbered procedure describing the whole recording in
chronological order. Rules:

- Every step must keep its global timestamp prefix, e.g. `[14.3s] Click …`.
- When chunks overlap, the same action often appears in two inputs.
  Merge duplicates into a single step, preferring the more specific
  description.
- EVENTS JSON is authoritative for typed text, clicked labels, and
  navigations. If a chunk describes a click or navigation that does not
  appear in EVENTS within ±0.5s of its timestamp, drop that step.
- Never invent steps, selectors, URLs, or values absent from the inputs.
- Preserve verbatim typed values.
- Preserve `external` vs `in_page_click` phrasing when describing
  navigations — a later stage depends on this distinction.

Output the final numbered procedure only — no preamble, no commentary,
no recap, no code fences. The first character of your reply must be `1`.
"""


_USER_TEMPLATE = """\
TOTAL_DURATION: {duration:.2f}s

CHUNK PROCEDURES (in chronological order):

{chunks_block}

EVENTS (JSON, authoritative — `t_global` is the canonical recording time):
```json
{events_json}
```
"""


def _resolve_model() -> str:
    config = get_config()
    if config.providers and config.providers[0].default_model:
        return config.providers[0].default_model
    return "local"


def _format_chunks(chunk_procedures: list[tuple[float, float, str]]) -> str:
    parts: list[str] = []
    for i, (t0, t1, text) in enumerate(chunk_procedures):
        parts.append(
            f"--- CHUNK {i + 1} [{t0:.1f}s - {t1:.1f}s] ---\n{text.strip()}"
        )
    return "\n\n".join(parts)


async def merge_chunk_procedures(
    chunk_procedures: list[tuple[float, float, str]],
    events: list[dict[str, Any]],
    total_duration: float,
) -> str:
    """Stitch per-chunk procedures into a single chronological procedure."""
    if not chunk_procedures:
        return ""

    provider = get_provider()
    model = _resolve_model()

    events_text = json.dumps(events, ensure_ascii=False, indent=2)
    user_message = _USER_TEMPLATE.format(
        duration=total_duration,
        chunks_block=_format_chunks(chunk_procedures),
        events_json=events_text,
    )

    log.info(
        "merger: merging %d chunks (%d events, duration %.2fs) via %s",
        len(chunk_procedures), len(events), total_duration, model,
    )

    response = await provider.client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=get_config().temperatures.recorder_merger,
        max_tokens=8192,
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
                "merger: content empty, using reasoning "
                "(finish_reason=%s, %d chars)",
                choice.finish_reason, len(reasoning),
            )
            content = reasoning

    if not content.strip():
        raise MergerError(
            f"empty LLM response (finish_reason={choice.finish_reason})"
        )
    return content.strip()
