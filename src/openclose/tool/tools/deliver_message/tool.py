"""Factory and orchestrator for the ``deliver_message`` tool."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from openclose.log import get_logger
from openclose.tool.tool import Tool, ToolParameter, ToolResult
from openclose.tool.tools.deliver_message import discord as dc_sender
from openclose.tool.tools.deliver_message import telegram as tg_sender
from openclose.tool.tools.deliver_message.config import (
    ChannelSpec,
    load_messaging_config,
    resolve_channels,
)
from openclose.tool.tools.deliver_message.splitter import split_message

log = get_logger(__name__)


MAX_CHANNELS = 10
MAX_MESSAGE_CHARS = 100_000
_HTTP_TIMEOUT = 15.0
_INTER_CHUNK_DELAY_ENV = "OPENCLOSE_DELIVER_MESSAGE_CHUNK_DELAY_MS"
_DEFAULT_INTER_CHUNK_DELAY_MS = 350


def make_deliver_message_tool() -> Tool:
    """Return the ``deliver_message`` :class:`Tool`.

    Reads the current channel aliases from ``.env`` in the openclose
    config directory at tool-creation time and lists them in the
    LLM-visible description, so the agent can pick a valid alias without
    guessing.
    """
    cfg = load_messaging_config()
    aliases_blurb = _format_aliases_for_prompt(cfg)
    return Tool(
        name="deliver_message",
        description=(
            "Send a text message to one or more pre-configured channels "
            "(Telegram or Discord bots). "
            f"{aliases_blurb} "
            "Long messages are automatically split to respect each "
            "platform's length limits (Telegram 4096, Discord 2000); "
            "fenced code blocks are preserved across chunks."
        ),
        parameters=[
            ToolParameter(
                name="channels",
                type="array",
                description=(
                    "One or more channel aliases (max 10). "
                    f"{aliases_blurb}"
                ),
                items={"type": "string"},
            ),
            ToolParameter(
                name="message",
                description=(
                    "Message body (max 100000 characters).  Fenced "
                    "code blocks (```...```) are preserved."
                ),
            ),
            ToolParameter(
                name="format",
                description=(
                    "'plain' (default) sends as-is; 'markdown' uses "
                    "Telegram legacy Markdown and Discord native "
                    "markdown."
                ),
                required=False,
                default="plain",
                enum=["plain", "markdown"],
            ),
            ToolParameter(
                name="title",
                description=(
                    "Optional title prefixed to the message "
                    "(bold in markdown mode, plain otherwise)."
                ),
                required=False,
                default="",
            ),
        ],
        execute_fn=_execute,
    )


@dataclass
class _ChannelResult:
    alias: str
    platform: str
    chunks_total: int
    chunks_sent: int
    message_ids: list[str]
    error: str

    @property
    def ok(self) -> bool:
        return not self.error and self.chunks_sent == self.chunks_total


async def _execute(
    channels: list[str] | None = None,
    message: str = "",
    format: str = "plain",
    title: str = "",
    **kwargs: object,
) -> ToolResult:
    # ── Input validation ───────────────────────────────────────────
    if not isinstance(channels, list) or not channels:
        return ToolResult(
            error="'channels' must be a non-empty list of alias strings"
        )
    if len(channels) > MAX_CHANNELS:
        return ToolResult(error=f"Too many channels (max {MAX_CHANNELS})")
    if not isinstance(message, str) or not message.strip():
        return ToolResult(error="'message' is required and must be non-empty")
    if len(message) > MAX_MESSAGE_CHARS:
        return ToolResult(
            error=f"Message too long ({len(message)} > {MAX_MESSAGE_CHARS} chars)"
        )
    fmt = format.lower() if isinstance(format, str) else "plain"
    if fmt not in ("plain", "markdown"):
        return ToolResult(error=f"Invalid format {format!r}; must be plain or markdown")

    # ── Build the full text (with optional title) ──────────────────
    full_text = _apply_title(title if isinstance(title, str) else "", message, fmt)

    # ── Resolve channels ───────────────────────────────────────────
    cfg = load_messaging_config()
    specs, unknown = resolve_channels(cfg, channels)
    if unknown:
        available = sorted(cfg.channels.keys())
        hint = (
            f" Available aliases: {', '.join(available)}."
            if available
            else " No channel aliases are configured."
        )
        return ToolResult(
            error=(
                f"Unknown channel alias(es): {', '.join(unknown)}.{hint} "
                "Aliases are defined in the .env file in your openclose "
                "config directory as OPENCLOSE_CHANNEL_<ALIAS>=<platform>:<target_id>."
            )
        )
    if not specs:
        return ToolResult(error="No channels resolved")

    # ── Verify tokens for each required platform ───────────────────
    needed_platforms = {s.platform for s in specs}
    missing_tokens = [
        p for p in needed_platforms if not cfg.token_for(p)
    ]
    if missing_tokens:
        return ToolResult(
            error=(
                "Missing bot token(s) for: "
                f"{', '.join(missing_tokens)}.  Set "
                "OPENCLOSE_TELEGRAM_BOT_TOKEN / OPENCLOSE_DISCORD_BOT_TOKEN."
            )
        )

    # ── Enforce Telegram outbound allowlist (if set) ───────────────
    blocked = [s for s in specs if not cfg.is_target_allowed(s)]
    if blocked:
        listing = ", ".join(f"{s.alias} (telegram:{s.target_id})" for s in blocked)
        return ToolResult(
            error=(
                "Telegram target(s) not in OPENCLOSE_TELEGRAM_ALLOWED_USERS: "
                f"{listing}. Add the chat_id(s) to the allowlist or remove "
                "OPENCLOSE_TELEGRAM_ALLOWED_USERS to disable gating."
            )
        )

    # ── Deliver per channel ────────────────────────────────────────
    delay_s = _inter_chunk_delay_s()
    results: list[_ChannelResult] = []

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for spec in specs:
            token = cfg.token_for(spec.platform)
            # Missing-token check already short-circuited above, but mypy
            # doesn't know that.
            assert token is not None
            r = await _send_to_channel(
                client, spec, token, full_text, markdown=(fmt == "markdown"),
                chunk_delay_s=delay_s,
            )
            results.append(r)

    return _build_result(results)


def _format_aliases_for_prompt(cfg: Any) -> str:
    """Short human-readable list of configured aliases for LLM prompts.

    Listed with platform so the LLM can pick meaningfully, e.g.::

        Available channel aliases: me (telegram), ops (telegram),
        teamchat (discord).
    """
    if not cfg.channels:
        return (
            "No channel aliases are configured yet — the user must add "
            "OPENCLOSE_CHANNEL_<ALIAS>=<platform>:<target_id> lines to "
            "the .env file in their openclose config directory before this "
            "tool can deliver."
        )
    entries = [
        f"{alias} ({spec.platform})"
        for alias, spec in sorted(cfg.channels.items())
    ]
    return f"Available channel aliases: {', '.join(entries)}."


def _apply_title(title: str, message: str, fmt: str) -> str:
    title = title.strip()
    if not title:
        return message
    if fmt == "markdown":
        return f"**{title}**\n\n{message}"
    return f"{title}\n\n{message}"


def _inter_chunk_delay_s() -> float:
    raw = os.environ.get(_INTER_CHUNK_DELAY_ENV)
    if raw:
        try:
            ms = int(raw)
            if ms >= 0:
                return ms / 1000.0
        except ValueError:
            pass
    return _DEFAULT_INTER_CHUNK_DELAY_MS / 1000.0


async def _send_to_channel(
    client: httpx.AsyncClient,
    spec: ChannelSpec,
    token: str,
    text: str,
    *,
    markdown: bool,
    chunk_delay_s: float,
) -> _ChannelResult:
    hard_limit = _hard_limit_for(spec.platform)
    try:
        chunks = split_message(text, hard_limit)
    except Exception as e:
        return _ChannelResult(
            alias=spec.alias, platform=spec.platform,
            chunks_total=0, chunks_sent=0, message_ids=[],
            error=f"splitter error: {e}",
        )

    sender = _sender_for(spec.platform)
    message_ids: list[str] = []
    sent = 0

    for i, chunk in enumerate(chunks):
        outcome = await sender(
            client, token, spec.target_id, chunk, markdown=markdown
        )
        if not outcome.ok:
            return _ChannelResult(
                alias=spec.alias, platform=spec.platform,
                chunks_total=len(chunks), chunks_sent=sent,
                message_ids=message_ids,
                error=f"chunk {i + 1}/{len(chunks)}: {outcome.error}",
            )
        if outcome.message_id is not None:
            message_ids.append(outcome.message_id)
        sent += 1
        if i + 1 < len(chunks) and chunk_delay_s > 0:
            await asyncio.sleep(chunk_delay_s)

    return _ChannelResult(
        alias=spec.alias, platform=spec.platform,
        chunks_total=len(chunks), chunks_sent=sent,
        message_ids=message_ids, error="",
    )


def _hard_limit_for(platform: str) -> int:
    if platform == "telegram":
        return tg_sender.HARD_LIMIT
    if platform == "discord":
        return dc_sender.HARD_LIMIT
    raise ValueError(f"unknown platform: {platform}")


def _sender_for(platform: str) -> Any:
    if platform == "telegram":
        return tg_sender.send
    if platform == "discord":
        return dc_sender.send
    raise ValueError(f"unknown platform: {platform}")


def _build_result(results: list[_ChannelResult]) -> ToolResult:
    n = len(results)
    n_ok = sum(1 for r in results if r.ok)
    n_failed = n - n_ok
    total_chunks = sum(r.chunks_total for r in results)

    lines = [f"Delivered to {n_ok}/{n} channels ({total_chunks} chunks total)."]
    for r in results:
        if r.ok:
            lines.append(
                f"- {r.alias} ({r.platform}): "
                f"{r.chunks_sent}/{r.chunks_total} chunks, ok"
            )
        else:
            lines.append(
                f"- {r.alias} ({r.platform}): FAILED after "
                f"{r.chunks_sent}/{r.chunks_total} chunks — {r.error}"
            )

    output = "\n".join(lines)

    if n_failed == 0:
        error = ""
    elif n_failed == n:
        error = "All channels failed"
    else:
        error = f"Partial delivery: {n_failed}/{n} failed"

    metadata: dict[str, Any] = {
        "total_chunks": total_chunks,
        "channels": [
            {
                "alias": r.alias,
                "platform": r.platform,
                "status": "ok" if r.ok else "error",
                "chunks_sent": r.chunks_sent,
                "chunks_total": r.chunks_total,
                "message_ids": list(r.message_ids),
                "error": r.error,
            }
            for r in results
        ],
    }

    return ToolResult(output=output, error=error, metadata=metadata)
