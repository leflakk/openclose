"""Tests for SSE streaming."""

from __future__ import annotations

import json
from typing import AsyncIterator

import pytest

from openclose.agent.loop import StreamEvent, ToolCall
from openclose.server.sse import stream_events


async def _make_events() -> AsyncIterator[StreamEvent]:
    yield StreamEvent("text", content="Hello ")
    yield StreamEvent("text", content="world")
    tc = ToolCall()
    tc.name = "read"
    tc.id = "call_1"
    yield StreamEvent("tool_call", tool_call=tc)
    yield StreamEvent("tool_result", tool_result="file content", tool_call=tc)
    yield StreamEvent("error", error="test error")
    yield StreamEvent("done", done=True)


@pytest.mark.asyncio
async def test_stream_events() -> None:
    events: list[str] = []
    async for chunk in stream_events(_make_events()):
        events.append(chunk)

    assert len(events) == 6

    # Check text event
    data = json.loads(events[0].removeprefix("data: ").strip())
    assert data["type"] == "text"
    assert data["content"] == "Hello "

    # Check tool call
    data = json.loads(events[2].removeprefix("data: ").strip())
    assert data["type"] == "tool_call"
    assert data["tool_name"] == "read"

    # Check tool result
    data = json.loads(events[3].removeprefix("data: ").strip())
    assert data["type"] == "tool_result"

    # Check error
    data = json.loads(events[4].removeprefix("data: ").strip())
    assert data["error"] == "test error"

    # Check done
    data = json.loads(events[5].removeprefix("data: ").strip())
    assert data["done"] is True
