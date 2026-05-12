"""Tests targeting specific coverage gaps: grep error paths, process timeout
draining, and session processor tool_result / compaction paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openclose.util.process import ProcessResult


# ── grep.py: ripgrep error path (lines 29-39) ──────────────────────────────

@pytest.mark.asyncio
async def test_grep_ripgrep_error_returncode(tmp_path: Path) -> None:
    """When ripgrep returns a code > 1 (error), the tool returns an error."""
    from openclose.tool.tools.grep import make_grep_tool

    (tmp_path / "test.py").write_text("hello\n")
    tool = make_grep_tool(str(tmp_path))

    fake_result = ProcessResult(returncode=2, stdout="", stderr="rg: broken")
    with patch("openclose.tool.tools.grep.run", new_callable=AsyncMock, return_value=fake_result):
        with patch("shutil.which", return_value="/usr/bin/rg"):
            result = await tool.execute(pattern="hello")
    assert not result.ok
    assert "rg: broken" in result.error


@pytest.mark.asyncio
async def test_grep_ripgrep_error_no_stderr(tmp_path: Path) -> None:
    """When ripgrep fails with no stderr, a fallback message is returned."""
    from openclose.tool.tools.grep import make_grep_tool

    (tmp_path / "test.py").write_text("hello\n")
    tool = make_grep_tool(str(tmp_path))

    fake_result = ProcessResult(returncode=2, stdout="", stderr="")
    with patch("openclose.tool.tools.grep.run", new_callable=AsyncMock, return_value=fake_result):
        with patch("shutil.which", return_value="/usr/bin/rg"):
            result = await tool.execute(pattern="hello")
    assert not result.ok
    assert "ripgrep failed" in result.error


@pytest.mark.asyncio
async def test_grep_ripgrep_no_match(tmp_path: Path) -> None:
    """When ripgrep returns 1 (no matches), tool returns 'No matches'."""
    from openclose.tool.tools.grep import make_grep_tool

    (tmp_path / "test.py").write_text("hello\n")
    tool = make_grep_tool(str(tmp_path))

    fake_result = ProcessResult(returncode=1, stdout="", stderr="")
    with patch("openclose.tool.tools.grep.run", new_callable=AsyncMock, return_value=fake_result):
        with patch("shutil.which", return_value="/usr/bin/rg"):
            result = await tool.execute(pattern="zzz_no_match")
    assert "No matches" in result.output


@pytest.mark.asyncio
async def test_grep_ripgrep_include_flag(tmp_path: Path) -> None:
    """When include is specified, it's passed as --glob to rg."""
    from openclose.tool.tools.grep import make_grep_tool

    (tmp_path / "test.py").write_text("hello\n")
    tool = make_grep_tool(str(tmp_path))

    fake_result = ProcessResult(returncode=0, stdout="test.py:1:hello", stderr="")
    with patch("openclose.tool.tools.grep.run", new_callable=AsyncMock, return_value=fake_result) as mock_run:
        with patch("shutil.which", return_value="/usr/bin/rg"):
            result = await tool.execute(pattern="hello", include="*.py")
    assert result.ok
    # Verify --glob *.py was in the args
    call_args = mock_run.call_args
    assert "*.py" in call_args.args


# ── grep.py: python fallback 200-match limit (lines 93-98) ─────────────────

@pytest.mark.asyncio
async def test_python_grep_200_match_limit(tmp_path: Path) -> None:
    """Python fallback grep stops at 200 matches."""
    from openclose.tool.tools.grep import _python_grep

    # Create a file with 250 matching lines
    lines = [f"match line {i}" for i in range(250)]
    (tmp_path / "big.txt").write_text("\n".join(lines))

    result = await _python_grep("match", str(tmp_path), "")
    assert result.ok
    # Should have exactly 200 matches
    assert result.output.count("match line") == 200


@pytest.mark.asyncio
async def test_python_grep_200_limit_across_files(tmp_path: Path) -> None:
    """200-match limit triggers the outer break across multiple files."""
    from openclose.tool.tools.grep import _python_grep

    # Create multiple files, each with 150 matching lines
    for i in range(3):
        lines = [f"match_{i}_line_{j}" for j in range(150)]
        (tmp_path / f"file{i}.txt").write_text("\n".join(lines))

    result = await _python_grep("match_", str(tmp_path), "")
    assert result.ok
    assert result.output.count(":") <= 200 * 3  # at most 200 match lines


@pytest.mark.asyncio
async def test_python_grep_file_read_error(tmp_path: Path) -> None:
    """Python fallback handles OSError on file read gracefully."""
    from openclose.tool.tools.grep import _python_grep

    (tmp_path / "good.txt").write_text("match here\n")
    bad = tmp_path / "bad.txt"
    bad.write_text("match there\n")

    # Make the IgnoreManager not filter anything, then patch read_text to
    # fail for the bad file.
    original_read_text = Path.read_text

    def patched_read_text(self: Path, *a: Any, **kw: Any) -> str:
        if self.name == "bad.txt":
            raise OSError("permission denied")
        return original_read_text(self, *a, **kw)

    with patch.object(Path, "read_text", patched_read_text):
        result = await _python_grep("match", str(tmp_path), "")
    # Should still return matches from the good file
    assert result.ok
    assert "match here" in result.output


@pytest.mark.asyncio
async def test_python_grep_search_exception(tmp_path: Path) -> None:
    """Python fallback catches generic exceptions from glob."""
    from openclose.tool.tools.grep import _python_grep

    with patch.object(Path, "glob", side_effect=RuntimeError("boom")):
        result = await _python_grep("pattern", str(tmp_path), "")
    assert not result.ok
    assert "Search error" in result.error


# ── process.py: timeout with stderr content (line 110) ─────────────────────

@pytest.mark.asyncio
async def test_process_timeout_with_stderr() -> None:
    """On timeout, stderr content gets a newline before the timeout message."""
    from openclose.util.process import run

    result = await run(
        "bash", "-c", "echo err_output >&2 && sleep 30",
        timeout=1.0,
    )
    assert result.timed_out
    assert "err_output" in result.stderr
    assert "\n" in result.stderr
    assert "timed out" in result.stderr.lower()


@pytest.mark.asyncio
async def test_process_timeout_no_stderr() -> None:
    """On timeout with no stderr, no leading newline."""
    from openclose.util.process import run

    result = await run("sleep", "30", timeout=0.2)
    assert result.timed_out
    assert result.stderr.startswith("Process timed out")


# ── process.py: pending task cleanup (lines 122-131) ───────────────────────

@pytest.mark.asyncio
async def test_process_normal_completion() -> None:
    """Normal completion collects stdout and stderr."""
    from openclose.util.process import run

    result = await run("bash", "-c", "echo out && echo err >&2")
    assert result.ok
    assert "out" in result.stdout
    assert "err" in result.stderr
    assert not result.timed_out


# ── delegate.py: _run_subagent + execute ───────────────────────────────────

@pytest.mark.asyncio
async def test_delegate_no_tools_available(tmp_path: Path) -> None:
    """Delegate returns error when no allowed tools are in the registry."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry

    empty_registry = ToolRegistry()
    tool = make_delegate_tool(str(tmp_path), empty_registry)
    result = await tool.execute(mission_1="anything")
    assert not result.ok
    assert "No tools available" in result.error


@pytest.mark.asyncio
async def test_delegate_missing_missions() -> None:
    """Delegate rejects missing mission_1 and entries with empty/whitespace-only strings."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry

    tool = make_delegate_tool(".", ToolRegistry())

    # No mission_1 supplied
    result = await tool.execute()
    assert not result.ok
    assert "required" in result.error

    # Empty mission_1
    result = await tool.execute(mission_1="")
    assert not result.ok
    assert "non-empty" in result.error

    # Whitespace-only mission_1
    result = await tool.execute(mission_1="   ")
    assert not result.ok
    assert "non-empty" in result.error

    # Mixed: valid mission_1 + empty mission_2
    result = await tool.execute(mission_1="real", mission_2="")
    assert not result.ok
    assert "mission_2" in result.error
    assert "non-empty" in result.error


@pytest.mark.asyncio
async def test_delegate_subagent_text_events(tmp_path: Path) -> None:
    """Delegate _run_subagent processes text, tool_call, and tool_result events."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    # Create a registry with a read tool (allowed sub-tool)
    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(
        name="read",
        description="read a file",
        parameters=[],
        execute_fn=noop,
    ))
    registry.register(Tool(
        name="grep",
        description="search",
        parameters=[],
        execute_fn=noop,
    ))

    tool = make_delegate_tool(str(tmp_path), registry)

    # Mock AgentLoop.run to yield a text event then done
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
            content="<report>\nFindings: foo.py:1 — fine.\n</report>",
        )

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="map the repo")

    assert result.ok
    # Only content inside <report>...</report> is surfaced; pre-tool scratch is dropped.
    assert "Findings: foo.py:1" in result.output
    assert "Thinking before" not in result.output


@pytest.mark.asyncio
async def test_delegate_subagent_error_event(tmp_path: Path) -> None:
    """Delegate handles error events from sub-agent."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="read", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("error", error="something broke")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="investigate something")

    # Error event without any tool call → mission rejected as ungrounded.
    # The tool returns ok=True with a rejection notice (content the parent
    # can act on), and per_mission marks it as fallback.
    assert result.ok
    assert "Mission rejected" in result.output
    assert result.metadata["per_mission"][0]["fallback"] is True
    assert result.metadata["fallback"] is True


@pytest.mark.asyncio
async def test_delegate_subagent_exception(tmp_path: Path) -> None:
    """Delegate handles exceptions from sub-agent gracefully."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="read", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    async def mock_run(self: Any, msg: str) -> Any:
        raise RuntimeError("agent crashed")
        yield  # noqa: F841 — make it a generator

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="investigate something")

    # Crash before any tool call → ungrounded → rejection notice.
    assert result.ok
    assert "Mission rejected" in result.output
    assert result.metadata["per_mission"][0]["fallback"] is True


@pytest.mark.asyncio
async def test_delegate_subagent_uses_temperatures_delegate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The temperature passed to the sub-agent must come from
    ``[temperatures] delegate``, not from any ``[[agents]]`` block."""
    from openclose.agent.agent import Agent
    from openclose.agent.loop import StreamEvent
    from openclose.config.config import load_config
    from openclose.config.paths import ConfigPaths
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.tool.tools.delegate import make_delegate_tool

    monkeypatch.setattr(
        ConfigPaths,
        "user_config_path",
        classmethod(lambda cls: tmp_path / "nonexistent" / "config.toml"),  # type: ignore[arg-type,unused-ignore]
    )
    config_dir = tmp_path / ".openclose"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[temperatures]\ndelegate = 0.42\n")
    load_config(project_dir=tmp_path)

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    captured: dict[str, Agent] = {}
    real_init = __import__("openclose.agent.loop", fromlist=["AgentLoop"]).AgentLoop.__init__

    def spy_init(self: Any, *args: Any, **kwargs: Any) -> None:
        # Agent is the first positional or `agent=` kwarg
        if args:
            captured["agent"] = args[0]
        else:
            captured["agent"] = kwargs["agent"]
        real_init(self, *args, **kwargs)

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("text", content="<report>done</report>")

    with patch("openclose.agent.loop.AgentLoop.__init__", spy_init):
        with patch("openclose.provider.provider.get_provider") as mock_prov:
            mock_prov.return_value = MagicMock()
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                await tool.execute(mission_1="map the repo")

    # Reset the singleton for downstream tests.
    load_config()

    assert captured["agent"].temperature == 0.42


@pytest.mark.asyncio
async def test_delegate_invalid_budget() -> None:
    """Delegate rejects budgets that are not in the enum (case-sensitive)."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry

    tool = make_delegate_tool(".", ToolRegistry())
    # Wrong case should fail; values are lowercase.
    result = await tool.execute(mission_1="anything", budget="Default")
    assert not result.ok
    assert "budget" in result.error.lower()
    assert "default" in result.error  # error lists valid values
    assert "extended" in result.error


@pytest.mark.asyncio
async def test_delegate_budget_reminder_fires_once_at_95pct(tmp_path: Path) -> None:
    """When the sub-agent crosses 95% of its tool-call budget, the delegate
    calls `loop.request_user_nudge` exactly once with a reminder that
    references the usage and asks for a 'Stopping point' section.
    Subsequent tool calls past the threshold do NOT re-trigger it."""
    from openclose.tool.tools.delegate import (
        make_delegate_tool,
        _BUDGET_MAX_TOOL_CALLS,
        _BUDGET_REMINDER_PCT,
    )
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent, AgentLoop

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    cap = _BUDGET_MAX_TOOL_CALLS["default"]
    threshold = int(cap * _BUDGET_REMINDER_PCT)

    nudge_calls: list[str] = []
    real_request_nudge = AgentLoop.request_user_nudge

    def spy_request_nudge(self: AgentLoop, text: str) -> None:
        nudge_calls.append(text)
        real_request_nudge(self, text)

    tc_mock = MagicMock()
    tc_mock.name = "read"
    tc_mock.id = "tc-1"
    tc_mock.arguments_raw = "{}"

    # Yield a couple of tool calls past the threshold to verify the
    # reminder fires exactly once, not on every subsequent tool_result.
    async def mock_run(self: Any, msg: str) -> Any:
        for _ in range(threshold + 2):
            yield StreamEvent("tool_call", tool_call=tc_mock)
            yield StreamEvent("tool_result", tool_call=tc_mock, tool_result="ok")
        yield StreamEvent("text", content="<report>done</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch.object(AgentLoop, "request_user_nudge", spy_request_nudge):
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                result = await tool.execute(mission_1="x", budget="default")

    assert result.ok
    assert len(nudge_calls) == 1, (
        f"expected exactly one nudge, got {len(nudge_calls)}"
    )
    text = nudge_calls[0]
    assert "Budget reminder" in text
    assert "Stopping point" in text
    assert f"{threshold}/{cap}" in text


@pytest.mark.asyncio
async def test_delegate_budget_reminder_does_not_fire_below_threshold(tmp_path: Path) -> None:
    """A sub-agent that finishes well under the budget never triggers the
    reminder."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent, AgentLoop

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    nudge_calls: list[str] = []

    def spy_request_nudge(self: AgentLoop, text: str) -> None:
        nudge_calls.append(text)

    tc_mock = MagicMock()
    tc_mock.name = "read"
    tc_mock.id = "tc-1"
    tc_mock.arguments_raw = "{}"

    async def mock_run(self: Any, msg: str) -> Any:
        # 3 tool calls — well below 95% of 30.
        for _ in range(3):
            yield StreamEvent("tool_call", tool_call=tc_mock)
            yield StreamEvent("tool_result", tool_call=tc_mock, tool_result="ok")
        yield StreamEvent("text", content="<report>done</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch.object(AgentLoop, "request_user_nudge", spy_request_nudge):
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                result = await tool.execute(mission_1="x", budget="default")

    assert result.ok
    assert nudge_calls == []


@pytest.mark.asyncio
async def test_agent_loop_drains_pending_nudge_before_next_step(tmp_path: Path) -> None:
    """`request_user_nudge` queues a user message that the loop appends to
    `_messages` at the start of the next iteration, then clears the slot."""
    from openclose.agent.agent import Agent, AgentMode
    from openclose.agent.loop import AgentLoop

    agent = Agent(
        name="t", description="", model="x",
        max_steps=5, system_prompt="sp", mode=AgentMode.SUBAGENT,
    )
    loop = AgentLoop(
        agent=agent, provider=MagicMock(), tool_executor=None,
        tool_schemas=[], project_dir=str(tmp_path),
    )

    loop.request_user_nudge("please wrap up")
    assert loop._pending_nudge == "please wrap up"
    assert all(m.get("content") != "please wrap up" for m in loop._messages)

    # Simulate the drain block at the top of a loop iteration.
    if loop._pending_nudge is not None:
        loop._messages.append({"role": "user", "content": loop._pending_nudge})
        loop._pending_nudge = None

    assert loop._pending_nudge is None
    assert loop._messages[-1] == {"role": "user", "content": "please wrap up"}


@pytest.mark.asyncio
async def test_delegate_obsolete_mode_kwarg_is_ignored(tmp_path: Path) -> None:
    """The old `mode` parameter has been removed. If a parent agent still
    passes it (cached schema), `**_kwargs` absorbs it silently — no error,
    no branching of the system prompt."""
    from openclose.tool.tools.delegate import make_delegate_tool, _SUBAGENT_PROMPT
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent
    from openclose.agent import agent as agent_mod

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    captured: dict[str, str] = {}
    real_agent_cls = agent_mod.Agent

    def capturing_agent(*args: Any, **kwargs: Any) -> Any:
        captured["system_prompt"] = kwargs.get("system_prompt", "")
        return real_agent_cls(*args, **kwargs)

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("text", content="<report>r</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch.object(agent_mod, "Agent", side_effect=capturing_agent):
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                result = await tool.execute(mission_1="x", mode="ad_hoc")
    assert result.ok
    assert _SUBAGENT_PROMPT in captured["system_prompt"]

    # No `mode` argument — same trunk prompt, same success.
    captured.clear()
    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch.object(agent_mod, "Agent", side_effect=capturing_agent):
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                result = await tool.execute(mission_1="x")
    assert result.ok
    assert _SUBAGENT_PROMPT in captured["system_prompt"]
    # The tool schema must NOT advertise `mode` any more.
    schema = tool.to_schema()
    props = schema["parameters"]["properties"]
    assert "mode" not in props
    assert "mission_1" in props
    assert "missions" not in props  # array form is gone
    assert "mission" not in props  # singular form is gone too


@pytest.mark.asyncio
async def test_delegate_budget_sets_max_steps(tmp_path: Path) -> None:
    """Delegate threads budget into the sub-agent's max_steps as a safety
    ceiling above the tool-call budget — strict enforcement happens via
    cancel_event, not max_steps."""
    from openclose.tool.tools.delegate import (
        make_delegate_tool,
        _BUDGET_MAX_TOOL_CALLS,
        _STEP_SAFETY_MULTIPLIER,
    )
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent
    from openclose.agent import agent as agent_mod

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    captured: dict[str, int] = {}
    real_agent_cls = agent_mod.Agent

    def capturing_agent(*args: Any, **kwargs: Any) -> Any:
        captured["max_steps"] = kwargs.get("max_steps", -1)
        return real_agent_cls(*args, **kwargs)

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("text", content="<report>report</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch.object(agent_mod, "Agent", side_effect=capturing_agent):
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                result = await tool.execute(mission_1="anything", budget="default")

    assert result.ok
    expected = _BUDGET_MAX_TOOL_CALLS["default"] * _STEP_SAFETY_MULTIPLIER + 10
    assert captured["max_steps"] == expected

    # Extended budget threads through too.
    captured.clear()
    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch.object(agent_mod, "Agent", side_effect=capturing_agent):
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                result = await tool.execute(mission_1="anything", budget="extended")

    assert result.ok
    expected = _BUDGET_MAX_TOOL_CALLS["extended"] * _STEP_SAFETY_MULTIPLIER + 10
    assert captured["max_steps"] == expected


@pytest.mark.asyncio
async def test_delegate_pre_tool_text_does_not_become_report(tmp_path: Path) -> None:
    """Pre-tool-call scratch thinking must NOT be served as a report.

    Sub-agents routinely emit short interim text like 'Let me try X' before
    each tool call. Treating that as a fallback report poisons the parent's
    context with noise — we'd rather fall through to the breadcrumb path
    that surfaces tool calls + stop reason.
    """
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    tc_mock = MagicMock()
    tc_mock.name = "read"
    tc_mock.id = "tc-1"
    tc_mock.arguments_raw = '{"file_path":"foo.py"}'

    async def mock_run(self: Any, msg: str) -> Any:
        # Sub-agent emits scratch thinking, makes a tool call, then ends —
        # never emitting a <report>...</report> block.
        yield StreamEvent("text", content="Let me try a different approach:")
        yield StreamEvent("tool_call", tool_call=tc_mock)
        yield StreamEvent("tool_result", tool_call=tc_mock, tool_result="def parse(): ...")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="find the parser")

    assert result.ok
    # Scratch thinking is NOT promoted to a report.
    assert "Let me try a different approach" not in result.output
    # Breadcrumb fallback fires instead — names the tool that ran.
    assert "read" in result.output
    assert result.metadata.get("fallback") is True


@pytest.mark.asyncio
async def test_delegate_synthesises_report_from_steps(tmp_path: Path) -> None:
    """Subagent with zero text events still returns a breadcrumb report."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="grep", description="g", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    tc_mock = MagicMock()
    tc_mock.name = "grep"
    tc_mock.id = "tc-1"
    tc_mock.arguments_raw = '{"pattern":"_consume_fields"}'

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("tool_call", tool_call=tc_mock)
        yield StreamEvent("tool_result", tool_call=tc_mock, tool_result="docstring.py:140")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="find napoleon usages")

    assert result.ok
    # Fallback report names the tool that ran so the parent can pick up the trail.
    assert "grep" in result.output
    assert "_consume_fields" in result.output
    assert result.metadata.get("fallback") is True


# ── delegate.py: budget parameter ──────────────────────────────────────────

def _capture_agent_kwargs(captured: dict[str, Any]) -> Any:
    """Build a side_effect that captures the Agent constructor kwargs."""
    from openclose.agent import agent as agent_mod
    real_agent_cls = agent_mod.Agent

    def factory(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_agent_cls(*args, **kwargs)

    return factory


@pytest.mark.asyncio
async def test_delegate_uses_exploration_trunk(tmp_path: Path) -> None:
    """The single free-form trunk prompt is used; the rigid Map/Findings/
    Caveats skeleton is gone."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent
    from openclose.agent import agent as agent_mod

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    captured: dict[str, Any] = {}

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("text", content="<report>report</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch.object(agent_mod, "Agent", side_effect=_capture_agent_kwargs(captured)):
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                result = await tool.execute(mission_1="anything")

    assert result.ok
    sp = captured.get("system_prompt", "")
    assert "read-only sub-agent" in sp
    # Trunk frames the mission as free-form exploration and reminds the
    # sub-agent not to pivot into a generic project map.
    assert "exploration mission" in sp
    assert "Do NOT pivot" in sp
    # Caveats are still mentioned (closing block of the trunk).
    assert "Caveats" in sp
    # The old fixed report skeleton is gone from the trunk.
    assert "Required report structure" not in sp
    # Review-mode artefacts must not appear in the trunk anymore.
    assert "independent reviewer sub-agent" not in sp
    assert "## Scope" not in sp
    # Default budget is communicated.
    assert "Budget: default" in sp


@pytest.mark.asyncio
async def test_delegate_extended_budget_prompt(tmp_path: Path) -> None:
    """Extended budget swaps in the extended budget block which mentions
    findings as part of the expected output."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent
    from openclose.agent import agent as agent_mod

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    captured: dict[str, Any] = {}

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("text", content="<report>report</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch.object(agent_mod, "Agent", side_effect=_capture_agent_kwargs(captured)):
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                result = await tool.execute(mission_1="x", budget="extended")

    assert result.ok
    sp = captured.get("system_prompt", "")
    assert "Budget: extended" in sp
    # Extended budget explicitly tells the sub-agent to surface findings.
    assert "findings" in sp.lower()


@pytest.mark.asyncio
async def test_delegate_empty_budget_falls_back(tmp_path: Path) -> None:
    """Empty/whitespace budget falls back to default without erroring."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent
    from openclose.agent import agent as agent_mod

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    captured: dict[str, Any] = {}

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("text", content="<report>report</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch.object(agent_mod, "Agent", side_effect=_capture_agent_kwargs(captured)):
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                result = await tool.execute(mission_1="anything", budget="")

    assert result.ok  # empty falls back, no error
    sp = captured.get("system_prompt", "")
    assert "Budget: default" in sp


@pytest.mark.asyncio
async def test_delegate_default_label(tmp_path: Path) -> None:
    """Subagent step metadata carries a per-mission `Mission i/N` label so the
    UI groups steps by mission."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))

    tool = make_delegate_tool(str(tmp_path), registry)

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("text", content="<report>map report</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="map the repo")

    assert result.ok
    steps = result.metadata.get("subagent_steps", [])
    assert steps, "expected at least one recorded step"
    assert all(s.get("subagent_label") == "Mission 1/1" for s in steps)


# ── delegate.py: report-marker extraction ─────────────────────────────────


@pytest.mark.asyncio
async def test_delegate_extracts_report_from_markers(tmp_path: Path) -> None:
    """Final text wrapped in <report>...</report> is unwrapped; pre-tag
    scratch thinking is discarded."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    final = (
        "Let me also check edge case Y... and Z...\n"
        "Now let me think about W...\n"
        "<report>\n## Scope\nReviewed foo.py:10-50.\n\n## Findings\nNo issues.\n</report>\n"
        "(end of analysis)"
    )

    tc = MagicMock()
    tc.name = "read"
    tc.id = "tc-rfm"
    tc.arguments_raw = '{"file_path":"foo.py"}'

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("tool_call", tool_call=tc)
        yield StreamEvent("tool_result", tool_call=tc, tool_result="content")
        yield StreamEvent("text", content=final)

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="review foo.py")

    assert result.ok
    out = result.output
    assert "## Scope" in out
    assert "## Findings" in out
    # Pre- and post-marker chatter is stripped.
    assert "edge case Y" not in out
    assert "end of analysis" not in out


@pytest.mark.asyncio
async def test_delegate_marker_missing_uses_breadcrumb(tmp_path: Path) -> None:
    """When the model ignores <report> markers, the text is treated as
    scratch thinking and the breadcrumb fallback fires. Free-form text
    without markers is unreliable as a report — interim chatter and
    final summaries can't be told apart, so we don't try."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    final = "## Findings\nfoo.py:10 — looks fine."

    tc = MagicMock()
    tc.name = "read"
    tc.id = "tc-x"
    tc.arguments_raw = "{}"

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("tool_call", tool_call=tc)
        yield StreamEvent("tool_result", tool_call=tc, tool_result="ok")
        yield StreamEvent("text", content=final)

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="x")

    assert result.ok
    assert result.metadata.get("fallback") is True
    # Marker-less text is dropped — only the breadcrumb survives.
    assert "foo.py:10" not in result.output
    assert "read" in result.output


@pytest.mark.asyncio
async def test_delegate_rejects_zero_tool_call_report(tmp_path: Path) -> None:
    """A sub-agent that emits a <report> without making any tool call
    cannot have grounded it — the content is fabricated from prior
    knowledge of similar projects, not facts about this repo. Reject it
    outright: discard the report, mark the mission as fallback, and emit
    a clear rejection notice instead."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    # Sub-agent emits only text — no tool_call events.
    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent(
            "text",
            content="<report>\n## Scope\nReviewed nothing.\n</report>",
        )

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="review the diff")

    assert result.ok
    assert result.metadata.get("tool_call_count") == 0
    # Fabricated content is discarded — the rejection notice replaces it.
    assert "Reviewed nothing" not in result.output
    assert "## Scope" not in result.output
    assert "Mission rejected" in result.output
    assert "zero tool calls" in result.output
    # The mission is marked as fallback (degraded) so the parent can detect it.
    pm = result.metadata["per_mission"][0]
    assert pm["fallback"] is True
    assert pm["tool_call_count"] == 0
    assert "rejected" in pm["stop_reason"].lower()


@pytest.mark.asyncio
async def test_delegate_no_warning_when_tool_calls_made(tmp_path: Path) -> None:
    """When the sub-agent did use tools, no hallucination warning fires."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    tc = MagicMock()
    tc.name = "read"
    tc.id = "tc-1"
    tc.arguments_raw = '{"f":"x"}'

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("tool_call", tool_call=tc)
        yield StreamEvent("tool_result", tool_call=tc, tool_result="contents")
        yield StreamEvent(
            "text", content="<report>\n## Findings\nfoo.py:1 — fine.\n</report>",
        )

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="x")

    assert result.ok
    assert result.metadata.get("tool_call_count") == 1
    assert "WARNING" not in result.output
    assert "## Findings" in result.output


# ── delegate.py: error path preserves last_text_block + surfaces reason ───


@pytest.mark.asyncio
async def test_delegate_error_path_uses_breadcrumb(tmp_path: Path) -> None:
    """When the loop yields an error mid-investigation and no <report>
    block was emitted, the breadcrumb fallback fires and surfaces the
    stop reason (so the parent can tell 'budget hit' from 'provider
    broke')."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    tc = MagicMock()
    tc.name = "read"
    tc.id = "tc-x"
    tc.arguments_raw = "{}"

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("text", content="Investigating the parse_args function.")
        yield StreamEvent("tool_call", tool_call=tc)
        yield StreamEvent("tool_result", tool_call=tc, tool_result="def parse_args(): ...")
        yield StreamEvent("error", error="Max steps (150) reached")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="find parse_args")

    assert result.ok
    # Pre-tool scratch is dropped — the breadcrumb names the tool that ran.
    assert "Investigating the parse_args function" not in result.output
    assert "read" in result.output
    assert result.metadata.get("fallback") is True
    # stop_reason now lives per-mission.
    assert result.metadata["per_mission"][0]["stop_reason"] == "Max steps (150) reached"


@pytest.mark.asyncio
async def test_delegate_error_no_text_surfaces_reason_in_fallback(tmp_path: Path) -> None:
    """When the sub-agent produced tool calls but no text and then errored,
    the breadcrumb fallback header names the stop reason so the parent
    can tell 'budget hit' from 'provider broke'."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="grep", description="g", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    tc = MagicMock()
    tc.name = "grep"
    tc.id = "tc-1"
    tc.arguments_raw = '{"pattern":"foo"}'

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("tool_call", tool_call=tc)
        yield StreamEvent("tool_result", tool_call=tc, tool_result="bar.py:10")
        yield StreamEvent("error", error="Provider timeout")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="find foo")

    assert result.ok
    assert result.metadata.get("fallback") is True
    assert result.metadata["per_mission"][0]["stop_reason"] == "Provider timeout"
    # The headline names the reason so the parent doesn't have to guess.
    assert "Provider timeout" in result.output


# ── delegate.py: tool-call budget enforced via cancel_event ───────────────


@pytest.mark.asyncio
async def test_delegate_enforces_tool_call_budget(tmp_path: Path) -> None:
    """Once the tool-call cap is hit, cancel_event is set and the loop is
    expected to exit cleanly. The output reflects the budget reason."""
    from openclose.tool.tools.delegate import (
        make_delegate_tool,
        _BUDGET_MAX_TOOL_CALLS,
    )
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent, AgentLoop

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="grep", description="g", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    cap = _BUDGET_MAX_TOOL_CALLS["default"]

    # Mock run yields cap+5 tool_call/tool_result pairs and never produces
    # final text. We expect cancel_event to be set on the loop instance
    # after the cap-th tool_result.
    captured_cancel: dict[str, Any] = {}
    real_init = AgentLoop.__init__

    def init_capture(self: Any, *args: Any, **kwargs: Any) -> None:
        captured_cancel["event"] = kwargs.get("cancel_event")
        real_init(self, *args, **kwargs)

    async def mock_run(self: Any, msg: str) -> Any:
        for i in range(cap + 5):
            tc = MagicMock()
            tc.name = "grep"
            tc.id = f"tc-{i}"
            tc.arguments_raw = "{}"
            yield StreamEvent("tool_call", tool_call=tc)
            yield StreamEvent("tool_result", tool_call=tc, tool_result=f"result {i}")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch.object(AgentLoop, "__init__", init_capture):
            with patch("openclose.agent.loop.AgentLoop.run", mock_run):
                result = await tool.execute(mission_1="x", budget="default")

    # cancel_event was wired in
    ev = captured_cancel.get("event")
    assert ev is not None
    assert ev.is_set(), "cancel_event must fire when tool-call cap is hit"

    # Output is a fallback (no final text) with budget reason in metadata.
    assert result.ok
    assert result.metadata.get("fallback") is True
    sr = result.metadata["per_mission"][0]["stop_reason"]
    assert "Tool-call budget" in sr
    assert f"({cap})" in sr


# ── delegate.py: concurrent missions in a single call ─────────────────────


@pytest.mark.asyncio
async def test_delegate_runs_multiple_missions_concurrently(tmp_path: Path) -> None:
    """Three missions in one call → three sub-agents run; their reports are
    each wrapped in a `=== Mission i/N ===` header in the combined output;
    every recorded step carries the per-mission `Mission i/N` label so the
    UI groups them cleanly; metadata aggregates per-mission stop_reasons
    and the global tool_call_count."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    call_count = {"n": 0}

    async def mock_run(self: Any, msg: str) -> Any:
        call_count["n"] += 1
        tc = MagicMock()
        tc.name = "read"
        tc.id = f"tc-{call_count['n']}"
        tc.arguments_raw = "{}"
        yield StreamEvent("tool_call", tool_call=tc)
        yield StreamEvent("tool_result", tool_call=tc, tool_result="ok")
        body = msg.split("Mission: ", 1)[-1]
        yield StreamEvent("text", content=f"<report>REPORT_FOR[{body}]</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="alpha", mission_2="beta", mission_3="gamma")

    assert result.ok
    out = result.output
    assert "=== Mission 1/3 ===" in out
    assert "=== Mission 2/3 ===" in out
    assert "=== Mission 3/3 ===" in out
    assert "REPORT_FOR[alpha]" in out
    assert "REPORT_FOR[beta]" in out
    assert "REPORT_FOR[gamma]" in out

    steps = result.metadata.get("subagent_steps", [])
    labels = {s.get("subagent_label") for s in steps}
    assert labels == {"Mission 1/3", "Mission 2/3", "Mission 3/3"}

    per_mission = result.metadata.get("per_mission", [])
    assert [p["index"] for p in per_mission] == [1, 2, 3]
    assert all(p["fallback"] is False for p in per_mission)
    assert all(p["tool_call_count"] == 1 for p in per_mission)
    assert result.metadata["tool_call_count"] == 3
    assert "fallback" not in result.metadata


@pytest.mark.asyncio
async def test_delegate_one_mission_fallback_others_succeed(tmp_path: Path) -> None:
    """One mission emits a real <report>, the other only tool calls. The
    combined output keeps both — the real report verbatim and a breadcrumb
    for the other. Top-level `fallback` is NOT set because not every mission
    fell back."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="grep", description="g", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    async def mock_run(self: Any, msg: str) -> Any:
        tc = MagicMock()
        tc.name = "grep"
        tc.arguments_raw = "{}"
        if "alpha" in msg:
            tc.id = "tc-a"
            yield StreamEvent("tool_call", tool_call=tc)
            yield StreamEvent("tool_result", tool_call=tc, tool_result="hit")
            yield StreamEvent("text", content="<report>ALPHA_REPORT</report>")
        else:
            tc.id = "tc-b"
            tc.arguments_raw = '{"q":"beta"}'
            yield StreamEvent("tool_call", tool_call=tc)
            yield StreamEvent("tool_result", tool_call=tc, tool_result="found")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="alpha", mission_2="beta")

    assert result.ok
    out = result.output
    assert "ALPHA_REPORT" in out
    assert "Fallback report" in out
    assert "grep" in out

    per_mission = result.metadata["per_mission"]
    assert per_mission[0]["fallback"] is False
    assert per_mission[1]["fallback"] is True
    assert "fallback" not in result.metadata


@pytest.mark.asyncio
async def test_delegate_rejects_missing_mission_1() -> None:
    """`mission_1` is required — calls without it (or with an empty/whitespace
    string) are a parent bug worth surfacing."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry

    tool = make_delegate_tool(".", ToolRegistry())
    result = await tool.execute()
    assert not result.ok
    assert "mission_1" in result.error
    assert "required" in result.error

    result = await tool.execute(mission_1="")
    assert not result.ok
    assert "mission_1" in result.error
    assert "non-empty" in result.error

    result = await tool.execute(mission_1="   ")
    assert not result.ok
    assert "mission_1" in result.error
    assert "non-empty" in result.error


@pytest.mark.asyncio
async def test_delegate_rejects_empty_string_in_optional_slot() -> None:
    """An empty or whitespace-only string in `mission_2`/`mission_3` is a
    parent bug — surface it rather than silently filtering."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry

    tool = make_delegate_tool(".", ToolRegistry())
    result = await tool.execute(mission_1="valid mission", mission_2="  ")
    assert not result.ok
    assert "mission_2" in result.error
    assert "non-empty" in result.error


@pytest.mark.asyncio
async def test_delegate_all_missions_fallback_sets_top_level_flag(tmp_path: Path) -> None:
    """When every mission falls back, the top-level `fallback=True` flag is
    set so the parent can detect a fully-degraded run with one check."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    async def mock_run(self: Any, msg: str) -> Any:
        tc = MagicMock()
        tc.name = "read"
        tc.id = "tc"
        tc.arguments_raw = "{}"
        yield StreamEvent("tool_call", tool_call=tc)
        yield StreamEvent("tool_result", tool_call=tc, tool_result="x")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="a", mission_2="b")

    assert result.ok
    assert result.metadata["fallback"] is True
    assert all(p["fallback"] is True for p in result.metadata["per_mission"])


@pytest.mark.asyncio
async def test_delegate_all_missions_zero_tool_calls_each_rejected(tmp_path: Path) -> None:
    """When every mission ends with zero tool calls (e.g. provider error
    before any tool fired), each one gets the rejection notice and the
    top-level fallback flag is set. The tool still returns ok=True so the
    parent sees the structured rejections rather than a bare error."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    async def mock_run(self: Any, msg: str) -> Any:
        yield StreamEvent("error", error="provider unreachable")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="a", mission_2="b")

    assert result.ok
    out = result.output
    assert out.count("Mission rejected") == 2
    assert result.metadata["fallback"] is True
    assert all(p["fallback"] is True for p in result.metadata["per_mission"])
    assert all(p["tool_call_count"] == 0 for p in result.metadata["per_mission"])


@pytest.mark.asyncio
async def test_delegate_one_mission_exception_does_not_abort_siblings(
    tmp_path: Path,
) -> None:
    """An exception in one mission's loop is isolated to that mission;
    sibling missions keep running and their reports are preserved."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    async def mock_run(self: Any, msg: str) -> Any:
        if "boom" in msg:
            raise RuntimeError("synthetic mission crash")
            yield  # noqa: F841 — make this a generator
        tc = MagicMock()
        tc.name = "read"
        tc.id = "tc"
        tc.arguments_raw = "{}"
        yield StreamEvent("tool_call", tool_call=tc)
        yield StreamEvent("tool_result", tool_call=tc, tool_result="ok")
        yield StreamEvent("text", content="<report>SAFE_REPORT</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="safe", mission_2="boom")

    assert result.ok
    assert "SAFE_REPORT" in result.output
    per_mission = result.metadata["per_mission"]
    assert per_mission[0]["fallback"] is False
    assert "synthetic mission crash" in per_mission[1]["stop_reason"]


@pytest.mark.asyncio
async def test_delegate_schema_advertises_three_string_mission_slots() -> None:
    """The tool schema must expose mission_1/2/3 as strings, with mission_1
    required and 2/3 optional, and no array `missions`, no singular
    `mission`, no obsolete `mode`."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry

    tool = make_delegate_tool(".", ToolRegistry())
    schema = tool.to_schema()
    props = schema["parameters"]["properties"]
    for slot in ("mission_1", "mission_2", "mission_3"):
        assert slot in props
        assert props[slot]["type"] == "string"
    assert "missions" not in props
    assert "mission" not in props
    assert "mode" not in props
    required = schema["parameters"]["required"]
    assert "mission_1" in required
    assert "mission_2" not in required
    assert "mission_3" not in required
    assert props["budget"]["enum"] == ["default", "extended"]
    assert "budget" not in required


# ── delegate.py: sparse-slot semantics ─────────────────────────────────────


@pytest.mark.asyncio
async def test_delegate_rejects_mission_3_alone_without_mission_1() -> None:
    """`mission_1` is required by the schema. A call that supplies only
    `mission_3` (or only `mission_2`) violates the schema contract and
    must be rejected with a clear error — not silently treated as a
    one-mission call."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry

    tool = make_delegate_tool(".", ToolRegistry())

    result = await tool.execute(mission_3="probe")
    assert not result.ok
    assert "mission_1" in result.error
    assert "required" in result.error

    result = await tool.execute(mission_2="probe")
    assert not result.ok
    assert "mission_1" in result.error
    assert "required" in result.error


@pytest.mark.asyncio
async def test_delegate_sparse_slot_mission_1_and_3_skipping_2_runs_two(
    tmp_path: Path,
) -> None:
    """`mission_1` + `mission_3` (skipping `mission_2`) runs both as a
    two-mission call. The hole between slots is collapsed: presented
    missions occupy positions 1 and 2, labelled `Mission 1/2` and
    `Mission 2/2`, with `mission_text` preserving the original slot
    order (mission_1 first, mission_3 second)."""
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tool import Tool, ToolResult as TR
    from openclose.agent.loop import StreamEvent

    registry = ToolRegistry()

    async def noop(**kw: object) -> TR:
        return TR(output="ok")

    registry.register(Tool(name="read", description="r", parameters=[], execute_fn=noop))
    tool = make_delegate_tool(str(tmp_path), registry)

    call_count = {"n": 0}

    async def mock_run(self: Any, msg: str) -> Any:
        call_count["n"] += 1
        tc = MagicMock()
        tc.name = "read"
        tc.id = f"tc-{call_count['n']}"
        tc.arguments_raw = "{}"
        yield StreamEvent("tool_call", tool_call=tc)
        yield StreamEvent("tool_result", tool_call=tc, tool_result="ok")
        body = msg.split("Mission: ", 1)[-1]
        yield StreamEvent("text", content=f"<report>R[{body}]</report>")

    with patch("openclose.provider.provider.get_provider") as mock_prov:
        mock_prov.return_value = MagicMock()
        with patch("openclose.agent.loop.AgentLoop.run", mock_run):
            result = await tool.execute(mission_1="a", mission_3="c")

    assert result.ok
    assert "=== Mission 1/2 ===" in result.output
    assert "=== Mission 2/2 ===" in result.output
    assert "R[a]" in result.output
    assert "R[c]" in result.output
    per_mission = result.metadata["per_mission"]
    assert len(per_mission) == 2
    assert [p["mission_text"] for p in per_mission] == ["a", "c"]
    assert [p["label"] for p in per_mission] == ["Mission 1/2", "Mission 2/2"]

