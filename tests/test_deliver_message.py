"""Tests for the deliver_message tool."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from openclose.tool.tools.deliver_message import discord as dc_sender
from openclose.tool.tools.deliver_message import make_deliver_message_tool
from openclose.tool.tools.deliver_message import telegram as tg_sender
from openclose.tool.tools.deliver_message.config import (
    ChannelSpec,
    load_messaging_config,
    reset_env_cache,
    resolve_channels,
)
from openclose.tool.tools.deliver_message.splitter import (
    _find_break,
    _state_after,
    split_message,
)


# ───────────────────────── splitter: _find_break ────────────────────────

def test_find_break_empty() -> None:
    assert _find_break("") == 0


def test_find_break_paragraph_preferred() -> None:
    w = "para1.\n\npara2 with word breaks. More."
    assert _find_break(w) == len("para1.\n\n")


def test_find_break_line_fallback() -> None:
    w = "line1\nline2\nline3"
    assert _find_break(w) == len("line1\nline2\n")


def test_find_break_sentence_fallback() -> None:
    w = "One sentence. Two sentences. Three"
    assert _find_break(w) == len("One sentence. Two sentences. ")


def test_find_break_word_fallback() -> None:
    w = "oneword twoword threeword"
    assert _find_break(w) == len("oneword twoword ")


def test_find_break_raw_fallback() -> None:
    w = "abcdefghij"
    assert _find_break(w) == 10


# ───────────────────────── splitter: _state_after ───────────────────────

def test_state_after_no_fence() -> None:
    assert _state_after("hello\nworld", None) is None


def test_state_after_opens_block() -> None:
    assert _state_after("hello\n```py", None) == "py"


def test_state_after_closes_block() -> None:
    assert _state_after("code\n```", "py") is None


def test_state_after_open_close_net_zero() -> None:
    assert _state_after("x\n```py\ncode\n```\nafter", None) is None


def test_state_after_unnamed_block_open() -> None:
    assert _state_after("```", None) == ""


# ───────────────────────── split_message — short paths ─────────────────

def test_split_short_message() -> None:
    chunks = split_message("hello", hard_limit=100)
    assert chunks == ["hello"]


def test_split_empty_message() -> None:
    assert split_message("", hard_limit=100) == [""]


def test_split_exact_boundary() -> None:
    text = "x" * 100
    chunks = split_message(text, hard_limit=100)
    assert chunks == [text]


# ───────────────────────── split_message — boundaries ─────────────────

def test_split_paragraph_boundary_preferred() -> None:
    """A message with paragraph breaks should split on \\n\\n."""
    para = "p" * 60
    text = f"{para}\n\n{para}\n\n{para}"  # 60 + 2 + 60 + 2 + 60 = 184
    chunks = split_message(text, hard_limit=80)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 80
    # All chunks (except last) end right after a paragraph break, with
    # no content bleed.
    stripped = [re.sub(r"\n\(\d+/\d+\)$", "", c) for c in chunks]
    # Concatenation should equal original (we split after \n\n so that
    # the break stays attached to the previous chunk).
    assert "".join(stripped) == text


def test_split_line_boundary_when_no_paragraphs() -> None:
    lines = [f"line{i}" for i in range(30)]
    text = "\n".join(lines)
    chunks = split_message(text, hard_limit=60)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 60
    stripped = [re.sub(r"\n\(\d+/\d+\)$", "", c) for c in chunks]
    assert "".join(stripped) == text


def test_split_sentence_boundary_when_no_newlines() -> None:
    text = "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten."
    chunks = split_message(text, hard_limit=50)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 50


def test_split_word_boundary_fallback() -> None:
    text = " ".join(["word"] * 60)
    chunks = split_message(text, hard_limit=60)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 60


def test_split_raw_slice_last_resort() -> None:
    text = "x" * 500
    chunks = split_message(text, hard_limit=100)
    assert len(chunks) >= 5
    for c in chunks:
        assert len(c) <= 100


# ───────────────────────── split_message — markers ─────────────────────

def test_split_no_marker_when_single_chunk() -> None:
    chunks = split_message("short", hard_limit=100)
    assert chunks == ["short"]
    assert "(1/1)" not in chunks[0]


def test_split_marker_applied_for_multi_chunk() -> None:
    text = "a " * 500
    chunks = split_message(text, hard_limit=100)
    assert len(chunks) >= 2
    total = len(chunks)
    for i, c in enumerate(chunks, start=1):
        assert c.endswith(f"\n({i}/{total})")
        assert len(c) <= 100


# ───────────────────────── split_message — code blocks ────────────────

def test_split_small_code_block_stays_intact() -> None:
    text = "before\n```py\nx = 1\n```\nafter"
    chunks = split_message(text, hard_limit=200)
    assert chunks == [text]


def test_split_large_code_block_preserved() -> None:
    """A code block larger than the limit should be split into multiple
    fenced pieces, each closed and reopened with the same language tag."""
    body = "\n".join(f"line_{i} = {i}" for i in range(80))
    text = f"intro\n```py\n{body}\n```\nend"
    chunks = split_message(text, hard_limit=200)

    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 200
        # Count fences — must be even in each chunk (balanced)
        fences = sum(1 for line in c.split("\n")
                     if line.strip().startswith("```"))
        assert fences % 2 == 0, f"Unbalanced fences in chunk:\n{c}"


def test_split_preserves_language_on_reopen() -> None:
    body = "\n".join(f"x = {i}" for i in range(200))
    text = f"```python\n{body}\n```"
    chunks = split_message(text, hard_limit=200)

    assert len(chunks) >= 2
    # Every chunk that contains code should have the "python" tag.
    interior_or_first = [c for c in chunks if "```python" in c]
    assert len(interior_or_first) >= 1


# ───────────────────────── split_message — hard-limit safety ──────────

def test_no_chunk_exceeds_hard_limit_small_limit() -> None:
    text = ("hello world. " * 200)
    chunks = split_message(text, hard_limit=100)
    for c in chunks:
        assert len(c) <= 100


def test_no_chunk_exceeds_hard_limit_discord() -> None:
    text = "x" * 10_000
    chunks = split_message(text, hard_limit=2000)
    for c in chunks:
        assert len(c) <= 2000


def test_no_chunk_exceeds_hard_limit_telegram() -> None:
    text = "x" * 20_000
    chunks = split_message(text, hard_limit=4096)
    for c in chunks:
        assert len(c) <= 4096


# ───────────────────────── split_message — reassembly invariant ───────

def _strip_markers(chunks: list[str]) -> list[str]:
    return [re.sub(r"\n\(\d+/\d+\)$", "", c) for c in chunks]


def test_reassembly_prose_no_code() -> None:
    """Prose-only input should reassemble exactly (no fences inserted)."""
    para = "The quick brown fox jumps over the lazy dog. " * 30
    text = f"{para}\n\n{para}\n\n{para}"
    chunks = split_message(text, hard_limit=300)
    assert "".join(_strip_markers(chunks)) == text


# ───────────────────────── config: .env loading ────────────────────────

@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ConfigPaths.config_dir() at a tmp dir and scrub env vars."""
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
        "OPENCLOSE_DELIVER_MESSAGE_CHUNK_DELAY_MS",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key in list(os.environ):
        if key.startswith("OPENCLOSE_CHANNEL_"):
            monkeypatch.delenv(key, raising=False)
    yield tmp_path
    reset_env_cache()


def test_config_loads_from_env_file(config_dir: Path) -> None:
    (config_dir / ".env").write_text(
        "OPENCLOSE_TELEGRAM_BOT_TOKEN=tg_token_123\n"
        "OPENCLOSE_DISCORD_BOT_TOKEN=dc_token_456\n"
        "OPENCLOSE_CHANNEL_OPS=telegram:-1001234567\n"
        "OPENCLOSE_CHANNEL_TEAM=discord:987654321\n"
    )
    cfg = load_messaging_config()
    assert cfg.telegram_token == "tg_token_123"
    assert cfg.discord_token == "dc_token_456"
    assert cfg.channels["ops"] == ChannelSpec(
        alias="ops", platform="telegram", target_id="-1001234567"
    )
    assert cfg.channels["team"] == ChannelSpec(
        alias="team", platform="discord", target_id="987654321"
    )


def test_config_real_env_overrides_file(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (config_dir / ".env").write_text(
        "OPENCLOSE_TELEGRAM_BOT_TOKEN=from_file\n"
    )
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_BOT_TOKEN", "from_real_env")
    cfg = load_messaging_config()
    assert cfg.telegram_token == "from_real_env"


def test_config_handles_missing_env_file(config_dir: Path) -> None:
    # No .env file written — should return empty config.
    cfg = load_messaging_config()
    assert cfg.telegram_token is None
    assert cfg.discord_token is None
    assert cfg.channels == {}


def test_config_alias_is_lowercased(config_dir: Path) -> None:
    (config_dir / ".env").write_text(
        "OPENCLOSE_CHANNEL_MixedCase=telegram:123\n"
    )
    cfg = load_messaging_config()
    assert "mixedcase" in cfg.channels
    assert cfg.channels["mixedcase"].target_id == "123"


def test_config_malformed_channel_skipped(
    config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (config_dir / ".env").write_text(
        "OPENCLOSE_CHANNEL_NOCOLON=just_a_value\n"
        "OPENCLOSE_CHANNEL_BADPLATFORM=slack:123\n"
        "OPENCLOSE_CHANNEL_EMPTYID=telegram:\n"
        "OPENCLOSE_CHANNEL_GOOD=telegram:123\n"
    )
    cfg = load_messaging_config()
    assert set(cfg.channels) == {"good"}


# ───────────────────────── config: resolve_channels ────────────────────

def test_resolve_channels_returns_specs_and_unknown() -> None:
    cfg = _make_cfg(
        {
            "ops": ChannelSpec("ops", "telegram", "-100"),
            "team": ChannelSpec("team", "discord", "42"),
        }
    )
    resolved, unknown = resolve_channels(cfg, ["ops", "missing", "team"])
    assert [s.alias for s in resolved] == ["ops", "team"]
    assert unknown == ["missing"]


def test_resolve_channels_case_insensitive() -> None:
    cfg = _make_cfg({"ops": ChannelSpec("ops", "telegram", "-100")})
    resolved, unknown = resolve_channels(cfg, ["OPS", "Ops"])
    # Duplicates after lowercasing are deduplicated.
    assert len(resolved) == 1
    assert unknown == []


def test_resolve_channels_empty_input() -> None:
    cfg = _make_cfg({"ops": ChannelSpec("ops", "telegram", "-100")})
    resolved, unknown = resolve_channels(cfg, [])
    assert resolved == []
    assert unknown == []


def _make_cfg(
    channels: dict[str, ChannelSpec],
    telegram_allowed_users: frozenset[str] | None = None,
) -> Any:
    from openclose.tool.tools.deliver_message.config import MessagingConfig
    return MessagingConfig(
        telegram_token="tg_tok",
        discord_token="dc_tok",
        channels=channels,
        telegram_allowed_users=telegram_allowed_users,
    )


# ───────────────────────── config: TELEGRAM_ALLOWED_USERS ──────────────

def test_config_allowed_users_unset_means_no_restriction(
    config_dir: Path,
) -> None:
    (config_dir / ".env").write_text(
        "OPENCLOSE_TELEGRAM_BOT_TOKEN=tg\n"
    )
    cfg = load_messaging_config()
    assert cfg.telegram_allowed_users is None


def test_config_allowed_users_parsed_as_set(config_dir: Path) -> None:
    (config_dir / ".env").write_text(
        "OPENCLOSE_TELEGRAM_BOT_TOKEN=tg\n"
        "OPENCLOSE_TELEGRAM_ALLOWED_USERS=111, 222 ,-1001234\n"
    )
    cfg = load_messaging_config()
    assert cfg.telegram_allowed_users == frozenset({"111", "222", "-1001234"})


def test_config_allowed_users_empty_string_is_unrestricted(
    config_dir: Path,
) -> None:
    (config_dir / ".env").write_text(
        "OPENCLOSE_TELEGRAM_BOT_TOKEN=tg\n"
        "OPENCLOSE_TELEGRAM_ALLOWED_USERS=\n"
    )
    cfg = load_messaging_config()
    assert cfg.telegram_allowed_users is None


def test_is_target_allowed_discord_never_gated() -> None:
    cfg = _make_cfg(
        {"team": ChannelSpec("team", "discord", "123")},
        telegram_allowed_users=frozenset({"99"}),
    )
    assert cfg.is_target_allowed(cfg.channels["team"]) is True


def test_is_target_allowed_telegram_in_list() -> None:
    cfg = _make_cfg(
        {"ops": ChannelSpec("ops", "telegram", "-100")},
        telegram_allowed_users=frozenset({"-100"}),
    )
    assert cfg.is_target_allowed(cfg.channels["ops"]) is True


def test_is_target_allowed_telegram_not_in_list() -> None:
    cfg = _make_cfg(
        {"ops": ChannelSpec("ops", "telegram", "-100")},
        telegram_allowed_users=frozenset({"-999"}),
    )
    assert cfg.is_target_allowed(cfg.channels["ops"]) is False


# ───────────────────────── telegram.send ───────────────────────────────

@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make asyncio.sleep a no-op so rate-limit tests run instantly."""
    import asyncio

    async def _nap(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _nap)


@pytest.mark.asyncio
@respx.mock
async def test_telegram_send_success() -> None:
    route = respx.post(
        "https://api.telegram.org/bottoken123/sendMessage"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": -1}}},
        )
    )
    async with httpx.AsyncClient() as client:
        result = await tg_sender.send(
            client, "token123", "-1001", "hello", markdown=False
        )
    assert result.ok
    assert result.message_id == "42"
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_telegram_send_markdown_parse_mode() -> None:
    captured: dict[str, Any] = {}

    def _respond(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1}}
        )

    respx.post(
        "https://api.telegram.org/bottoken123/sendMessage"
    ).mock(side_effect=_respond)

    async with httpx.AsyncClient() as client:
        await tg_sender.send(
            client, "token123", "-1001", "**hi**", markdown=True
        )
    assert captured["body"]["parse_mode"] == "Markdown"


@pytest.mark.asyncio
@respx.mock
async def test_telegram_send_429_then_success(no_sleep: None) -> None:
    route = respx.post(
        "https://api.telegram.org/bottoken/sendMessage"
    ).mock(
        side_effect=[
            httpx.Response(
                429,
                json={"ok": False, "parameters": {"retry_after": 1}},
            ),
            httpx.Response(
                200, json={"ok": True, "result": {"message_id": 7}}
            ),
        ]
    )
    async with httpx.AsyncClient() as client:
        result = await tg_sender.send(
            client, "token", "-1", "hi", markdown=False
        )
    assert result.ok
    assert result.message_id == "7"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_telegram_send_429_exhausts_budget(no_sleep: None) -> None:
    respx.post(
        "https://api.telegram.org/bottoken/sendMessage"
    ).mock(
        side_effect=[
            httpx.Response(
                429,
                json={"ok": False, "parameters": {"retry_after": 0}},
            ),
            httpx.Response(
                429,
                json={"ok": False, "parameters": {"retry_after": 0}},
            ),
        ]
    )
    async with httpx.AsyncClient() as client:
        result = await tg_sender.send(
            client, "token", "-1", "hi", markdown=False
        )
    assert not result.ok


@pytest.mark.asyncio
@respx.mock
async def test_telegram_send_403_no_retry() -> None:
    route = respx.post(
        "https://api.telegram.org/bottoken/sendMessage"
    ).mock(
        return_value=httpx.Response(
            403,
            json={
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot was kicked from the chat",
            },
        )
    )
    async with httpx.AsyncClient() as client:
        result = await tg_sender.send(
            client, "token", "-1", "hi", markdown=False
        )
    assert not result.ok
    assert "kicked" in result.error
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_telegram_token_redacted_in_errors() -> None:
    respx.post(
        "https://api.telegram.org/botsecret_tok/sendMessage"
    ).mock(
        return_value=httpx.Response(
            400,
            json={
                "ok": False,
                "description": "Bad Request: token secret_tok is junk",
            },
        )
    )
    async with httpx.AsyncClient() as client:
        result = await tg_sender.send(
            client, "secret_tok", "-1", "hi", markdown=False
        )
    assert "secret_tok" not in result.error
    assert "<redacted>" in result.error


# ───────────────────────── discord.send ────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_discord_send_success() -> None:
    route = respx.post(
        "https://discord.com/api/v10/channels/123/messages"
    ).mock(
        return_value=httpx.Response(
            201, json={"id": "987654", "content": "hi"}
        )
    )
    async with httpx.AsyncClient() as client:
        result = await dc_sender.send(
            client, "bot_tok", "123", "hi", markdown=False
        )
    assert result.ok
    assert result.message_id == "987654"
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_discord_send_authorization_header() -> None:
    captured: dict[str, Any] = {}

    def _respond(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(201, json={"id": "1"})

    respx.post(
        "https://discord.com/api/v10/channels/123/messages"
    ).mock(side_effect=_respond)

    async with httpx.AsyncClient() as client:
        await dc_sender.send(client, "mytoken", "123", "hi", markdown=False)
    assert captured["authorization"] == "Bot mytoken"


@pytest.mark.asyncio
@respx.mock
async def test_discord_send_429_then_success(no_sleep: None) -> None:
    route = respx.post(
        "https://discord.com/api/v10/channels/123/messages"
    ).mock(
        side_effect=[
            httpx.Response(429, json={"retry_after": 0.1}),
            httpx.Response(201, json={"id": "77"}),
        ]
    )
    async with httpx.AsyncClient() as client:
        result = await dc_sender.send(
            client, "tok", "123", "hi", markdown=False
        )
    assert result.ok
    assert result.message_id == "77"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_discord_send_403_no_retry() -> None:
    route = respx.post(
        "https://discord.com/api/v10/channels/123/messages"
    ).mock(
        return_value=httpx.Response(
            403,
            json={"code": 50001, "message": "Missing Access"},
        )
    )
    async with httpx.AsyncClient() as client:
        result = await dc_sender.send(
            client, "tok", "123", "hi", markdown=False
        )
    assert not result.ok
    assert "Missing Access" in result.error
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_discord_token_redacted_in_errors() -> None:
    respx.post(
        "https://discord.com/api/v10/channels/123/messages"
    ).mock(
        return_value=httpx.Response(
            400, json={"message": "Invalid token secret_xyz"}
        )
    )
    async with httpx.AsyncClient() as client:
        result = await dc_sender.send(
            client, "secret_xyz", "123", "hi", markdown=False
        )
    assert "secret_xyz" not in result.error
    assert "<redacted>" in result.error


# ───────────────────────── tool: end-to-end execute ────────────────────

@pytest.fixture
def fake_env(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: None
) -> Iterator[None]:
    """Populate a plausible .env file and zero chunk delay."""
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_BOT_TOKEN", "tg_tok")
    monkeypatch.setenv("OPENCLOSE_DISCORD_BOT_TOKEN", "dc_tok")
    monkeypatch.setenv("OPENCLOSE_CHANNEL_OPS", "telegram:-100")
    monkeypatch.setenv("OPENCLOSE_CHANNEL_TEAM", "discord:42")
    monkeypatch.setenv("OPENCLOSE_DELIVER_MESSAGE_CHUNK_DELAY_MS", "0")
    yield


@pytest.mark.asyncio
@respx.mock
async def test_execute_happy_path_telegram(fake_env: None) -> None:
    route = respx.post(
        "https://api.telegram.org/bottg_tok/sendMessage"
    ).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 101}}
        )
    )
    tool = make_deliver_message_tool()
    result = await tool.execute(channels=["ops"], message="hello")
    assert result.ok
    assert "1/1 channels" in result.output
    assert route.call_count == 1
    assert result.metadata["channels"][0]["message_ids"] == ["101"]


@pytest.mark.asyncio
@respx.mock
async def test_execute_splits_long_message_for_discord(fake_env: None) -> None:
    """A long message to Discord should produce multiple POSTs, each
    under 2000 chars."""
    route = respx.post(
        "https://discord.com/api/v10/channels/42/messages"
    ).mock(
        side_effect=lambda req: httpx.Response(201, json={"id": "x"})
    )
    long_message = ("paragraph of text. " * 300)  # ~5700 chars
    tool = make_deliver_message_tool()
    result = await tool.execute(channels=["team"], message=long_message)
    assert result.ok
    assert route.call_count >= 2
    for call in route.calls:
        import json

        body = json.loads(call.request.content)
        assert len(body["content"]) <= 2000
    assert result.metadata["total_chunks"] >= 2


@pytest.mark.asyncio
@respx.mock
async def test_execute_multi_channel_partial_failure(fake_env: None) -> None:
    respx.post(
        "https://api.telegram.org/bottg_tok/sendMessage"
    ).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1}}
        )
    )
    respx.post(
        "https://discord.com/api/v10/channels/42/messages"
    ).mock(
        return_value=httpx.Response(
            403, json={"message": "Missing Access"}
        )
    )
    tool = make_deliver_message_tool()
    result = await tool.execute(
        channels=["ops", "team"], message="hi"
    )
    assert not result.ok
    assert "Partial delivery" in result.error
    assert "1/2 failed" in result.error
    meta_channels = {c["alias"]: c for c in result.metadata["channels"]}
    assert meta_channels["ops"]["status"] == "ok"
    assert meta_channels["team"]["status"] == "error"
    assert "Missing Access" in meta_channels["team"]["error"]


@pytest.mark.asyncio
async def test_execute_unknown_alias_errors_before_http(
    fake_env: None,
) -> None:
    tool = make_deliver_message_tool()
    result = await tool.execute(channels=["nope"], message="hi")
    assert not result.ok
    assert "Unknown channel" in result.error


@pytest.mark.asyncio
async def test_execute_message_too_long_rejected(fake_env: None) -> None:
    tool = make_deliver_message_tool()
    result = await tool.execute(
        channels=["ops"], message="x" * 200_000
    )
    assert not result.ok
    assert "too long" in result.error.lower()


@pytest.mark.asyncio
async def test_execute_empty_message_rejected(fake_env: None) -> None:
    tool = make_deliver_message_tool()
    result = await tool.execute(channels=["ops"], message="")
    assert not result.ok


@pytest.mark.asyncio
async def test_execute_empty_channels_rejected(fake_env: None) -> None:
    tool = make_deliver_message_tool()
    result = await tool.execute(channels=[], message="hi")
    assert not result.ok


@pytest.mark.asyncio
async def test_execute_too_many_channels_rejected(fake_env: None) -> None:
    tool = make_deliver_message_tool()
    result = await tool.execute(
        channels=["c"] * 20, message="hi"
    )
    assert not result.ok
    assert "Too many" in result.error


@pytest.mark.asyncio
async def test_execute_missing_token_rejected(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: None
) -> None:
    monkeypatch.setenv("OPENCLOSE_CHANNEL_OPS", "telegram:-100")
    # Intentionally no OPENCLOSE_TELEGRAM_BOT_TOKEN
    tool = make_deliver_message_tool()
    result = await tool.execute(channels=["ops"], message="hi")
    assert not result.ok
    assert "telegram" in result.error.lower()


def test_tool_schema_has_required_fields() -> None:
    tool = make_deliver_message_tool()
    schema = tool.to_schema()
    assert schema["name"] == "deliver_message"
    required = schema["parameters"]["required"]
    assert "channels" in required
    assert "message" in required
    assert "format" not in required
    assert "title" not in required
    props = schema["parameters"]["properties"]
    assert props["channels"]["type"] == "array"
    assert props["channels"]["items"] == {"type": "string"}
    assert props["format"]["enum"] == ["plain", "markdown"]


def test_tool_description_lists_configured_aliases(fake_env: None) -> None:
    tool = make_deliver_message_tool()
    # The aliases should appear in BOTH the tool description and the
    # channels parameter description so the LLM sees them regardless of
    # which field it reads.
    assert "ops (telegram)" in tool.description
    assert "team (discord)" in tool.description
    schema = tool.to_schema()
    assert "ops (telegram)" in schema["parameters"]["properties"]["channels"]["description"]


def test_tool_description_indicates_no_aliases_when_empty(
    config_dir: Path,
) -> None:
    tool = make_deliver_message_tool()
    assert "No channel aliases are configured" in tool.description


@pytest.mark.asyncio
async def test_unknown_alias_error_lists_available(
    fake_env: None,
) -> None:
    tool = make_deliver_message_tool()
    result = await tool.execute(channels=["nope"], message="hi")
    assert not result.ok
    assert "Unknown channel" in result.error
    assert "Available aliases:" in result.error
    assert "ops" in result.error
    assert "team" in result.error


# ───────────────────────── registration + agent denial ────────────────

def test_deliver_message_is_registered() -> None:
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tools import register_all_tools

    registry = ToolRegistry()
    register_all_tools(registry, ".")
    assert registry.get("deliver_message") is not None


def test_plan_agent_denies_deliver_message() -> None:
    from openclose.config.agents import _BUILTIN_AGENT_DEFAULTS

    denied = _BUILTIN_AGENT_DEFAULTS["plan"]["denied_tools"]
    assert "deliver_message" in denied


# ───── execute: TELEGRAM_ALLOWED_USERS enforcement ─────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_execute_telegram_allowlist_permits_listed_target(
    fake_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_ALLOWED_USERS", "-100, 999")
    respx.post(
        "https://api.telegram.org/bottg_tok/sendMessage"
    ).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1}}
        )
    )
    tool = make_deliver_message_tool()
    result = await tool.execute(channels=["ops"], message="hi")
    assert result.ok


@pytest.mark.asyncio
@respx.mock
async def test_execute_telegram_allowlist_blocks_unlisted_target(
    fake_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_ALLOWED_USERS", "999")
    route = respx.post(
        "https://api.telegram.org/bottg_tok/sendMessage"
    ).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1}}
        )
    )
    tool = make_deliver_message_tool()
    result = await tool.execute(channels=["ops"], message="hi")
    assert not result.ok
    assert "OPENCLOSE_TELEGRAM_ALLOWED_USERS" in result.error
    assert "ops" in result.error
    # No HTTP was sent because the allowlist gate short-circuits.
    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_execute_telegram_allowlist_does_not_affect_discord(
    fake_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A restrictive Telegram allowlist must not block Discord deliveries.
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_ALLOWED_USERS", "999")
    route = respx.post(
        "https://discord.com/api/v10/channels/42/messages"
    ).mock(return_value=httpx.Response(201, json={"id": "d1"}))
    tool = make_deliver_message_tool()
    result = await tool.execute(channels=["team"], message="hi")
    assert result.ok
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_execute_mixed_allowed_and_blocked_telegram(
    fake_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both telegram aliases; allowlist permits only one.
    monkeypatch.setenv("OPENCLOSE_CHANNEL_OTHER", "telegram:-999")
    monkeypatch.setenv("OPENCLOSE_TELEGRAM_ALLOWED_USERS", "-100")
    tg_route = respx.post(
        "https://api.telegram.org/bottg_tok/sendMessage"
    ).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1}}
        )
    )
    tool = make_deliver_message_tool()
    result = await tool.execute(
        channels=["ops", "other"], message="hi"
    )
    # The whole call is refused; no HTTP fired.
    assert not result.ok
    assert "other" in result.error
    assert "ops" not in result.error  # only the blocked one is listed
    assert tg_route.call_count == 0
