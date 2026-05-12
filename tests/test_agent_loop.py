"""Tests for the agent loop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openclose.agent.agent import get_agent
from openclose.agent.loop import AgentLoop, ToolCall, StreamEvent


def test_tool_call_arguments_parsing() -> None:
    """ToolCall should parse JSON arguments."""
    tc = ToolCall()
    tc.id = "call_1"
    tc.name = "read"
    tc.append_arguments('{"path": "/tmp/test.py"}')
    assert tc.arguments == {"path": "/tmp/test.py"}
    assert tc.arguments_raw == '{"path": "/tmp/test.py"}'


def test_tool_call_invalid_json() -> None:
    """ToolCall should return empty dict for invalid JSON."""
    tc = ToolCall()
    tc.append_arguments("not json")
    assert tc.arguments == {}


def test_tool_call_empty() -> None:
    """ToolCall with no arguments should return empty dict."""
    tc = ToolCall()
    assert tc.arguments == {}


def test_stream_event_types() -> None:
    """StreamEvent should hold correct data."""
    text_event = StreamEvent("text", content="hello")
    assert text_event.type == "text"
    assert text_event.content == "hello"
    assert not text_event.done

    done_event = StreamEvent("done", done=True)
    assert done_event.done

    error_event = StreamEvent("error", error="fail")
    assert error_event.error == "fail"

    tc = ToolCall()
    tc.name = "bash"
    tool_event = StreamEvent("tool_call", tool_call=tc)
    assert tool_event.tool_call is not None
    assert tool_event.tool_call.name == "bash"


# ── _switch_agent (plan→build mid-loop swap) ────────────────────────────────


def test_agent_loop_switch_agent_rebinds_tools(tmp_path: Path) -> None:
    """Switching the loop's agent re-filters tool schemas and resets per-run noise.

    Regression: clicking "Execute" on a plan used to leave the loop running
    with the plan agent's read-only tool schemas, so the build agent could
    not call write/edit/bash. The fix swaps the in-memory agent and
    re-derives the tool view at end-of-round.
    """
    plan_agent = get_agent("plan")
    schemas = [
        {"name": "read",  "description": "", "parameters": {}},
        {"name": "write", "description": "", "parameters": {}},
        {"name": "bash",  "description": "", "parameters": {}},
        {"name": "plan",  "description": "", "parameters": {}},
    ]
    loop = AgentLoop(
        agent=plan_agent,
        provider=MagicMock(),
        tool_schemas=schemas,
        project_dir=str(tmp_path),
    )
    # Plan agent: read+plan+bash visible, write filtered out.
    assert "read" in loop._tool_names
    assert "plan" in loop._tool_names
    assert "write" not in loop._tool_names
    assert "bash" in loop._tool_names

    # Pre-load some "noise" state to verify the reset.
    loop._recent_calls.append(("read", "{}"))
    loop._recent_bash.append(("ls", None))
    loop._doom_nudged = True
    loop._install_burst_nudged = True
    loop._step = 7

    loop._switch_agent("build")

    # Agent + tools swapped.
    assert loop._agent.name == "build"
    assert "write" in loop._tool_names
    assert "bash" in loop._tool_names
    assert "plan" not in loop._tool_names
    # Per-run noise reset.
    assert list(loop._recent_calls) == []
    assert list(loop._recent_bash) == []
    assert loop._doom_nudged is False
    assert loop._install_burst_nudged is False
    # Step budget preserved (no plan→build reset exploit).
    assert loop._step == 7


def test_agent_loop_switch_agent_injects_plan_into_extra_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After plan.md is written, _switch_agent populates extra_context with it.

    Mirrors processor.py's extra-context injection so the build agent's
    next system prompt contains the same "## Active Plan" block that a
    fresh AgentLoop would get on the next user message.
    """
    # Redirect XDG_CONFIG_HOME so ConfigPaths.project_runtime_dir lands
    # under tmp_path (Linux convention; the tests already gate on Linux
    # via the project's CI matrix).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()

    from openclose.config.paths import ConfigPaths
    runtime_dir = ConfigPaths.project_runtime_dir(str(project_dir))
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "plan.md").write_text("## Step 1\nRead the code")

    loop = AgentLoop(
        agent=get_agent("plan"),
        provider=MagicMock(),
        tool_schemas=[],
        project_dir=str(project_dir),
    )
    assert loop._extra_context == ""

    loop._switch_agent("build")

    assert "## Active Plan" in loop._extra_context
    assert "## Step 1" in loop._extra_context


def test_agent_loop_no_pending_switch_by_default(tmp_path: Path) -> None:
    """A fresh loop should not have a pending agent swap."""
    loop = AgentLoop(
        agent=get_agent("build"),
        provider=MagicMock(),
        tool_schemas=[],
        project_dir=str(tmp_path),
    )
    assert loop._pending_agent_switch is None
