"""Tests for jobs.notify — channel resolution and send_job_notification."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openclose.jobs.notify import (
    _MESSAGE_CAP,
    _truncate,
    list_channel_aliases,
    send_job_notification,
)
from openclose.tool.tools.deliver_message.config import (
    ChannelSpec,
    reset_env_cache,
)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point deliver_message ConfigPaths at a tmp dir and scrub env vars."""
    import os

    monkeypatch.setattr(
        "openclose.tool.tools.deliver_message.config.ConfigPaths.config_dir",
        classmethod(lambda cls: tmp_path),
    )
    reset_env_cache()
    for key in [
        "OPENCLOSE_TELEGRAM_BOT_TOKEN",
        "OPENCLOSE_DISCORD_BOT_TOKEN",
        "OPENCLOSE_TELEGRAM_ALLOWED_USERS",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key in list(os.environ):
        if key.startswith("OPENCLOSE_CHANNEL_"):
            monkeypatch.delenv(key, raising=False)
    yield tmp_path
    reset_env_cache()


# ───────────────────────── _truncate ──────────────────────────────

def test_truncate_short_unchanged() -> None:
    assert _truncate("hello") == "hello"


def test_truncate_at_boundary() -> None:
    text = "x" * _MESSAGE_CAP
    assert _truncate(text) == text


def test_truncate_over_limit_adds_marker() -> None:
    text = "y" * (_MESSAGE_CAP + 100)
    out = _truncate(text)
    assert len(out) <= _MESSAGE_CAP
    assert out.endswith("[truncated]")


# ───────────────────────── list_channel_aliases ────────────────────

def test_list_channel_aliases_empty(config_dir: Path) -> None:
    assert list_channel_aliases() == []


def test_list_channel_aliases_sorted(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCLOSE_CHANNEL_ZULU", "telegram:-111")
    monkeypatch.setenv("OPENCLOSE_CHANNEL_ALPHA", "discord:222")
    reset_env_cache()
    out = list_channel_aliases()
    aliases = [x["alias"] for x in out]
    assert aliases == ["alpha", "zulu"]
    assert {x["platform"] for x in out} == {"telegram", "discord"}


# ───────────────────────── send_job_notification ───────────────────

@pytest.mark.asyncio
async def test_send_unknown_alias_returns_error(config_dir: Path) -> None:
    ok, err = await send_job_notification("ghost", "hello")
    assert ok is False
    assert "Unknown channel alias" in err


@pytest.mark.asyncio
async def test_send_missing_token_returns_error(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCLOSE_CHANNEL_TEAM", "telegram:-100")
    # No token
    reset_env_cache()
    ok, err = await send_job_notification("team", "hello")
    assert ok is False
    assert "No bot token" in err


@pytest.mark.asyncio
async def test_send_telegram_allowlist_blocks(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_BOT_TOKEN", "tg_tok")
    monkeypatch.setenv("OPENCLOSE_CHANNEL_USER", "telegram:99999")
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_ALLOWED_USERS", "1,2,3")  # 99999 not in list
    reset_env_cache()
    ok, err = await send_job_notification("user", "hello")
    assert ok is False
    assert "allowlist" in err.lower()


@pytest.mark.asyncio
async def test_send_telegram_happy_path(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_BOT_TOKEN", "tg_tok")
    monkeypatch.setenv("OPENCLOSE_CHANNEL_OPS", "telegram:-100")
    reset_env_cache()

    fake_outcome = MagicMock()
    fake_outcome.ok = True
    fake_outcome.error = ""
    fake_send = AsyncMock(return_value=fake_outcome)

    with patch("openclose.jobs.notify.tg.send", fake_send):
        ok, err = await send_job_notification("ops", "short message")

    assert ok is True
    assert err == ""
    fake_send.assert_awaited_once()
    # Target_id and token passed through.
    args = fake_send.call_args
    assert args.args[1] == "tg_tok"
    assert args.args[2] == "-100"


@pytest.mark.asyncio
async def test_send_discord_happy_path(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCLOSE_DISCORD_BOT_TOKEN", "dc_tok")
    monkeypatch.setenv("OPENCLOSE_CHANNEL_TEAM", "discord:42")
    reset_env_cache()

    fake_outcome = MagicMock()
    fake_outcome.ok = True
    fake_outcome.error = ""

    with patch(
        "openclose.jobs.notify.dc.send",
        AsyncMock(return_value=fake_outcome),
    ) as fake_send:
        ok, err = await send_job_notification("team", "hello")

    assert ok is True
    fake_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_truncates_long_messages(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("OPENCLOSE_CHANNEL_OPS", "telegram:-1")
    reset_env_cache()

    fake_outcome = MagicMock()
    fake_outcome.ok = True
    fake_outcome.error = ""

    with patch(
        "openclose.jobs.notify.tg.send",
        AsyncMock(return_value=fake_outcome),
    ) as fake_send:
        text = "a" * (_MESSAGE_CAP + 500)
        await send_job_notification("ops", text)

    sent_text = fake_send.call_args.args[3]
    assert len(sent_text) <= _MESSAGE_CAP


@pytest.mark.asyncio
async def test_send_reports_outcome_error(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("OPENCLOSE_CHANNEL_OPS", "telegram:-1")
    reset_env_cache()

    fake_outcome = MagicMock()
    fake_outcome.ok = False
    fake_outcome.error = "rate limited"

    with patch(
        "openclose.jobs.notify.tg.send",
        AsyncMock(return_value=fake_outcome),
    ):
        ok, err = await send_job_notification("ops", "hello")

    assert ok is False
    assert err == "rate limited"


@pytest.mark.asyncio
async def test_send_unsupported_platform_returns_error(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a channel somehow resolves to an unknown platform, error cleanly."""
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_BOT_TOKEN", "tok")
    reset_env_cache()

    # Inject a channel with a synthetic platform via the resolved list.
    fake_cfg = MagicMock()
    fake_cfg.token_for.return_value = "tok"
    fake_cfg.is_target_allowed.return_value = True
    fake_spec = ChannelSpec(alias="x", platform="telegram", target_id="1")
    # Override platform after construction via type: ignore — ChannelSpec is Literal-typed.
    object.__setattr__(fake_spec, "platform", "sms")

    with patch("openclose.jobs.notify.load_messaging_config", return_value=fake_cfg), \
         patch("openclose.jobs.notify.resolve_channels", return_value=([fake_spec], [])):
        ok, err = await send_job_notification("x", "hello")

    assert ok is False
    assert "Unsupported platform" in err
