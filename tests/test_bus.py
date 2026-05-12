"""Tests for the async event bus."""

from __future__ import annotations


import pytest

from openclose.bus.bus import EventBus


@pytest.mark.asyncio
async def test_emit_and_listen() -> None:
    """Listeners should receive emitted events."""
    bus = EventBus()
    received: list[str] = []

    async def handler(name: str) -> None:
        received.append(name)

    bus.on("test.event", handler)
    await bus.emit("test.event", name="hello")
    assert received == ["hello"]


@pytest.mark.asyncio
async def test_multiple_listeners() -> None:
    """Multiple listeners should all be called."""
    bus = EventBus()
    calls: list[int] = []

    async def handler1(**kwargs: object) -> None:
        calls.append(1)

    async def handler2(**kwargs: object) -> None:
        calls.append(2)

    bus.on("multi", handler1)
    bus.on("multi", handler2)
    await bus.emit("multi")
    assert sorted(calls) == [1, 2]


@pytest.mark.asyncio
async def test_unsubscribe() -> None:
    """Unsubscribe function should remove the listener."""
    bus = EventBus()
    calls: list[int] = []

    async def handler(**kwargs: object) -> None:
        calls.append(1)

    unsub = bus.on("unsub.test", handler)
    await bus.emit("unsub.test")
    assert len(calls) == 1

    unsub()
    await bus.emit("unsub.test")
    assert len(calls) == 1  # no new call


@pytest.mark.asyncio
async def test_error_in_listener_does_not_propagate() -> None:
    """Errors in listeners should be logged, not raised."""
    bus = EventBus()
    calls: list[int] = []

    async def bad_handler(**kwargs: object) -> None:
        raise ValueError("boom")

    async def good_handler(**kwargs: object) -> None:
        calls.append(1)

    bus.on("err", bad_handler)
    bus.on("err", good_handler)
    await bus.emit("err")  # should not raise
    assert calls == [1]


@pytest.mark.asyncio
async def test_clear_all() -> None:
    """Clear should remove all listeners."""
    bus = EventBus()

    async def handler(**kwargs: object) -> None:
        pass

    bus.on("a", handler)
    bus.on("b", handler)
    bus.clear()
    assert len(bus._listeners) == 0


@pytest.mark.asyncio
async def test_clear_specific_event() -> None:
    """Clear with event name should only remove that event's listeners."""
    bus = EventBus()

    async def handler(**kwargs: object) -> None:
        pass

    bus.on("keep", handler)
    bus.on("remove", handler)
    bus.clear("remove")
    assert "keep" in bus._listeners
    assert "remove" not in bus._listeners


@pytest.mark.asyncio
async def test_emit_no_listeners() -> None:
    """Emitting with no listeners should not error."""
    bus = EventBus()
    await bus.emit("nobody.listening", data="test")
