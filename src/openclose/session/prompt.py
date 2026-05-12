"""Session prompt builder — assembles context for the LLM."""

from __future__ import annotations

from openclose.session.session import SessionManager
from openclose.log import get_logger

log = get_logger(__name__)


def build_message_history(
    session_manager: SessionManager,
    session_id: str,
) -> list[dict[str, str]]:
    """Build a message history from stored messages for LLM context.

    Returns list of dicts with 'role' and 'content' keys.
    """
    messages = session_manager.get_messages(session_id)
    history: list[dict[str, str]] = []
    for msg in messages:
        history.append({"role": msg.role, "content": msg.content})
    return history
