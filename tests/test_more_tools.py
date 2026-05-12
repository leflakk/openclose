"""Additional tool tests for coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openclose.tool.tool import Tool, ToolResult
from openclose.tool.registry import ToolRegistry
from openclose.tool.tools.ask_user import make_ask_user_tool
from openclose.tool.tools.ask_user_broker import AskUserBroker
from openclose.tool.tools.read import make_read_tool
from openclose.tool.tools.write import make_write_tool
from openclose.tool.tools.edit import make_edit_tool
from openclose.tool.tools.glob import make_glob_tool
from openclose.tool.tools.grep import make_grep_tool
from openclose.tool.tools.bash import make_bash_tool


# --- read relative path ---

@pytest.mark.asyncio
async def test_read_relative_path(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("world")
    tool = make_read_tool(str(tmp_path))
    result = await tool.execute(file_path="hello.txt")
    assert result.ok
    assert "world" in result.output


# --- write relative path ---

@pytest.mark.asyncio
async def test_write_relative_path(tmp_path: Path) -> None:
    tool = make_write_tool(str(tmp_path))
    result = await tool.execute(file_path="rel.txt", content="data")
    assert result.ok
    assert (tmp_path / "rel.txt").read_text() == "data"


# --- edit relative path and file not found ---

@pytest.mark.asyncio
async def test_edit_relative_path(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("old_value = 1")
    tool = make_edit_tool(str(tmp_path))
    result = await tool.execute(
        file_path="code.py", old_string="old_value", new_string="new_value"
    )
    assert result.ok


@pytest.mark.asyncio
async def test_edit_missing_file(tmp_path: Path) -> None:
    tool = make_edit_tool(str(tmp_path))
    result = await tool.execute(
        file_path="nope.py", old_string="x", new_string="y"
    )
    assert not result.ok


# --- glob relative ---

@pytest.mark.asyncio
async def test_glob_no_match(tmp_path: Path) -> None:
    tool = make_glob_tool(str(tmp_path))
    result = await tool.execute(pattern="*.xyz")
    assert "No files matched" in result.output


@pytest.mark.asyncio
async def test_glob_relative_base(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.py").write_text("")
    tool = make_glob_tool(str(tmp_path))
    result = await tool.execute(pattern="*.py", path="sub")
    assert result.ok


# --- bash empty command ---

@pytest.mark.asyncio
async def test_bash_empty() -> None:
    tool = make_bash_tool("/tmp")
    result = await tool.execute(command="")
    assert not result.ok


@pytest.mark.asyncio
async def test_bash_with_stderr() -> None:
    tool = make_bash_tool("/tmp")
    result = await tool.execute(command="echo err >&2")
    assert "[stderr]" in result.output
    assert "err" in result.output
    assert "$ echo err >&2" in result.output


# --- tool without execute fn ---

@pytest.mark.asyncio
async def test_tool_no_execute() -> None:
    tool = Tool(name="noop", description="noop")
    result = await tool.execute()
    assert not result.ok


# --- registry ---

def test_registry_get_tool() -> None:
    registry = ToolRegistry()
    registry.register(Tool(name="mytool", description="desc"))
    assert registry.get("mytool") is not None
    assert registry.get("missing") is None


def test_registry_list() -> None:
    registry = ToolRegistry()
    registry.register(Tool(name="a", description=""))
    registry.register(Tool(name="b", description=""))
    assert len(registry.list_tools()) == 2


@pytest.mark.asyncio
async def test_registry_execute_error() -> None:
    registry = ToolRegistry()

    async def bad_fn(**kwargs: object) -> ToolResult:
        raise RuntimeError("boom")

    registry.register(Tool(name="bad", description="", execute_fn=bad_fn))
    result = await registry.execute("bad", {})
    assert "Error" in result.error


# --- grep fallback python ---

@pytest.mark.asyncio
async def test_grep_pattern_in_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def hello():\n    pass\n")
    (tmp_path / "b.py").write_text("def world():\n    pass\n")
    tool = make_grep_tool(str(tmp_path))
    result = await tool.execute(pattern="world", include="*.py")
    assert result.ok
    assert "world" in result.output


@pytest.mark.asyncio
async def test_grep_no_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("nothing here")
    tool = make_grep_tool(str(tmp_path))
    result = await tool.execute(pattern="zzzznotfound")
    assert "No matches" in result.output


# --- ask_user tool ---


@pytest.mark.asyncio
async def test_ask_user_single_choice_rejected() -> None:
    tool = make_ask_user_tool()
    result = await tool.execute(questions=[{"question": "Q?", "choices": ["Only"]}])
    assert not result.ok


@pytest.mark.asyncio
async def test_ask_user_tool_max_questions() -> None:
    tool = make_ask_user_tool()
    questions = [{"question": f"Q{i}?", "choices": ["A", "B"]} for i in range(10)]
    result = await tool.execute(questions=questions)
    assert result.ok
    assert result.metadata.get("awaiting_ask_user") is True


@pytest.mark.asyncio
async def test_ask_user_broker_reply() -> None:
    broker = AskUserBroker()

    async def reply_later() -> None:
        await asyncio.sleep(0.05)
        broker.reply("req1", [{"question": "Q?", "answer": "A"}])

    asyncio.create_task(reply_later())
    result = await broker.ask("req1", [{"question": "Q?", "choices": ["A", "B"]}])
    assert result.answers == [{"question": "Q?", "answer": "A"}]


def test_ask_user_broker_reply_unknown() -> None:
    broker = AskUserBroker()
    assert broker.reply("nonexistent", []) is False
