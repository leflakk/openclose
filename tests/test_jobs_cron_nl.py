"""Tests for jobs.cron_nl — cron validation, JSON extraction, next-fire helpers."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from openclose.jobs.cron_nl import (
    CronTranslateError,
    CronTranslation,
    _extract_json,
    _valid_cron,
    next_fire_time,
    next_occurrences,
    translate_cron,
)


# ───────────────────────── _valid_cron ────────────────────────────────

def test_valid_cron_standard() -> None:
    assert _valid_cron("0 9 * * *") is True


def test_valid_cron_with_ranges() -> None:
    assert _valid_cron("0 9-17 * * 1-5") is True


def test_valid_cron_with_step() -> None:
    assert _valid_cron("*/15 * * * *") is True


def test_valid_cron_rejects_too_few_fields() -> None:
    assert _valid_cron("0 9 *") is False


def test_valid_cron_rejects_too_many_fields() -> None:
    assert _valid_cron("0 9 * * * 2025") is False


def test_valid_cron_rejects_nonsense() -> None:
    assert _valid_cron("not a cron") is False


def test_valid_cron_rejects_bad_field() -> None:
    assert _valid_cron("99 9 * * *") is False


def test_valid_cron_strips_whitespace() -> None:
    assert _valid_cron("  0 9 * * *  ") is True


# ───────────────────────── _extract_json ─────────────────────────────

def test_extract_json_bare_object() -> None:
    obj = _extract_json('{"cron": "0 9 * * *"}')
    assert obj == {"cron": "0 9 * * *"}


def test_extract_json_with_code_fence() -> None:
    obj = _extract_json('```json\n{"cron": "* * * * *"}\n```')
    assert obj == {"cron": "* * * * *"}


def test_extract_json_with_bare_fence() -> None:
    obj = _extract_json('```\n{"cron": "* * * * *"}\n```')
    assert obj == {"cron": "* * * * *"}


def test_extract_json_with_leading_prose() -> None:
    obj = _extract_json('Here you go: {"cron": "* * * * *"} done')
    assert obj == {"cron": "* * * * *"}


def test_extract_json_rejects_no_json() -> None:
    with pytest.raises(CronTranslateError, match="not JSON"):
        _extract_json("just prose, no braces")


def test_extract_json_rejects_malformed_json_inside() -> None:
    with pytest.raises(CronTranslateError, match="invalid"):
        _extract_json('prefix {not: json} suffix')


def test_extract_json_rejects_non_object() -> None:
    with pytest.raises(CronTranslateError, match="not a JSON object"):
        _extract_json("[1, 2, 3]")


# ───────────────────────── next_occurrences ─────────────────────────

def test_next_occurrences_count() -> None:
    occurrences = next_occurrences("0 9 * * *", timezone="UTC", count=3)
    assert len(occurrences) == 3
    # Each entry should be parseable ISO-8601
    for s in occurrences:
        datetime.fromisoformat(s)


def test_next_occurrences_monotonic() -> None:
    occurrences = next_occurrences("0 * * * *", timezone="UTC", count=4)
    parsed = [datetime.fromisoformat(s) for s in occurrences]
    for earlier, later in zip(parsed, parsed[1:]):
        assert later > earlier


def test_next_occurrences_falls_back_to_utc_on_bad_timezone() -> None:
    occurrences = next_occurrences("0 9 * * *", timezone="Not/A/Real", count=1)
    assert len(occurrences) == 1


def test_next_occurrences_respects_custom_timezone() -> None:
    paris = next_occurrences("0 9 * * *", timezone="Europe/Paris", count=1)
    utc = next_occurrences("0 9 * * *", timezone="UTC", count=1)
    paris_dt = datetime.fromisoformat(paris[0])
    utc_dt = datetime.fromisoformat(utc[0])
    assert paris_dt.utcoffset() != utc_dt.utcoffset()


# ───────────────────────── next_fire_time ───────────────────────────

def test_next_fire_time_strictly_after_ref() -> None:
    ref = datetime(2025, 1, 1, 8, 30, tzinfo=ZoneInfo("UTC"))
    fire = next_fire_time("0 9 * * *", "UTC", after=ref)
    assert fire > ref
    assert fire.hour == 9
    assert fire.minute == 0


def test_next_fire_time_naive_after_gets_timezone() -> None:
    ref_naive = datetime(2025, 1, 1, 8, 30)
    fire = next_fire_time("0 9 * * *", "UTC", after=ref_naive)
    # Output is tz-aware in the given zone.
    assert fire.tzinfo is not None


def test_next_fire_time_uses_now_when_no_after() -> None:
    before = datetime.now(tz=ZoneInfo("UTC"))
    fire = next_fire_time("* * * * *", "UTC")
    assert fire > before


def test_next_fire_time_invalid_timezone_falls_back_to_utc() -> None:
    fire = next_fire_time("0 9 * * *", "Not/Real")
    assert fire.tzinfo is not None


# ───────────────────────── translate_cron ───────────────────────────

@pytest.mark.asyncio
async def test_translate_cron_empty_input_raises() -> None:
    with pytest.raises(CronTranslateError, match="Empty"):
        await translate_cron("")


@pytest.mark.asyncio
async def test_translate_cron_passthrough_on_valid_cron() -> None:
    result = await translate_cron("0 9 * * *")
    assert isinstance(result, CronTranslation)
    assert result.cron == "0 9 * * *"
    assert result.description == ""


@pytest.mark.asyncio
async def test_translate_cron_passthrough_stripped() -> None:
    result = await translate_cron("  */5 * * * *  ")
    assert result.cron == "*/5 * * * *"


@pytest.mark.asyncio
async def test_translate_cron_llm_path_success() -> None:
    """NL input should be sent to the LLM, result validated."""
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = (
        '{"cron": "0 9 * * *", "description": "every day at 9am"}'
    )
    fake_provider = MagicMock()
    fake_provider.chat_sync = AsyncMock(return_value=fake_response)
    fake_provider.detect_model = AsyncMock(return_value="gpt-test")

    fake_config = MagicMock()
    fake_config.providers = []

    with patch("openclose.jobs.cron_nl.get_provider", return_value=fake_provider), \
         patch("openclose.jobs.cron_nl.get_config", return_value=fake_config):
        result = await translate_cron("every day at 9am")

    assert result.cron == "0 9 * * *"
    assert result.description == "every day at 9am"


@pytest.mark.asyncio
async def test_translate_cron_llm_empty_response_raises() -> None:
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = ""
    fake_provider = MagicMock()
    fake_provider.chat_sync = AsyncMock(return_value=fake_response)
    fake_provider.detect_model = AsyncMock(return_value="gpt-test")

    fake_config = MagicMock()
    fake_config.providers = []

    with patch("openclose.jobs.cron_nl.get_provider", return_value=fake_provider), \
         patch("openclose.jobs.cron_nl.get_config", return_value=fake_config):
        with pytest.raises(CronTranslateError, match="empty"):
            await translate_cron("hello")


@pytest.mark.asyncio
async def test_translate_cron_llm_invalid_cron_raises() -> None:
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = '{"cron": "not a cron"}'
    fake_provider = MagicMock()
    fake_provider.chat_sync = AsyncMock(return_value=fake_response)
    fake_provider.detect_model = AsyncMock(return_value="gpt-test")

    fake_config = MagicMock()
    fake_config.providers = []

    with patch("openclose.jobs.cron_nl.get_provider", return_value=fake_provider), \
         patch("openclose.jobs.cron_nl.get_config", return_value=fake_config):
        with pytest.raises(CronTranslateError, match="invalid cron"):
            await translate_cron("hello")


@pytest.mark.asyncio
async def test_translate_cron_no_model_configured_raises() -> None:
    fake_provider = MagicMock()
    fake_provider.detect_model = AsyncMock(return_value="")
    fake_config = MagicMock()
    fake_config.providers = []

    with patch("openclose.jobs.cron_nl.get_provider", return_value=fake_provider), \
         patch("openclose.jobs.cron_nl.get_config", return_value=fake_config):
        with pytest.raises(CronTranslateError, match="No model"):
            await translate_cron("hello")


@pytest.mark.asyncio
async def test_translate_cron_uses_configured_default_model() -> None:
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = '{"cron": "0 9 * * *"}'
    fake_provider = MagicMock()
    fake_provider.chat_sync = AsyncMock(return_value=fake_response)
    fake_provider.detect_model = AsyncMock(return_value="")

    fake_config = MagicMock()
    fake_provider_obj = MagicMock()
    fake_provider_obj.default_model = "gpt-configured"
    fake_config.providers = [fake_provider_obj]

    with patch("openclose.jobs.cron_nl.get_provider", return_value=fake_provider), \
         patch("openclose.jobs.cron_nl.get_config", return_value=fake_config):
        await translate_cron("hello")

    kwargs = fake_provider.chat_sync.call_args.kwargs
    assert kwargs["model"] == "gpt-configured"
