"""Telegram Bot API sender for the deliver_message tool.

Sends a single chunk via ``POST /bot<token>/sendMessage``. Handles 429
rate limits with one capped retry, surfaces auth failures clearly, and
redacts the bot token from every surfaced error string.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from openclose.log import get_logger

log = get_logger(__name__)


API_BASE = "https://api.telegram.org"
HARD_LIMIT = 4096
"""Telegram ``sendMessage`` caps the ``text`` field at 4096 characters."""

_MAX_RETRY_AFTER = 30.0


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
    markdown: bool,
    retry_budget: int = 1,
) -> SendOutcome:
    """Send ``chunk`` to the chat identified by ``target_id``.

    ``markdown=True`` uses legacy ``parse_mode=Markdown`` (forgiving of
    unescaped punctuation; widely supported). For strict MarkdownV2 or
    HTML users should pre-format their text.
    """
    url = f"{API_BASE}/bot{token}/sendMessage"
    payload: dict[str, object] = {
        "chat_id": target_id,
        "text": chunk,
        "disable_web_page_preview": True,
    }
    if markdown:
        payload["parse_mode"] = "Markdown"

    attempt = 0
    while True:
        try:
            response = await client.post(url, json=payload)
        except httpx.RequestError as e:
            return SendOutcome(False, None, _redact(f"network error: {e}", token))

        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                return SendOutcome(False, None, "invalid JSON in telegram response")
            if not body.get("ok"):
                desc = str(body.get("description", "unknown error"))
                return SendOutcome(False, None, _redact(desc, token))
            message_id = body.get("result", {}).get("message_id")
            return SendOutcome(True, str(message_id) if message_id else None, "")

        if response.status_code == 429 and attempt < retry_budget:
            retry_after = _extract_retry_after(response)
            log.debug("telegram 429; sleeping %.1fs before retry", retry_after)
            await asyncio.sleep(min(retry_after, _MAX_RETRY_AFTER))
            attempt += 1
            continue

        # Non-retryable error. Extract description without leaking token.
        desc = _extract_error_description(response) or f"HTTP {response.status_code}"
        return SendOutcome(False, None, _redact(desc, token))


def _extract_retry_after(response: httpx.Response) -> float:
    """Return the retry_after value from a Telegram 429 response."""
    try:
        body = response.json()
    except ValueError:
        return 1.0
    params = body.get("parameters", {})
    if isinstance(params, dict):
        val = params.get("retry_after")
        if isinstance(val, (int, float)) and val >= 0:
            return float(val)
    return 1.0


def _extract_error_description(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    desc = body.get("description")
    return str(desc) if desc else None


def _redact(text: str, token: str) -> str:
    if token and token in text:
        return text.replace(token, "<redacted>")
    return text
