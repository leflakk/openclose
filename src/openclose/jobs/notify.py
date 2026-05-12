"""Job notification delivery — reuses the deliver_message HTTP senders directly.

Kept intentionally small: a single `send_job_notification(alias, text)` that
resolves the alias, picks the right platform, calls the corresponding sender,
and returns `(ok, error)`. No message-chunking — job notifications are short
by construction and we hard-cap at the Discord 2000-char limit.
"""

from __future__ import annotations

import httpx

from openclose.log import get_logger
from openclose.tool.tools.deliver_message.config import (
    ChannelSpec,
    load_messaging_config,
    resolve_channels,
)
from openclose.tool.tools.deliver_message import telegram as tg
from openclose.tool.tools.deliver_message import discord as dc

log = get_logger(__name__)

# Universal hard cap — Discord is the stricter of the two platforms.
_MESSAGE_CAP = 2000
_HTTP_TIMEOUT = 10.0


def list_channel_aliases() -> list[dict[str, str]]:
    """Return all configured aliases as `[{alias, platform}]`, alias-sorted."""
    cfg = load_messaging_config()
    out = [
        {"alias": spec.alias, "platform": spec.platform}
        for spec in cfg.channels.values()
    ]
    out.sort(key=lambda d: d["alias"])
    return out


def _truncate(text: str) -> str:
    if len(text) <= _MESSAGE_CAP:
        return text
    return text[: _MESSAGE_CAP - 20].rstrip() + "\n…[truncated]"


async def send_job_notification(
    alias: str,
    text: str,
    *,
    markdown: bool = False,
) -> tuple[bool, str]:
    """Send `text` to the configured channel `alias`. Returns `(ok, error_reason)`."""
    cfg = load_messaging_config()
    resolved, unknown = resolve_channels(cfg, [alias])
    if unknown or not resolved:
        return False, f"Unknown channel alias: {alias!r}"

    spec: ChannelSpec = resolved[0]
    token = cfg.token_for(spec.platform)
    if not token:
        return False, f"No bot token configured for platform {spec.platform!r}"

    if not cfg.is_target_allowed(spec):
        return False, f"Channel {alias!r} is not in the Telegram allowlist"

    chunk = _truncate(text)

    outcome: tg.SendOutcome | dc.SendOutcome
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        if spec.platform == "telegram":
            outcome = await tg.send(client, token, spec.target_id, chunk, markdown=markdown)
        elif spec.platform == "discord":
            outcome = await dc.send(client, token, spec.target_id, chunk, markdown=markdown)
        else:
            return False, f"Unsupported platform {spec.platform!r}"

    return (outcome.ok, "" if outcome.ok else outcome.error)
