"""Discord Bot API sender for the deliver_message tool.

Sends a single chunk via ``POST /channels/{id}/messages``. Handles 429
rate limits with one capped retry, surfaces auth failures clearly, and
redacts the bot token from every surfaced error string.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from openclose.log import get_logger

log = get_logger(__name__)


API_BASE = "https://discord.com/api/v10"
HARD_LIMIT = 2000
"""Discord ``create_message`` caps ``content`` at 2000 characters."""

_MAX_RETRY_AFTER = 30.0
_USER_AGENT = "openclose-deliver-message/1.0"


@dataclass
class SendOutcome:
    """Result of a single ``send()`` call."""

    ok: bool
    message_id: str | None
    error: str


async def send(
    client: httpx.AsyncClient,
    token: str,
    target_id: str,
    chunk: str,
    *,
    markdown: bool,  # noqa: ARG001 — Discord always renders markdown
    retry_budget: int = 1,
) -> SendOutcome:
    """Send ``chunk`` to the channel identified by ``target_id``.

    Discord parses markdown natively; the ``markdown`` flag is accepted
    for API symmetry with ``telegram.send`` but has no effect.
    """
    url = f"{API_BASE}/channels/{target_id}/messages"
    payload = {"content": chunk}
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
    }

    attempt = 0
    while True:
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.RequestError as e:
            return SendOutcome(False, None, _redact(f"network error: {e}", token))

        if 200 <= response.status_code < 300:
            try:
                body = response.json()
            except ValueError:
                return SendOutcome(False, None, "invalid JSON in discord response")
            message_id = body.get("id")
            return SendOutcome(True, str(message_id) if message_id else None, "")

        if response.status_code == 429 and attempt < retry_budget:
            retry_after = _extract_retry_after(response)
            log.debug("discord 429; sleeping %.2fs before retry", retry_after)
            await asyncio.sleep(min(retry_after, _MAX_RETRY_AFTER))
            attempt += 1
            continue

        desc = _extract_error_message(response) or f"HTTP {response.status_code}"
        return SendOutcome(False, None, _redact(desc, token))


def _extract_retry_after(response: httpx.Response) -> float:
    try:
        body = response.json()
    except ValueError:
        header = response.headers.get("retry-after")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return 1.0
    val = body.get("retry_after")
    if isinstance(val, (int, float)) and val >= 0:
        return float(val)
    return 1.0


def _extract_error_message(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    msg = body.get("message")
    return str(msg) if msg else None


def _redact(text: str, token: str) -> str:
    if token and token in text:
        return text.replace(token, "<redacted>")
    return text
