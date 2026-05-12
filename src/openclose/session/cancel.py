"""Per-session cancellation registry for interrupting LLM generation."""

from __future__ import annotations

import asyncio

from openclose.log import get_logger

log = get_logger(__name__)


class CancelRegistry:
    """Maps session_id to an asyncio.Event used to signal cancellation."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def register(self, session_id: str) -> asyncio.Event:
        """Create or reset a cancel event for a session."""
        event = asyncio.Event()
        self._events[session_id] = event
        return event

    def cancel(self, session_id: str) -> bool:
        """Set the cancel event, returning True if a session was active."""
        event = self._events.get(session_id)
        if event is None:
            return False
        event.set()
        log.info("Cancelled generation for session %s", session_id)
        return True

    def unregister(self, session_id: str) -> None:
        """Remove the cancel event when processing finishes."""
        self._events.pop(session_id, None)

    def is_cancelled(self, session_id: str) -> bool:
        event = self._events.get(session_id)
        return event is not None and event.is_set()


_registry: CancelRegistry | None = None


def get_cancel_registry() -> CancelRegistry:
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = CancelRegistry()
    return _registry
