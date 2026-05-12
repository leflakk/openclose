"""Tests for the plan tool, plan broker, and related session/route features."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openclose.tool.tools.plan import make_plan_tool
from openclose.tool.tools.plan_broker import PlanBroker, PlanReply
from openclose.tool.registry import ToolRegistry
from openclose.tool.tool import Tool, ToolResult
from openclose.agent.prompt import build_system_prompt
from openclose.agent.agent import Agent
from openclose.storage.db import Database


# ── Plan tool ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_tool_returns_marker(tmp_path: Path) -> None:
    """phase='final' returns the awaiting_plan_review marker."""
    tool = make_plan_tool(str(tmp_path), ToolRegistry())
    result = await tool.execute(content="## Step 1\nRead the code", phase="final")
    assert result.ok
    assert result.metadata.get("awaiting_plan_review") is True
    assert result.metadata.get("plan_content") == "## Step 1\nRead the code"


@pytest.mark.asyncio
async def test_plan_tool_empty_content(tmp_path: Path) -> None:
    tool = make_plan_tool(str(tmp_path), ToolRegistry())
    result = await tool.execute(content="", phase="final")
    assert not result.ok
    assert "required" in result.error.lower()


@pytest.mark.asyncio
async def test_plan_tool_phase_required(tmp_path: Path) -> None:
    """Calling without phase returns an error mentioning phase."""
    tool = make_plan_tool(str(tmp_path), ToolRegistry())
    result = await tool.execute(content="some plan")
    assert not result.ok
    assert "phase" in result.error.lower()


@pytest.mark.asyncio
async def test_plan_tool_phase_invalid(tmp_path: Path) -> None:
    """Calling with an unknown phase value returns an error."""
    tool = make_plan_tool(str(tmp_path), ToolRegistry())
    result = await tool.execute(content="some plan", phase="other")
    assert not result.ok
    assert "phase" in result.error.lower()
    assert "draft" in result.error and "final" in result.error


@pytest.mark.asyncio
async def test_plan_tool_phase_whitespace_treated_as_missing(tmp_path: Path) -> None:
    tool = make_plan_tool(str(tmp_path), ToolRegistry())
    result = await tool.execute(content="some plan", phase="   ")
    assert not result.ok
    assert "phase" in result.error.lower()


@pytest.mark.asyncio
async def test_plan_tool_draft_no_tools_available(tmp_path: Path) -> None:
    """phase='draft' returns an error when no allowed sub-tools are in the registry."""
    tool = make_plan_tool(str(tmp_path), ToolRegistry())
    result = await tool.execute(content="some plan", phase="draft")
    assert not result.ok
    assert "No tools available" in result.error


@pytest.mark.asyncio
async def test_plan_tool_draft_subagent_text_events(tmp_path: Path) -> None:
    """phase='draft' processes text, tool_call, and tool_result events and
    surfaces only the <report>...</report> body as the tool output."""
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> ToolResult:
        return ToolResult(output="ok")

    registry.register(Tool(name="read", description="read a file", parameters=[], execute_fn=noop))
    registry.register(Tool(name="grep", description="search", parameters=[], execute_fn=noop))

    tool = make_plan_tool(str(tmp_path), registry)

    tc_mock = MagicMock()
    tc_mock.name = "read"
    tc_mock.id = "tc-1"
    tc_mock.arguments_raw = "{}"

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("text", content="Thinking before the tool call.")
        yield StreamEvent("tool_call", tool_call=tc_mock)
        yield StreamEvent("tool_result", tool_call=tc_mock, tool_result="file contents")
        yield StreamEvent(
            "text",
            content=(
                "<report>\n"
                "**Verdict**: APPROVE WITH MINOR EDITS\n"
                "**Issues**: 1. Step 2 references plan.py:99 but the file ends at line 50.\n"
                "</report>"
            ),
        )

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(content="## Step 1", phase="draft")

    assert result.ok
    assert "APPROVE WITH MINOR EDITS" in result.output
    assert "plan.py:99" in result.output
    # Pre-tool scratch is dropped — only <report> body surfaces.
    assert "Thinking before" not in result.output
    assert result.metadata.get("phase") == "draft"
    assert result.metadata.get("tool_call_count") == 1


@pytest.mark.asyncio
async def test_plan_tool_draft_zero_tool_calls_rejected(tmp_path: Path) -> None:
    """A reviewer that emits only text (no tool_call) produces a rejection notice."""
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> ToolResult:
        return ToolResult(output="ok")

    registry.register(Tool(name="read", description="read", parameters=[], execute_fn=noop))

    tool = make_plan_tool(str(tmp_path), registry)

    async def mock_run(self: Any, msg: str) -> Any:
        # Text only, no tool_call event — should be rejected as ungrounded
        yield StreamEvent(
            "text",
            content="<report>The plan looks fine to me!</report>",
        )

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(content="## Step 1", phase="draft")

    # Tool returns ok=True with a rejection notice (content the parent can act on)
    assert result.ok
    assert "rejected" in result.output.lower()
    assert result.metadata.get("tool_call_count") == 0
    # The rubber-stamped <report> body must NOT leak through.
    assert "looks fine to me" not in result.output


@pytest.mark.asyncio
async def test_plan_tool_draft_metadata_shape(tmp_path: Path) -> None:
    """phase='draft' returns metadata with phase/subagent_steps/tool_call_count/stop_reason."""
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> ToolResult:
        return ToolResult(output="ok")

    registry.register(Tool(name="read", description="read", parameters=[], execute_fn=noop))

    tool = make_plan_tool(str(tmp_path), registry)

    tc_mock = MagicMock()
    tc_mock.name = "read"
    tc_mock.id = "tc-1"
    tc_mock.arguments_raw = "{}"

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("tool_call", tool_call=tc_mock)
        yield StreamEvent("tool_result", tool_call=tc_mock, tool_result="ok")
        yield StreamEvent("text", content="<report>OK</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(content="## P", phase="draft")

    assert result.ok
    md = result.metadata
    assert md["phase"] == "draft"
    assert md["tool_call_count"] == 1
    assert isinstance(md["subagent_steps"], list)
    assert "stop_reason" in md
    # Final phase must NOT carry the awaiting_plan_review marker.
    assert "awaiting_plan_review" not in md


# ── Plan broker ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_broker_execute() -> None:
    broker = PlanBroker()

    async def reply_later() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        assert len(pending) == 1
        broker.reply(pending[0]["request_id"], "execute")

    task = asyncio.create_task(reply_later())
    reply = await broker.ask("r1", "The plan", session_id="s1")
    assert reply.action == "execute"
    assert reply.feedback == ""
    await task


@pytest.mark.asyncio
async def test_plan_broker_execute_clear() -> None:
    broker = PlanBroker()

    async def reply_later() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        broker.reply(pending[0]["request_id"], "execute_clear")

    task = asyncio.create_task(reply_later())
    reply = await broker.ask("r1b", "The plan", session_id="s1")
    assert reply.action == "execute_clear"
    await task


@pytest.mark.asyncio
async def test_plan_broker_reject() -> None:
    broker = PlanBroker()

    async def reply_later() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        broker.reply(pending[0]["request_id"], "reject")

    task = asyncio.create_task(reply_later())
    reply = await broker.ask("r2", "The plan", session_id="s1")
    assert reply.action == "reject"
    await task


@pytest.mark.asyncio
async def test_plan_broker_revise_with_feedback() -> None:
    broker = PlanBroker()

    async def reply_later() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        broker.reply(pending[0]["request_id"], "revise", "Add error handling")

    task = asyncio.create_task(reply_later())
    reply = await broker.ask("r3", "The plan", session_id="s1")
    assert reply.action == "revise"
    assert reply.feedback == "Add error handling"
    await task


@pytest.mark.asyncio
async def test_plan_broker_cancel_session() -> None:
    broker = PlanBroker()

    async def ask_and_get_cancelled() -> PlanReply:
        return await broker.ask("r4", "The plan", session_id="s1")

    task = asyncio.create_task(ask_and_get_cancelled())
    await asyncio.sleep(0.01)
    broker.cancel_session("s1")
    reply = await task
    assert reply.action == "reject"


@pytest.mark.asyncio
async def test_plan_broker_list_pending() -> None:
    broker = PlanBroker()
    assert broker.list_pending() == []

    async def ask_task() -> None:
        await broker.ask("r5", "My plan", session_id="s1")

    task = asyncio.create_task(ask_task())
    await asyncio.sleep(0.01)
    pending = broker.list_pending()
    assert len(pending) == 1
    assert pending[0]["plan_content"] == "My plan"
    assert pending[0]["session_id"] == "s1"
    broker.reply(pending[0]["request_id"], "execute")
    await task


@pytest.mark.asyncio
async def test_plan_broker_reply_unknown() -> None:
    broker = PlanBroker()
    assert broker.reply("nonexistent", "execute") is False


# ── System prompt with plan context ───────────────────────────────────────


def test_plan_in_context_prompt() -> None:
    agent = Agent(name="build")
    prompt = build_system_prompt(
        agent,
        project_dir="/tmp/project",
        extra_context="## Active Plan\nStep 1: Do the thing",
    )
    assert "Active Plan" in prompt
    assert "Step 1: Do the thing" in prompt


def test_no_plan_in_context_prompt() -> None:
    agent = Agent(name="build")
    prompt = build_system_prompt(
        agent,
        project_dir="/tmp/project",
    )
    assert "Active Plan" not in prompt


# ── Session manager methods ───────────────────────────────────────────────


def test_session_update_agent(db: Database) -> None:
    from openclose.session.session import SessionManager

    mgr = SessionManager(db)
    session = mgr.create_session(title="test", agent="plan")
    assert session.agent == "plan"

    success = mgr.update_agent(session.id, "build")
    assert success

    updated = mgr.get_session(session.id)
    assert updated is not None
    assert updated.agent == "build"


def test_session_update_agent_not_found(db: Database) -> None:
    from openclose.session.session import SessionManager

    mgr = SessionManager(db)
    assert mgr.update_agent("nonexistent", "build") is False


def test_session_update_plan_in_context(db: Database) -> None:
    from openclose.session.session import SessionManager

    mgr = SessionManager(db)
    session = mgr.create_session(title="test", agent="build")
    assert session.plan_in_context is False

    success = mgr.update_plan_in_context(session.id, True)
    assert success

    updated = mgr.get_session(session.id)
    assert updated is not None
    assert updated.plan_in_context is True

    # Toggle back off
    mgr.update_plan_in_context(session.id, False)
    updated2 = mgr.get_session(session.id)
    assert updated2 is not None
    assert updated2.plan_in_context is False


def test_session_update_plan_in_context_not_found(db: Database) -> None:
    from openclose.session.session import SessionManager

    mgr = SessionManager(db)
    assert mgr.update_plan_in_context("nonexistent", True) is False
