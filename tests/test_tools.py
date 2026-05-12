"""Tests for the tool system."""

from __future__ import annotations

from pathlib import Path

import pytest

from openclose.tool.tool import Tool, ToolResult, ToolParameter
from openclose.tool.registry import ToolRegistry
from openclose.tool.truncation import truncate_output
from openclose.tool.tools.read import make_read_tool
from openclose.tool.tools.write import make_write_tool
from openclose.tool.tools.edit import make_edit_tool
from openclose.tool.tools.glob import make_glob_tool
from openclose.tool.tools.grep import make_grep_tool
from openclose.tool.tools.bash import make_bash_tool
from openclose.tool.tools.plan import make_plan_tool
from openclose.tool.tools.ask_user import make_ask_user_tool
from openclose.tool.tools import register_all_tools


# --- Tool framework ---

def test_tool_result() -> None:
    ok = ToolResult(output="hello")
    assert ok.ok
    assert ok.to_string() == "hello"

    err = ToolResult(error="fail")
    assert not err.ok
    assert "fail" in err.to_string()


def test_tool_schema() -> None:
    tool = Tool(
        name="test",
        description="A test tool",
        parameters=[
            ToolParameter(name="input", description="The input"),
        ],
    )
    schema = tool.to_schema()
    assert schema["name"] == "test"
    assert "input" in schema["parameters"]["properties"]


def test_truncate_output_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(1000))
    truncated = truncate_output(text, max_lines=10)
    assert "990 more lines truncated" in truncated


def test_truncate_output_bytes() -> None:
    text = "x" * 200_000
    truncated = truncate_output(text, max_bytes=1000)
    assert len(truncated) < 200_000


# --- Registry ---

@pytest.mark.asyncio
async def test_registry_execute() -> None:
    registry = ToolRegistry()

    async def my_fn(**kwargs: object) -> ToolResult:
        return ToolResult(output="ok")

    registry.register(Tool(name="my_tool", description="test", execute_fn=my_fn))
    result = await registry.execute("my_tool", {})
    assert result.output == "ok"
    assert result.ok


@pytest.mark.asyncio
async def test_registry_unknown_tool() -> None:
    registry = ToolRegistry()
    result = await registry.execute("nonexistent", {})
    assert "Unknown tool" in result.error


def test_registry_schemas() -> None:
    registry = ToolRegistry()
    register_all_tools(registry, "/tmp")
    schemas = registry.get_schemas()
    assert len(schemas) > 5
    names = [s["name"] for s in schemas]
    assert "read" in names
    assert "write" in names
    assert "bash" in names


# --- Read tool ---

@pytest.mark.asyncio
async def test_read_tool(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("line1\nline2\nline3\n")
    tool = make_read_tool(str(tmp_path))
    result = await tool.execute(file_path=str(f))
    assert result.ok
    assert "line1" in result.output
    assert "line2" in result.output


@pytest.mark.asyncio
async def test_read_tool_missing_file(tmp_path: Path) -> None:
    tool = make_read_tool(str(tmp_path))
    result = await tool.execute(file_path=str(tmp_path / "nope.txt"))
    assert not result.ok


@pytest.mark.asyncio
async def test_read_tool_offset(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("\n".join(f"line{i}" for i in range(100)))
    tool = make_read_tool(str(tmp_path))
    result = await tool.execute(file_path=str(f), offset=50, limit=5)
    assert result.ok
    assert "line50" in result.output
    assert "line55" not in result.output


# --- Write tool ---

@pytest.mark.asyncio
async def test_write_tool(tmp_path: Path) -> None:
    tool = make_write_tool(str(tmp_path))
    result = await tool.execute(
        file_path=str(tmp_path / "out.txt"),
        content="hello\nworld\n",
    )
    assert result.ok
    assert (tmp_path / "out.txt").read_text() == "hello\nworld\n"


@pytest.mark.asyncio
async def test_write_tool_creates_dirs(tmp_path: Path) -> None:
    tool = make_write_tool(str(tmp_path))
    result = await tool.execute(
        file_path=str(tmp_path / "sub" / "dir" / "file.txt"),
        content="nested",
    )
    assert result.ok
    assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"


# --- Edit tool ---

@pytest.mark.asyncio
async def test_edit_tool(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("def foo():\n    return 1\n")
    tool = make_edit_tool(str(tmp_path))
    result = await tool.execute(
        file_path=str(f),
        old_string="return 1",
        new_string="return 42",
    )
    assert result.ok
    assert "return 42" in f.read_text()


@pytest.mark.asyncio
async def test_edit_tool_not_found(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("hello")
    tool = make_edit_tool(str(tmp_path))
    result = await tool.execute(
        file_path=str(f),
        old_string="goodbye",
        new_string="hi",
    )
    assert not result.ok
    assert "not found" in result.error


@pytest.mark.asyncio
async def test_edit_tool_multiple_matches(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("a = 1\nb = 1\n")
    tool = make_edit_tool(str(tmp_path))
    result = await tool.execute(
        file_path=str(f),
        old_string="= 1",
        new_string="= 2",
    )
    assert not result.ok
    assert "2 times" in result.error


# --- Glob tool ---

@pytest.mark.asyncio
async def test_glob_tool(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    tool = make_glob_tool(str(tmp_path))
    result = await tool.execute(pattern="*.py")
    assert result.ok
    assert "a.py" in result.output
    assert "b.py" in result.output
    assert "c.txt" not in result.output


# --- Grep tool ---

@pytest.mark.asyncio
async def test_grep_tool(tmp_path: Path) -> None:
    (tmp_path / "test.py").write_text("def hello():\n    return 'world'\n")
    tool = make_grep_tool(str(tmp_path))
    result = await tool.execute(pattern="hello")
    assert result.ok
    assert "hello" in result.output


# --- Bash tool ---

@pytest.mark.asyncio
async def test_bash_tool() -> None:
    tool = make_bash_tool("/tmp")
    result = await tool.execute(command="echo hello")
    assert result.ok
    assert "hello" in result.output
    assert "$ echo hello" in result.output
    assert "[cwd: /tmp]" in result.output


@pytest.mark.asyncio
async def test_bash_tool_failure() -> None:
    tool = make_bash_tool("/tmp")
    result = await tool.execute(command="exit 1")
    assert not result.ok


# --- Plan tool ---

@pytest.mark.asyncio
async def test_plan_tool(tmp_path: Path) -> None:
    from openclose.tool.registry import ToolRegistry
    tool = make_plan_tool(str(tmp_path), ToolRegistry())
    result = await tool.execute(content="## Step 1\nDo the thing", phase="final")
    assert result.ok
    assert result.metadata.get("awaiting_plan_review") is True
    assert result.metadata.get("plan_content") == "## Step 1\nDo the thing"


# --- Ask user tool ---

@pytest.mark.asyncio
async def test_ask_user_tool() -> None:
    tool = make_ask_user_tool()
    result = await tool.execute(questions=[
        {"question": "Favorite color?", "choices": ["Red", "Blue", "Green"]},
    ])
    assert result.ok
    assert result.metadata.get("awaiting_ask_user") is True
    assert len(result.metadata.get("questions", [])) == 1


@pytest.mark.asyncio
async def test_ask_user_tool_empty() -> None:
    tool = make_ask_user_tool()
    result = await tool.execute(questions=[])
    assert not result.ok


@pytest.mark.asyncio
async def test_ask_user_tool_too_many() -> None:
    tool = make_ask_user_tool()
    questions = [{"question": f"Q{i}?", "choices": ["A", "B"]} for i in range(11)]
    result = await tool.execute(questions=questions)
    assert not result.ok


@pytest.mark.asyncio
async def test_ask_user_tool_missing_choices() -> None:
    tool = make_ask_user_tool()
    result = await tool.execute(questions=[{"question": "Q?", "choices": []}])
    assert not result.ok
