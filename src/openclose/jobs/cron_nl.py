"""Natural-language → cron translator + next-occurrence helpers.

Flow:
1. If the input is already a valid 5-field cron expression, skip the LLM
   and return it as-is (no description).
2. Otherwise call the provider with a tight schema-returning prompt and
   validate the result against croniter before handing it back.
3. `next_occurrences()` renders N upcoming fire times in the caller's tz.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel

from openclose.config.config import get_config
from openclose.log import get_logger
from openclose.provider.provider import get_provider

log = get_logger(__name__)


class CronTranslateError(RuntimeError):
    """Raised when the LLM output or user input can't be turned into a cron."""


class CronTranslation(BaseModel):
    cron: str
    description: str = ""


_SYSTEM_PROMPT = """\
You translate a short natural-language scheduling phrase into a standard
5-field cron expression (minute hour day-of-month month day-of-week).

Rules:
- Return exactly 5 space-separated fields. No seconds, no year.
- Day-of-week is 0-6 where 0 = Sunday (Unix convention), or shortcuts
  MON-SUN.
- Allowed operators: `*`, `,`, `-`, `/`.
- If the user's phrase is ambiguous (no time specified), pick a sane
  default: "daily" → 09:00, "weekly" → Monday 09:00, "monthly" → 1st of
  month 09:00.
- Assume times are in the timezone the user provided at the end.

OUTPUT CONTRACT (critical):
- Reply with a SINGLE JSON object and NOTHING ELSE.
- No preamble, no code fences, no commentary.
- Schema:
  {"cron": "<5-field cron>", "description": "<one-line English description>"}
- First character of your reply MUST be `{`.
"""


_CRON_SHAPE = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")


def _valid_cron(expr: str) -> bool:
    """True iff `expr` has 5 fields and croniter accepts it."""
    if not _CRON_SHAPE.match(expr.strip()):
        return False
    try:
        croniter(expr.strip(), datetime.now())
        return True
    except Exception:  # noqa: BLE001 — croniter raises several types
        return False


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)```\s*$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise CronTranslateError("LLM reply is not JSON")
        try:
            obj = json.loads(stripped[start:end + 1])
        except json.JSONDecodeError as e:
            raise CronTranslateError(f"LLM JSON invalid: {e}") from e
    if not isinstance(obj, dict):
        raise CronTranslateError("LLM reply is not a JSON object")
    return obj


async def translate_cron(text: str, timezone: str = "UTC") -> CronTranslation:
    """Return a validated cron expression for `text`.

    If `text` already parses as a cron expression, it's returned verbatim
    with an empty description. Otherwise the LLM is called.
    """
    text = text.strip()
    if not text:
        raise CronTranslateError("Empty input")

    if _valid_cron(text):
        return CronTranslation(cron=text, description="")

    provider = get_provider()
    config = get_config()
    model = config.providers[0].default_model if config.providers else ""
    if not model:
        model = await provider.detect_model() or ""
    if not model:
        raise CronTranslateError("No model configured")

    user_msg = f"Phrase: {text}\nTimezone: {timezone}"
    response = await provider.chat_sync(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        model=model,
        temperature=config.temperatures.cron_nl,
        max_tokens=300,
    )
    content = response.choices[0].message.content or ""
    if not content.strip():
        raise CronTranslateError("LLM returned empty response")

    raw = _extract_json(content)
    cron = str(raw.get("cron", "")).strip()
    if not _valid_cron(cron):
        raise CronTranslateError(f"LLM produced invalid cron: {cron!r}")

    return CronTranslation(
        cron=cron,
        description=str(raw.get("description", "")).strip(),
    )


def next_occurrences(
    cron: str, timezone: str = "UTC", count: int = 5
) -> list[str]:
    """Return the next `count` fire times as ISO-8601 strings in `timezone`."""
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz=tz)
    it = croniter(cron.strip(), now)
    return [it.get_next(datetime).isoformat(timespec="seconds") for _ in range(count)]


def next_fire_time(cron: str, timezone: str, after: datetime | None = None) -> datetime:
    """Return the next fire datetime strictly after `after` (or now)."""
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    ref = after if after is not None else datetime.now(tz=tz)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=tz)
    it = croniter(cron.strip(), ref)
    result: datetime = it.get_next(datetime)
    return result
