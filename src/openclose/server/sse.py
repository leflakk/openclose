"""Server-Sent Events helper for streaming agent responses."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openclose.agent.loop import StreamEvent


async def stream_events(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    """Convert StreamEvents to SSE data strings."""
    try:
        async for event in events:
            data: dict[str, Any] = {"type": event.type}

            if event.content:
                data["content"] = event.content
            if event.tool_call:
                data["tool_call_id"] = event.tool_call.id
                data["tool_name"] = event.tool_call.name
                data["tool_args"] = event.tool_call.arguments
            if event.tool_result:
                data["tool_result"] = event.tool_result
            if event.error:
                data["error"] = event.error
            if event.done:
                data["done"] = True
            if event.context_info:
                data["context_info"] = event.context_info
            if event.metadata:
                data["metadata"] = event.metadata
            if event.parent_tool_call_id:
                data["parent_tool_call_id"] = event.parent_tool_call_id
            if event.message_id:
                data["message_id"] = event.message_id
            if event.part_id:
                data["part_id"] = event.part_id

            yield f"data: {json.dumps(data)}\n\n"
    except Exception as e:
        error_data = {"type": "error", "error": str(e)}
        yield f"data: {json.dumps(error_data)}\n\n"
