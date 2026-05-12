"""Tests for parallel tool call execution in the agent loop."""

from __future__ import annotations

import asyncio
from typing import Any
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

from openclose.agent.agent import Agent
from openclose.agent.loop import AgentLoop
from openclose.tool.tool import ToolResult


def create_mock_provider(responses: list[MagicMock]) -> tuple[MagicMock, list[Any]]:
    """Create a mock provider that yields predefined responses."""
    mock = MagicMock()
    mock.detect_model = AsyncMock(return_value="test-model")

    # Use a list iterator for the responses
    response_iter = iter(responses)

    async def mock_chat(
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[MagicMock]:
        # Yield from the iterator
        for response in response_iter:
            yield response

    mock.chat = mock_chat
    return mock, []


def create_tool_executor() -> tuple[AsyncMock, list[tuple[str, Any]]]:
    """Create a mock tool executor that tracks call order."""
    call_log: list[tuple[str, Any]] = []
    executor = AsyncMock(side_effect=lambda name, args: asyncio.sleep(0.1) or ToolResult(output=f"result:{name}"))

    async def tracked_executor(name: str, args: dict[str, Any]) -> ToolResult:
        call_log.append((name, args))
        await asyncio.sleep(0.1)  # Simulate some work
        return ToolResult(output=f"result:{name}")

    return executor, call_log


def create_chunk_with_tool_calls(
    tool_calls: list[dict[str, Any]],
    text_content: str = "",
) -> MagicMock:
    """Create a mock chunk with tool calls."""
    chunk = MagicMock()
    choice = MagicMock()
    delta = MagicMock()
    delta.content = text_content
    delta.tool_calls = None

    # Create tool call deltas
    if tool_calls:
        tc_deltas = []
        for i, tc in enumerate(tool_calls):
            tc_delta = MagicMock()
            tc_delta.index = i
            tc_delta.id = tc.get("id", f"call_{i}")
            tc_delta.function = MagicMock()
            tc_delta.function.name = tc.get("name", "")
            tc_delta.function.arguments = tc.get("arguments", "{}")
            tc_deltas.append(tc_delta)
        delta.tool_calls = tc_deltas

    choice.delta = delta
    chunk.choices = [choice]
    return chunk


def create_final_chunk() -> MagicMock:
    """Create a final empty chunk."""
    chunk = MagicMock()
    choice = MagicMock()
    delta = MagicMock()
    delta.content = None
    delta.tool_calls = None
    choice.delta = delta
    chunk.choices = [choice]
    return chunk


class TestParallelToolCalls:
    """Test whether tool calls are executed in parallel or sequentially."""

    async def test_tool_calls_execute_in_parallel(self) -> None:
        """Tool calls should execute in parallel (not sequentially)."""
        # Create agent with access to all tools
        agent = Agent(
            name="test",
            model="test-model",
            allowed_tools=["bash", "read", "glob"],
        )

        # Track execution order
        execution_order: list[str] = []

        async def tool_executor(name: str, args: dict[str, Any]) -> ToolResult:
            execution_order.append(f"start:{name}")
            await asyncio.sleep(0.1)  # Simulate work
            execution_order.append(f"end:{name}")
            return ToolResult(output=f"result:{name}")

        # Mock provider that returns multiple tool calls
        tool_schemas = [
            {
                "name": "bash",
                "description": "Run bash command",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "read",
                "description": "Read file",
                "parameters": {"type": "object", "properties": {}},
            },
        ]

        # Create a single chunk with both tool calls (different indices)
        chunk = create_chunk_with_tool_calls([
            {"id": "call_1", "name": "bash", "arguments": "{}"},
            {"id": "call_2", "name": "read", "arguments": "{}"},
        ], text_content="I'll run bash")

        # First call returns tool calls, second call returns a final text to end the loop
        call_count = 0

        async def mock_chat(**kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                for c in [chunk, create_final_chunk()]:
                    yield c
            else:
                # Return a plain text response to terminate the loop
                final = create_final_chunk()
                final.choices[0].delta.content = "Done"
                yield final

        mock_provider = MagicMock()
        mock_provider.detect_model = AsyncMock(return_value="test-model")
        mock_provider.chat = mock_chat

        loop = AgentLoop(
            agent=agent,
            provider=mock_provider,
            tool_executor=tool_executor,
            tool_schemas=tool_schemas,
        )

        # Collect all events
        events = [e async for e in loop.run("test message")]

        # Check that tool calls were made
        tool_call_events = [e for e in events if e.type == "tool_call"]
        tool_result_events = [e for e in events if e.type == "tool_result"]

        # Should have 2 tool calls
        assert len(tool_call_events) == 2
        assert len(tool_result_events) == 2

        # Check execution order - with parallel execution,
        # both should start before either completes
        print(f"Execution order: {execution_order}")

        # Parallel execution: both start, then both end
        # Expected: ['start:bash', 'start:read', 'end:bash', 'end:read']
        # (order of start/end may vary slightly due to scheduling)
        assert execution_order[0] == "start:bash"
        assert execution_order[1] == "start:read"
        assert "end:bash" in execution_order
        assert "end:read" in execution_order

    async def test_parallel_execution_is_implemented(self) -> None:
        """Verify that parallel tool execution IS now implemented."""
        agent = Agent(
            name="test",
            model="test-model",
            allowed_tools=["bash", "read", "glob"],
        )

        execution_times: list[tuple[float, str]] = []

        async def timed_tool_executor(name: str, args: dict[str, Any]) -> ToolResult:
            start = asyncio.get_event_loop().time()
            execution_times.append((start, f"start:{name}"))
            await asyncio.sleep(0.1)  # Each tool takes 100ms
            end = asyncio.get_event_loop().time()
            execution_times.append((end, f"end:{name}"))
            return ToolResult(output=f"result:{name}")

        tool_schemas = [
            {"name": "bash", "description": "", "parameters": {}},
            {"name": "read", "description": "", "parameters": {}},
        ]

        chunk = create_chunk_with_tool_calls([
            {"id": "call_1", "name": "bash", "arguments": "{}"},
            {"id": "call_2", "name": "read", "arguments": "{}"},
        ], text_content="I'll run both")

        call_count = 0

        async def mock_chat(**kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                for c in [chunk, create_final_chunk()]:
                    yield c
            else:
                final = create_final_chunk()
                final.choices[0].delta.content = "Done"
                yield final

        mock_provider = MagicMock()
        mock_provider.detect_model = AsyncMock(return_value="test-model")
        mock_provider.chat = mock_chat

        loop = AgentLoop(
            agent=agent,
            provider=mock_provider,
            tool_executor=timed_tool_executor,
            tool_schemas=tool_schemas,
        )

        _ = [e async for e in loop.run("test")]

        # With parallel execution, total time should be ~100ms (both run concurrently)
        # With sequential execution, total time would be ~200ms (each takes 100ms)
        # Check the timing difference
        if len(execution_times) >= 4:
            first_start = execution_times[0][0]
            last_end = execution_times[-1][0]
            total_time = last_end - first_start

            # Parallel execution: total time should be < 0.15s
            # Sequential execution: total time would be > 0.15s
            print(f"Total execution time: {total_time:.3f}s")

            # Now parallel execution is implemented
            assert total_time < 0.15, "Tool calls execute in parallel, taking ~100ms total"


class TestToolCallCollection:
    """Test that tool calls are properly collected before execution."""

    async def test_multiple_tool_calls_collected(self) -> None:
        """Verify that multiple tool calls from a single response are all collected."""
        agent = Agent(name="test", model="test-model", allowed_tools=["bash", "read", "glob"])

        tool_schemas = [
            {"name": "bash", "description": "", "parameters": {}},
            {"name": "read", "description": "", "parameters": {}},
        ]

        # Single chunk with multiple tool calls
        chunk = MagicMock()
        choice = MagicMock()
        delta = MagicMock()
        delta.content = "Running tools"

        # Create tool call deltas for multiple tools
        tc_deltas = []
        for i, name in enumerate(["bash", "read"]):
            tc_delta = MagicMock()
            tc_delta.index = i
            tc_delta.id = f"call_{i}"
            tc_delta.function = MagicMock()
            tc_delta.function.name = name
            tc_delta.function.arguments = "{}"
            tc_deltas.append(tc_delta)
        delta.tool_calls = tc_deltas

        choice.delta = delta
        chunk.choices = [choice]

        call_count = 0

        async def mock_chat(**kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                for c in [chunk, create_final_chunk()]:
                    yield c
            else:
                final = create_final_chunk()
                final.choices[0].delta.content = "Done"
                yield final

        mock_provider = MagicMock()
        mock_provider.detect_model = AsyncMock(return_value="test-model")
        mock_provider.chat = mock_chat

        tool_calls_found: list[str] = []

        async def tool_executor(name: str, args: dict[str, Any]) -> ToolResult:
            tool_calls_found.append(name)
            return ToolResult(output=f"result:{name}")

        loop = AgentLoop(
            agent=agent,
            provider=mock_provider,
            tool_executor=tool_executor,
            tool_schemas=tool_schemas,
        )

        _ = [e async for e in loop.run("test")]

        # Both tools should be executed
        assert "bash" in tool_calls_found
        assert "read" in tool_calls_found
        assert len(tool_calls_found) == 2
