"""Final edge case tests to push coverage over 80%."""

from __future__ import annotations

from pathlib import Path

import pytest

from openclose.tool.tools.grep import _python_grep
from openclose.tool.tools.edit import make_edit_tool
from openclose.tool.tools.glob import make_glob_tool
from openclose.tool.tools.read import make_read_tool
from openclose.tool.tools.write import make_write_tool
from openclose.tool.tools.plan import make_plan_tool
from openclose.file.binary import is_binary
from openclose.file.diff import DiffTracker
from openclose.file.ignore import IgnoreManager
from openclose.permission.permission import PermissionEngine


# --- python grep fallback ---

@pytest.mark.asyncio
async def test_python_grep_basic(tmp_path: Path) -> None:
    (tmp_path / "file.py").write_text("hello world\nfoo bar\n")
    result = await _python_grep("hello", str(tmp_path), "")
    assert result.ok
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_python_grep_no_match(tmp_path: Path) -> None:
    (tmp_path / "file.py").write_text("nothing here\n")
    result = await _python_grep("zzzzz", str(tmp_path), "")
    assert "No matches" in result.output


@pytest.mark.asyncio
async def test_python_grep_invalid_regex(tmp_path: Path) -> None:
    result = await _python_grep("[invalid", str(tmp_path), "")
    assert not result.ok


@pytest.mark.asyncio
async def test_python_grep_with_include(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("match\n")
    (tmp_path / "b.txt").write_text("match\n")
    result = await _python_grep("match", str(tmp_path), "*.py")
    assert result.ok


@pytest.mark.asyncio
async def test_python_grep_single_file(tmp_path: Path) -> None:
    f = tmp_path / "page.md"
    f.write_text("alpha\nfind-me\nbeta\n")
    result = await _python_grep("find-me", str(f), "")
    assert result.ok
    assert "find-me" in result.output
    assert ":2:" in result.output


@pytest.mark.asyncio
async def test_python_grep_missing_path(tmp_path: Path) -> None:
    result = await _python_grep("x", str(tmp_path / "does_not_exist"), "")
    assert not result.ok
    assert "not found" in (result.error or "").lower()


# --- edit write errors ---

@pytest.mark.asyncio
async def test_edit_relative_missing(tmp_path: Path) -> None:
    tool = make_edit_tool(str(tmp_path))
    result = await tool.execute(file_path="missing.py", old_string="x", new_string="y")
    assert not result.ok


# --- glob relative and errors ---

@pytest.mark.asyncio
async def test_glob_relative_path(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("")
    tool = make_glob_tool(str(tmp_path))
    result = await tool.execute(pattern="*.txt", path=".")
    assert result.ok


# --- read errors ---

@pytest.mark.asyncio
async def test_read_relative_missing(tmp_path: Path) -> None:
    tool = make_read_tool(str(tmp_path))
    result = await tool.execute(file_path="nonexistent.txt")
    assert not result.ok


# --- write errors ---

@pytest.mark.asyncio
async def test_write_relative(tmp_path: Path) -> None:
    tool = make_write_tool(str(tmp_path))
    result = await tool.execute(file_path="new/deep/file.txt", content="ok")
    assert result.ok


# --- plan empty ---

@pytest.mark.asyncio
async def test_plan_empty_content(tmp_path: Path) -> None:
    from openclose.tool.registry import ToolRegistry
    tool = make_plan_tool(str(tmp_path), ToolRegistry())
    result = await tool.execute(content="", phase="final")
    assert not result.ok


# --- binary edge ---

def test_binary_permission_error(tmp_path: Path) -> None:
    # Non-existent file
    assert not is_binary(tmp_path / "nope.xyz")


# --- permission add_rule ---

def test_permission_add_rule() -> None:
    from openclose.permission.rules import PermissionRule, PermissionAction
    from openclose.permission.schema import PermissionRequest

    engine = PermissionEngine()
    engine.add_rule(PermissionRule(tool="bash", action=PermissionAction.ALLOW))
    resp = engine.check(PermissionRequest(tool_name="bash"))
    assert resp.allowed


# --- diff tracker snapshot existing ---

def test_diff_snapshot_twice(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("orig")
    tracker = DiffTracker()
    tracker.snapshot(str(f))
    tracker.snapshot(str(f))  # Should not overwrite
    f.write_text("modified")
    tracker.record_change(str(f))
    changes = tracker.get_changes()
    assert changes[0].original == "orig"


# --- ignore edge ---

def test_ignore_outside_root(tmp_path: Path) -> None:
    mgr = IgnoreManager(tmp_path)
    # Path outside root
    assert not mgr.is_ignored(Path("/completely/outside"))
