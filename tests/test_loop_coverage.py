"""Tests targeting agent/loop.py uncovered paths."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openclose.agent.agent import Agent
from openclose.agent.loop import AgentLoop, ToolCall
from openclose.tool.tool import ToolResult


# ── ToolCall edge cases ──────────────────────────────────────────────────────

def test_tool_call_arguments_non_dict() -> None:
    tc = ToolCall()
    tc._arguments = '["a", "b"]'
    assert tc.arguments == {}


def test_tool_call_arguments_invalid_json() -> None:
    tc = ToolCall()
    tc._arguments = "{bad json"
    assert tc.arguments == {}


def test_tool_call_arguments_empty() -> None:
    tc = ToolCall()
    assert tc.arguments == {}


def test_tool_call_arguments_raw() -> None:
    tc = ToolCall()
    tc._arguments = '{"key": "val"}'
    assert tc.arguments_raw == '{"key": "val"}'


# ── Helper to build a minimal provider mock ──────────────────────────────────

def _make_provider() -> Any:
    p = MagicMock()
    p.detect_model = AsyncMock(return_value="test-model")
    return p


def _make_agent(**kwargs: Any) -> Agent:
    return Agent(
        name=kwargs.get("name", "test"),
        description=kwargs.get("description", "test agent"),
        model=kwargs.get("model", "test-model"),
        max_steps=kwargs.get("max_steps", 5),
        system_prompt=kwargs.get("system_prompt", "You are helpful"),
        allowed_tools=kwargs.get("allowed_tools", []),
    )


def _build_chunk(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
) -> MagicMock:
    """Build a mock streaming chunk."""
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = None

    if tool_calls:
        tc_deltas = []
        for tc in tool_calls:
            d = MagicMock()
            d.index = tc.get("index", 0)
            d.id = tc.get("id")
            d.function = MagicMock()
            d.function.name = tc.get("name")
            d.function.arguments = tc.get("arguments")
            tc_deltas.append(d)
        delta.tool_calls = tc_deltas

    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason
    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


# ── Model auto-detection failure ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_model_detection_failure() -> None:
    provider = _make_provider()
    provider.detect_model = AsyncMock(return_value=None)
    agent = _make_agent(model="")
    loop = AgentLoop(agent=agent, provider=provider)

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    assert any(e.type == "error" and "No model configured" in e.error for e in events)


# ── Text-only response (no tool calls) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_run_text_only_response() -> None:
    provider = _make_provider()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(content="Hello world")

    provider.chat = mock_chat
    agent = _make_agent()
    loop = AgentLoop(agent=agent, provider=provider)

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    assert any(e.type == "text" and "Hello world" in e.content for e in events)
    assert any(e.type == "done" for e in events)


# ── LLM streaming error ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_llm_streaming_error() -> None:
    provider = _make_provider()

    async def mock_chat(**kwargs: Any) -> Any:
        raise RuntimeError("LLM connection failed")
        yield  # noqa: F841 — makes this an async generator

    provider.chat = mock_chat
    agent = _make_agent()
    loop = AgentLoop(agent=agent, provider=provider)

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    assert any(e.type == "error" and "LLM connection failed" in e.error for e in events)


# ── Cancellation before step ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_cancel_before_step() -> None:
    provider = _make_provider()
    agent = _make_agent()

    cancel = asyncio.Event()
    cancel.set()  # already cancelled
    loop = AgentLoop(agent=agent, provider=provider, cancel_event=cancel)

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    assert any(e.type == "done" for e in events)


# ── Cancellation after streaming with partial text ───────────────────────────

@pytest.mark.asyncio
async def test_run_cancel_after_streaming() -> None:
    provider = _make_provider()
    cancel = asyncio.Event()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(content="Partial")
        cancel.set()

    provider.chat = mock_chat
    agent = _make_agent()
    loop = AgentLoop(agent=agent, provider=provider, cancel_event=cancel)

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    assert any(e.type == "text" for e in events)
    assert any(e.type == "done" for e in events)


# ── Max steps reached ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_max_steps_reached() -> None:
    provider = _make_provider()
    call_count = 0

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        # Return tool calls so the loop continues
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": f"tc{call_count}", "name": "read",
            "arguments": json.dumps({"file_path": "/tmp/x.py"}),
        }])

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    provider.chat = mock_chat
    agent = _make_agent(max_steps=2)
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    assert any(e.type == "error" and "Max steps" in e.error for e in events)


# ── Agent-level tool denial ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_agent_tool_denied() -> None:
    provider = _make_provider()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": "tc1", "name": "bash",
            "arguments": json.dumps({"command": "ls"}),
        }])

    provider.chat = mock_chat
    agent = _make_agent(allowed_tools=["read"])  # bash not allowed

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "bash", "parameters": {"properties": {}}}],
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    tool_results = [e for e in events if e.type == "tool_result"]
    assert any("not allowed" in e.tool_result for e in tool_results)


# ── Path sandbox denial ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_path_sandbox_denied() -> None:
    provider = _make_provider()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": "tc1", "name": "write",
            "arguments": json.dumps({"file_path": "/etc/passwd", "content": "hack"}),
        }])

    provider.chat = mock_chat
    agent = _make_agent()

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "write", "parameters": {"properties": {}}}],
        project_dir="/tmp/project",
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    tool_results = [e for e in events if e.type == "tool_result"]
    assert any("Cannot" in e.tool_result for e in tool_results)


# ── Tool executor exception ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_tool_executor_exception() -> None:
    provider = _make_provider()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": "tc1", "name": "read",
            "arguments": json.dumps({"file_path": "/tmp/x.py"}),
        }])

    provider.chat = mock_chat

    async def bad_executor(name: str, args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("executor boom")

    agent = _make_agent()
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=bad_executor,
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    tool_results = [e for e in events if e.type == "tool_result"]
    assert any("Error executing" in e.tool_result for e in tool_results)


# ── No tool executor configured ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_no_tool_executor() -> None:
    provider = _make_provider()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": "tc1", "name": "read",
            "arguments": json.dumps({"file_path": "/tmp/x.py"}),
        }])

    provider.chat = mock_chat
    agent = _make_agent()
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=None,
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    tool_results = [e for e in events if e.type == "tool_result"]
    assert any("No tool executor" in e.tool_result for e in tool_results)


# ── Doom-loop detection (no broker) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_doom_loop_no_broker() -> None:
    provider = _make_provider()
    call_count = 0

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": f"tc{call_count}", "name": "read",
            "arguments": '{"file_path": "/tmp/same.py"}',
        }])

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="content")

    agent = _make_agent(max_steps=10)
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    assert any(e.type == "error" and "Doom loop" in e.error for e in events)


# ── Bash-windowed doom-loop detection ────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_bash_doom_consecutive_identical() -> None:
    """3 byte-identical bash calls in a row trigger the bash doom rule."""
    provider = _make_provider()
    call_count = 0

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": f"tc{call_count}", "name": "bash",
            "arguments": json.dumps({"command": "grep -rn class Field /lib/"}),
        }])

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="...")

    agent = _make_agent(max_steps=20)
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "bash", "parameters": {"properties": {}}}],
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    # The recovery nudge fires first; only the second trip yields the error.
    nudges = [
        m for m in loop.messages
        if m.get("role") == "user"
        and "ran the same bash command" in str(m.get("content", ""))
    ]
    assert len(nudges) == 1
    assert any(e.type == "error" and "Doom loop" in e.error for e in events)


@pytest.mark.asyncio
async def test_run_bash_doom_windowed_through_interleaving() -> None:
    """Probe → mutate → probe → mutate → probe loops are caught by the
    windowed bash doom rule even though the repeats are NOT consecutive.
    This is the specific failure mode that read-only doom and the
    consecutive install-burst both miss."""
    provider = _make_provider()
    call_count = 0
    PROBE = "python3 -c 'from foo import bar; bar()'"
    cmds = [
        PROBE,                       # 1
        "pip install setuptools",    # 2 — interleave (different command)
        PROBE,                       # 3
        "pip install babel",         # 4 — interleave
        PROBE,                       # 5 — third byte-identical PROBE in window of 5
    ]

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count <= len(cmds):
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": f"tc{call_count}", "name": "bash",
                "arguments": json.dumps({"command": cmds[call_count - 1]}),
            }])
        else:
            yield _build_chunk(content="done", finish_reason="stop")

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    agent = _make_agent(max_steps=20)
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "bash", "parameters": {"properties": {}}}],
    )

    async for _ in loop.run("hi"):
        pass

    nudges = [
        m for m in loop.messages
        if m.get("role") == "user"
        and "ran the same bash command" in str(m.get("content", ""))
    ]
    assert len(nudges) == 1, (
        f"expected windowed bash doom to fire after 3 interleaved repeats, "
        f"got {len(nudges)} nudges"
    )


@pytest.mark.asyncio
async def test_run_bash_doom_does_not_fire_on_distinct_commands() -> None:
    """5 different bash commands must NOT trigger bash doom — only
    byte-identical repeats count. This prevents false positives on
    legitimate exploration."""
    provider = _make_provider()
    call_count = 0
    cmds = [
        "ls /a",
        "ls /b",
        "cat /a/foo",
        "grep -rn x /a/",
        "echo done",
    ]

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count <= len(cmds):
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": f"tc{call_count}", "name": "bash",
                "arguments": json.dumps({"command": cmds[call_count - 1]}),
            }])
        else:
            yield _build_chunk(content="done", finish_reason="stop")

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    agent = _make_agent(max_steps=20)
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "bash", "parameters": {"properties": {}}}],
    )

    async for _ in loop.run("hi"):
        pass

    assert not any(
        m.get("role") == "user"
        and "ran the same bash command" in str(m.get("content", ""))
        for m in loop.messages
    )


# ── Cancellation after tool execution ────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_cancel_after_tool_execution() -> None:
    provider = _make_provider()
    cancel = asyncio.Event()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": "tc1", "name": "read",
            "arguments": json.dumps({"file_path": "/tmp/x.py"}),
        }])

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        cancel.set()
        return ToolResult(output="ok")

    agent = _make_agent()
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
        cancel_event=cancel,
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    assert any(e.type == "done" for e in events)


# ── Bash command safety check ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_bash_blocked_command() -> None:
    provider = _make_provider()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": "tc1", "name": "bash",
            "arguments": json.dumps({"command": "rm -rf /"}),
        }])

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    agent = _make_agent()
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "bash", "parameters": {"properties": {}}}],
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    tool_results = [e for e in events if e.type == "tool_result"]
    assert any("Blocked" in e.tool_result for e in tool_results)


# ── Permission engine DENY ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_permission_deny() -> None:
    provider = _make_provider()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": "tc1", "name": "read",
            "arguments": json.dumps({"file_path": "/tmp/x.py"}),
        }])

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    perm_engine = MagicMock()
    perm_resp = MagicMock()
    perm_resp.allowed = False
    perm_resp.needs_ask = False
    perm_resp.reason = "Blocked by DENY rule"
    perm_engine.check.return_value = perm_resp

    agent = _make_agent()
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
        permission_engine=perm_engine,
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    tool_results = [e for e in events if e.type == "tool_result"]
    assert any("DENY" in e.tool_result for e in tool_results)


# ── Permission engine ASK + reject ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_permission_ask_rejected() -> None:
    provider = _make_provider()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": "tc1", "name": "read",
            "arguments": json.dumps({"file_path": "/tmp/x.py"}),
        }])

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    perm_engine = MagicMock()
    perm_resp = MagicMock()
    perm_resp.allowed = False
    perm_resp.needs_ask = True
    perm_resp.reason = "Needs approval"
    perm_engine.check.return_value = perm_resp

    perm_broker = MagicMock()
    perm_broker.ask = AsyncMock(return_value="reject")

    agent = _make_agent()
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
        permission_engine=perm_engine,
        permission_broker=perm_broker,
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    tool_results = [e for e in events if e.type == "tool_result"]
    assert any("REJECTED" in e.tool_result for e in tool_results)


# ── Permission engine ASK + always ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_permission_ask_always() -> None:
    provider = _make_provider()
    call_count = 0

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": "tc1", "name": "read",
                "arguments": json.dumps({"file_path": "/tmp/x.py"}),
            }])
        else:
            yield _build_chunk(content="Done")

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="file content")

    perm_engine = MagicMock()
    perm_resp = MagicMock()
    perm_resp.allowed = False
    perm_resp.needs_ask = True
    perm_resp.reason = "Needs approval"
    perm_engine.check.return_value = perm_resp

    perm_broker = MagicMock()
    perm_broker.ask = AsyncMock(return_value="always")

    agent = _make_agent()
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
        permission_engine=perm_engine,
        permission_broker=perm_broker,
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    perm_engine.grant_session.assert_called_with("read")
    tool_results = [e for e in events if e.type == "tool_result"]
    assert any("file content" in e.tool_result for e in tool_results)


# ── Permission ASK without broker ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_permission_ask_no_broker() -> None:
    provider = _make_provider()

    async def mock_chat(**kwargs: Any) -> Any:
        yield _build_chunk(tool_calls=[{
            "index": 0, "id": "tc1", "name": "read",
            "arguments": json.dumps({"file_path": "/tmp/x.py"}),
        }])

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    perm_engine = MagicMock()
    perm_resp = MagicMock()
    perm_resp.allowed = False
    perm_resp.needs_ask = True
    perm_resp.reason = "Need approval"
    perm_engine.check.return_value = perm_resp

    agent = _make_agent()
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
        permission_engine=perm_engine,
        permission_broker=None,
    )

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    tool_results = [e for e in events if e.type == "tool_result"]
    assert any("No approval mechanism" in e.tool_result for e in tool_results)


# ── Empty response (no content, no tool calls) ──────────────────────────────

@pytest.mark.asyncio
async def test_run_empty_response_retries_then_terminates() -> None:
    """Empty responses should retry with nudge, then terminate after max retries."""
    from openclose.agent.loop import _MAX_EMPTY_RETRIES

    provider = _make_provider()
    call_count = 0

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        yield _build_chunk(content=None, tool_calls=None, finish_reason="stop")

    provider.chat = mock_chat
    agent = _make_agent()
    loop = AgentLoop(agent=agent, provider=provider)

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    # Should have retried _MAX_EMPTY_RETRIES - 1 times before terminating
    info_events = [e for e in events if e.type == "info"]
    assert len(info_events) == _MAX_EMPTY_RETRIES - 1
    assert any(e.type == "done" for e in events)
    assert call_count == _MAX_EMPTY_RETRIES


@pytest.mark.asyncio
async def test_run_empty_response_recovers() -> None:
    """Model recovers after one empty response thanks to nudge."""
    provider = _make_provider()
    call_count = 0

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield _build_chunk(content=None, tool_calls=None, finish_reason="stop")
        else:
            yield _build_chunk(content="Here is my response", finish_reason="stop")

    provider.chat = mock_chat
    agent = _make_agent()
    loop = AgentLoop(agent=agent, provider=provider)

    events = []
    async for event in loop.run("hi"):
        events.append(event)

    assert any(e.type == "info" for e in events)  # retry happened
    assert any(e.type == "text" and "Here is my response" in e.content for e in events)
    assert any(e.type == "done" for e in events)
    assert call_count == 2


# ── Install-burst detection ─────────────────────────────────────────────────

def test_detect_install_pattern_basic() -> None:
    from openclose.agent.loop import _detect_install_pattern

    assert _detect_install_pattern("pip install requests") == "pip install"
    assert _detect_install_pattern("pip3 install -U foo") == "pip install"
    assert _detect_install_pattern("pipx install black") == "pip install"
    assert _detect_install_pattern("npm install") == "npm install"
    assert _detect_install_pattern("npm i lodash") == "npm install"
    assert _detect_install_pattern("yarn add react") == "npm install"
    assert _detect_install_pattern("pnpm i") == "npm install"
    assert _detect_install_pattern("apt install vim") == "apt install"
    assert _detect_install_pattern("apt-get install -y gcc") == "apt install"
    assert _detect_install_pattern("brew install jq") == "brew install"
    assert _detect_install_pattern("uv pip install httpx") == "uv install"
    assert _detect_install_pattern("uv add httpx") == "uv install"
    assert _detect_install_pattern("cargo add serde") == "cargo install"
    assert _detect_install_pattern("ls -la") is None
    assert _detect_install_pattern("python -c 'print(1)'") is None
    # `pipeline` must not match `pip` (word boundary)
    assert _detect_install_pattern("pipeline install foo") is None


@pytest.mark.asyncio
async def test_run_install_burst_injects_nudge() -> None:
    """3 consecutive pip install bash calls trigger the reminder injection."""
    provider = _make_provider()
    call_count = 0

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": f"tc{call_count}", "name": "bash",
                "arguments": json.dumps({
                    "command": f"pip install pkg{call_count}",
                }),
            }])
        else:
            yield _build_chunk(content="done", finish_reason="stop")

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="installed")

    agent = _make_agent(max_steps=10)
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "bash", "parameters": {"properties": {}}}],
    )

    async for _ in loop.run("install some packages"):
        pass

    nudges = [
        m for m in loop.messages
        if m.get("role") == "user"
        and "pip install pattern" in str(m.get("content", ""))
    ]
    assert len(nudges) == 1, f"expected exactly 1 nudge, got {len(nudges)}"
    # The very first user message ("install some packages") shouldn't match.
    assert nudges[0]["content"].startswith("You've made 3 bash calls")


@pytest.mark.asyncio
async def test_run_install_burst_windowed_through_non_install_bash() -> None:
    """Windowed: a non-install bash interleaved between installs does NOT
    reset the count — the install pattern is still detected within the
    window, which is the whole point of the windowed rule."""
    provider = _make_provider()
    call_count = 0
    cmds = [
        "pip install foo",
        "pip install bar",
        "ls -la",  # interleave — must not break detection
        "pip install baz",
    ]

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count <= len(cmds):
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": f"tc{call_count}", "name": "bash",
                "arguments": json.dumps({"command": cmds[call_count - 1]}),
            }])
        else:
            yield _build_chunk(content="done", finish_reason="stop")

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    agent = _make_agent(max_steps=10)
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "bash", "parameters": {"properties": {}}}],
    )

    async for _ in loop.run("hi"):
        pass

    nudges = [
        m for m in loop.messages
        if m.get("role") == "user"
        and "pip install pattern" in str(m.get("content", ""))
    ]
    assert len(nudges) == 1


@pytest.mark.asyncio
async def test_run_install_burst_windowed_through_non_bash_tool() -> None:
    """Windowed: a non-bash tool between installs also does NOT reset.
    Counters are per-bash-call, so non-bash tools are simply ignored."""
    provider = _make_provider()
    call_count = 0

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count in (1, 2):
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": f"tc{call_count}", "name": "bash",
                "arguments": json.dumps({"command": f"pip install foo{call_count}"}),
            }])
        elif call_count == 3:
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": f"tc{call_count}", "name": "read",
                "arguments": json.dumps({"file_path": "/tmp/x.py"}),
            }])
        elif call_count == 4:
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": f"tc{call_count}", "name": "bash",
                "arguments": json.dumps({"command": "pip install bar"}),
            }])
        else:
            yield _build_chunk(content="done", finish_reason="stop")

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    agent = _make_agent(max_steps=10)
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[
            {"name": "bash", "parameters": {"properties": {}}},
            {"name": "read", "parameters": {"properties": {}}},
        ],
    )

    async for _ in loop.run("hi"):
        pass

    nudges = [
        m for m in loop.messages
        if m.get("role") == "user"
        and "pip install pattern" in str(m.get("content", ""))
    ]
    assert len(nudges) == 1


@pytest.mark.asyncio
async def test_run_install_burst_fires_once_per_streak() -> None:
    """Five consecutive same-pattern installs only inject one nudge."""
    provider = _make_provider()
    call_count = 0

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count <= 5:
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": f"tc{call_count}", "name": "bash",
                "arguments": json.dumps({
                    "command": f"npm install pkg{call_count}",
                }),
            }])
        else:
            yield _build_chunk(content="done", finish_reason="stop")

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    agent = _make_agent(max_steps=10)
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "bash", "parameters": {"properties": {}}}],
    )

    async for _ in loop.run("hi"):
        pass

    nudges = [
        m for m in loop.messages
        if m.get("role") == "user"
        and "npm install pattern" in str(m.get("content", ""))
    ]
    assert len(nudges) == 1


@pytest.mark.asyncio
async def test_run_install_burst_different_kinds_no_nudge() -> None:
    """Three installs of *different* kinds shouldn't trigger the nudge."""
    provider = _make_provider()
    call_count = 0
    cmds = [
        "pip install foo",
        "npm install bar",
        "apt install baz",
    ]

    async def mock_chat(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count <= len(cmds):
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": f"tc{call_count}", "name": "bash",
                "arguments": json.dumps({"command": cmds[call_count - 1]}),
            }])
        else:
            yield _build_chunk(content="done", finish_reason="stop")

    provider.chat = mock_chat

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="ok")

    agent = _make_agent(max_steps=10)
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "bash", "parameters": {"properties": {}}}],
    )

    async for _ in loop.run("hi"):
        pass

    assert not any(
        m.get("role") == "user"
        and "bash calls matching a" in str(m.get("content", ""))
        for m in loop.messages
    )


# ── tool_choice forwarding for sub-agent first turn ─────────────────────────

@pytest.mark.asyncio
async def test_subagent_first_turn_forces_tool_choice_required() -> None:
    """Sub-agent step 1 should send tool_choice='required' to the provider."""
    from openclose.agent.agent import AgentMode

    provider = _make_provider()
    captured_kwargs: list[dict[str, Any]] = []

    async def mock_chat(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        # Return text-only — loop will exit after one turn
        yield _build_chunk(content="done")

    provider.chat = mock_chat
    agent = Agent(
        name="delegate",
        description="sub",
        model="test",
        max_steps=5,
        system_prompt="x",
        mode=AgentMode.SUBAGENT,
        allowed_tools=["read"],
    )
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=AsyncMock(return_value=ToolResult(output="ok")),
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
    )

    async for _ in loop.run("Mission: x"):
        pass

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("tool_choice") == "required"


@pytest.mark.asyncio
async def test_subagent_step2_reverts_to_auto() -> None:
    """After step 1 issues a tool call, step 2 should NOT force tool_choice."""
    from openclose.agent.agent import AgentMode

    provider = _make_provider()
    captured_kwargs: list[dict[str, Any]] = []
    call_count = {"n": 0}

    async def mock_chat(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Step 1: emit a tool call so the loop progresses to step 2
            yield _build_chunk(tool_calls=[{
                "index": 0, "id": "tc1", "name": "read",
                "arguments": json.dumps({"file_path": "/tmp/x.py"}),
            }])
        else:
            # Step 2: emit text to terminate the loop
            yield _build_chunk(content="found it")

    provider.chat = mock_chat
    agent = Agent(
        name="delegate",
        description="sub",
        model="test",
        max_steps=5,
        system_prompt="x",
        mode=AgentMode.SUBAGENT,
        allowed_tools=["read"],
    )

    async def mock_executor(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output="contents")

    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=mock_executor,
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
        project_dir="/tmp",
    )

    async for _ in loop.run("Mission: x"):
        pass

    assert len(captured_kwargs) == 2
    assert captured_kwargs[0].get("tool_choice") == "required"
    assert captured_kwargs[1].get("tool_choice") is None


@pytest.mark.asyncio
async def test_primary_agent_never_forces_tool_choice() -> None:
    """Primary agents should never have tool_choice set."""
    from openclose.agent.agent import AgentMode

    provider = _make_provider()
    captured_kwargs: list[dict[str, Any]] = []

    async def mock_chat(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        yield _build_chunk(content="hi")

    provider.chat = mock_chat
    agent = Agent(
        name="build", description="main", model="test", max_steps=5,
        system_prompt="x", mode=AgentMode.PRIMARY, allowed_tools=["read"],
    )
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=AsyncMock(return_value=ToolResult(output="ok")),
        tool_schemas=[{"name": "read", "parameters": {"properties": {}}}],
    )

    async for _ in loop.run("hi"):
        pass

    assert captured_kwargs[0].get("tool_choice") is None


@pytest.mark.asyncio
async def test_subagent_with_no_tools_no_tool_choice() -> None:
    """Sub-agent with no tools must not force tool_choice (would 400)."""
    from openclose.agent.agent import AgentMode

    provider = _make_provider()
    captured_kwargs: list[dict[str, Any]] = []

    async def mock_chat(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        yield _build_chunk(content="ok")

    provider.chat = mock_chat
    agent = Agent(
        name="delegate", description="sub", model="test", max_steps=5,
        system_prompt="x", mode=AgentMode.SUBAGENT, allowed_tools=[],
    )
    loop = AgentLoop(
        agent=agent, provider=provider,
        tool_executor=AsyncMock(return_value=ToolResult(output="ok")),
        tool_schemas=None,
    )

    async for _ in loop.run("hi"):
        pass

    assert captured_kwargs[0].get("tool_choice") is None


# ── Parallel-tool-call streaming recovery ────────────────────────────────────

def test_split_top_level_json_objects_clean() -> None:
    from openclose.agent.loop import _split_top_level_json_objects
    s = '{"a": 1}{"b": 2}'
    assert _split_top_level_json_objects(s) == ['{"a": 1}', '{"b": 2}']


def test_split_top_level_json_objects_with_string_braces() -> None:
    from openclose.agent.loop import _split_top_level_json_objects
    # Braces inside strings must be ignored.
    s = '{"x":"a}b"}{"y":"{"}'
    assert _split_top_level_json_objects(s) == ['{"x":"a}b"}', '{"y":"{"}']


def test_split_top_level_json_objects_escaped_quote() -> None:
    from openclose.agent.loop import _split_top_level_json_objects
    s = r'{"x":"a\"b"}{"y":1}'
    assert _split_top_level_json_objects(s) == [r'{"x":"a\"b"}', '{"y":1}']


def test_recover_split_tool_args_re_splits_endpoint_bug() -> None:
    """Reproduces the SWE-bench failure: parallel glob calls whose
    args were chunked mid-string by the local OpenAI-compatible endpoint.
    """
    from openclose.agent.loop import ToolCall, _recover_split_tool_args
    tc0 = ToolCall()
    tc0.id, tc0.name = "c0", "glob"
    tc0.append_arguments(
        '{"pattern":"sphinx/ext/napoleon/**/*.py","path":"/home/x/sphinx-8056'
    )
    tc1 = ToolCall()
    tc1.id, tc1.name = "c1", "glob"
    tc1.append_arguments(
        '"}{"pattern":"**/test*napoleon*.py","path":"/home/x/sphinx-8056"}'
    )
    calls = {0: tc0, 1: tc1}
    assert _recover_split_tool_args(calls) is True
    assert json.loads(tc0.arguments_raw)["pattern"] == "sphinx/ext/napoleon/**/*.py"
    assert json.loads(tc0.arguments_raw)["path"] == "/home/x/sphinx-8056"
    assert json.loads(tc1.arguments_raw)["pattern"] == "**/test*napoleon*.py"


def test_recover_split_tool_args_noop_when_clean() -> None:
    from openclose.agent.loop import ToolCall, _recover_split_tool_args
    tc0 = ToolCall()
    tc0.append_arguments('{"a": 1}')
    tc1 = ToolCall()
    tc1.append_arguments('{"b": 2}')
    calls = {0: tc0, 1: tc1}
    assert _recover_split_tool_args(calls) is False
    assert tc0.arguments_raw == '{"a": 1}'
    assert tc1.arguments_raw == '{"b": 2}'


def test_recover_split_tool_args_bails_on_cardinality_mismatch() -> None:
    """If recovery would produce a different number of objects, leave it alone."""
    from openclose.agent.loop import ToolCall, _recover_split_tool_args
    tc0 = ToolCall()
    tc0.append_arguments('{"a": 1')  # bad
    tc1 = ToolCall()
    tc1.append_arguments(', "b": 2}')  # bad — concat is 1 object, not 2
    calls = {0: tc0, 1: tc1}
    assert _recover_split_tool_args(calls) is False
    # Args left unchanged
    assert tc0.arguments_raw == '{"a": 1'
    assert tc1.arguments_raw == ', "b": 2}'


def test_recover_split_tool_args_single_call_is_noop() -> None:
    from openclose.agent.loop import ToolCall, _recover_split_tool_args
    tc0 = ToolCall()
    tc0.append_arguments('{"bad": ')  # invalid but only one call
    calls = {0: tc0}
    assert _recover_split_tool_args(calls) is False
