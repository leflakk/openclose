"""Tests for file utilities — binary detection, ignore, diff tracking."""

from __future__ import annotations

from pathlib import Path

from openclose.file.binary import is_binary
from openclose.file.ignore import IgnoreManager
from openclose.file.diff import DiffTracker, FileChange


def test_binary_by_extension(tmp_path: Path) -> None:
    f = tmp_path / "image.png"
    f.write_bytes(b"fake png data")
    assert is_binary(f)


def test_not_binary_text(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("print('hello')")
    assert not is_binary(f)


def test_binary_by_null_bytes(tmp_path: Path) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello\x00world")
    assert is_binary(f)


def test_ignore_manager(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.log\nbuild/\n")

    mgr = IgnoreManager(tmp_path)
    assert mgr.is_ignored(tmp_path / "app.log")
    assert mgr.is_ignored(tmp_path / "build" / "output.js")
    assert not mgr.is_ignored(tmp_path / "src" / "main.py")


def test_ignore_defaults(tmp_path: Path) -> None:
    mgr = IgnoreManager(tmp_path)
    assert mgr.is_ignored(tmp_path / "__pycache__" / "module.pyc")
    assert mgr.is_ignored(tmp_path / "node_modules" / "pkg" / "index.js")


def test_diff_tracker(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("line1\nline2\n")

    tracker = DiffTracker()
    tracker.snapshot(str(f))

    # Modify
    f.write_text("line1\nline2\nline3\n")
    tracker.record_change(str(f))

    changes = tracker.get_changes()
    assert len(changes) == 1
    assert changes[0].path == str(f)
    assert not changes[0].is_new

    diff = tracker.get_diff(str(f))
    assert diff is not None
    assert "+line3" in diff


def test_diff_tracker_new_file(tmp_path: Path) -> None:
    f = tmp_path / "new.txt"

    tracker = DiffTracker()
    f.write_text("brand new\n")
    tracker.record_change(str(f))

    changes = tracker.get_changes()
    assert len(changes) == 1
    assert changes[0].is_new


def test_diff_tracker_deleted_file(tmp_path: Path) -> None:
    f = tmp_path / "delete.txt"
    f.write_text("goodbye\n")

    tracker = DiffTracker()
    tracker.snapshot(str(f))
    f.unlink()
    tracker.record_change(str(f))

    changes = tracker.get_changes()
    assert len(changes) == 1
    assert changes[0].is_deleted


def test_file_change_unified_diff() -> None:
    change = FileChange(
        path="test.py",
        original="a = 1\nb = 2\n",
        modified="a = 1\nb = 3\n",
    )
    diff = change.unified_diff()
    assert "-b = 2" in diff
    assert "+b = 3" in diff
