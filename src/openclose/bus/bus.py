"""Async event bus — publish/subscribe pattern for domain events."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Coroutine

from openclose.log import get_logger

log = get_logger(__name__)

Listener = Callable[..., Coroutine[Any, Any, None]]


class EventBus:
    """Simple async event bus.

    Usage:
        bus = EventBus()
        bus.on("session.created", my_handler)
        await bus.emit("session.created", session_id="abc")
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = defaultdict(list)

    def on(self, event: str, listener: Listener) -> Callable[[], None]:
        """Subscribe to an event. Returns an unsubscribe function."""
        self._listeners[event].append(listener)

        def unsubscribe() -> None:
            self._listeners[event].remove(listener)

        return unsubscribe

    async def emit(self, event: str, **kwargs: Any) -> None:
        """Emit an event to all subscribers. Errors in listeners are logged, not raised."""
        listeners = self._listeners.get(event, [])
        for listener in listeners:
            try:
                await listener(**kwargs)
            except Exception:
                log.exception("Error in event listener for %r", event)

    def clear(self, event: str | None = None) -> None:
        """Remove all listeners, or listeners for a specific event."""
        if event is None:
            self._listeners.clear()
        else:
            self._listeners.pop(event, None)


_bus: EventBus | None = None


def get_bus() -> EventBus:
    """Get the global event bus singleton."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
