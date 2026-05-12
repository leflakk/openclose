"""Pure-helper tests for server/routes.py — no FastAPI app needed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openclose.server.routes import (
    _apply_edit,
    _build_files_processed,
    _build_op_diff,
    _op_post_from_pre,
    _op_pre_from_post,
    _reconstruct_states,
    _resolve_tool_file_path,
    _segment_parts,
    _structured_diff,
    _undo_edit,
)


# ── _resolve_tool_file_path ─────────────────────────────────────────────────

def test_resolve_tool_file_path_untracked_tool() -> None:
    assert _resolve_tool_file_path("bash", {"file_path": "x"}, "/tmp") is None


def test_resolve_tool_file_path_no_path_arg() -> None:
    assert _resolve_tool_file_path("read", {}, "/tmp") is None


def test_resolve_tool_file_path_path_alias(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x")
    out = _resolve_tool_file_path("read", {"path": str(target)}, str(tmp_path))
    assert out == str(target.resolve())


def test_resolve_tool_file_path_relative(tmp_path: Path) -> None:
    target = tmp_path / "rel.txt"
    target.write_text("x")
    out = _resolve_tool_file_path(
        "edit", {"file_path": "rel.txt"}, str(tmp_path)
    )
    assert out == str(target.resolve())


def test_resolve_tool_file_path_nonstring_falls_through() -> None:
    # Neither file_path nor path is a usable string → None.
    assert _resolve_tool_file_path(
        "edit", {"file_path": 42, "path": None}, "/tmp"
    ) is None


# ── _build_files_processed ──────────────────────────────────────────────────

def _tool_call_part(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "part_type": "tool_call",
        "tool_name": tool,
        "content": json.dumps(args),
    }


def test_build_files_processed_orders_modified_files(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x")
    b.write_text("y")
    messages = [
        {"parts": [_tool_call_part("edit", {"file_path": str(a)})]},
        {"parts": [_tool_call_part("edit", {"file_path": str(b)})]},
    ]
    files = _build_files_processed(messages, str(tmp_path))
    paths = [f["path"] for f in files]
    assert paths == [str(a.resolve()), str(b.resolve())]
    assert all(f["action"] == "modified" for f in files)


def test_build_files_processed_skips_reads(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    a.write_text("x")
    messages = [
        {"parts": [_tool_call_part("read", {"file_path": str(a)})]},
    ]
    assert _build_files_processed(messages, str(tmp_path)) == []


def test_build_files_processed_created_outranks_modified(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    a.write_text("x")
    messages = [
        {"parts": [_tool_call_part("edit", {"file_path": str(a)})]},
        {"parts": [_tool_call_part("write", {"file_path": str(a)})]},
    ]
    files = _build_files_processed(messages, str(tmp_path))
    assert len(files) == 1
    assert files[0]["action"] == "created"


def test_build_files_processed_skips_invalid_json(tmp_path: Path) -> None:
    messages = [
        {"parts": [{
            "part_type": "tool_call",
            "tool_name": "edit",
            "content": "not-json",
        }]},
    ]
    assert _build_files_processed(messages, str(tmp_path)) == []


def test_build_files_processed_skips_non_dict_args(tmp_path: Path) -> None:
    messages = [
        {"parts": [{
            "part_type": "tool_call",
            "tool_name": "edit",
            "content": json.dumps([1, 2, 3]),
        }]},
    ]
    assert _build_files_processed(messages, str(tmp_path)) == []


def test_build_files_processed_skips_non_tool_call_parts(tmp_path: Path) -> None:
    messages = [{"parts": [{"part_type": "text", "content": "hello"}]}]
    assert _build_files_processed(messages, str(tmp_path)) == []


# ── _apply_edit / _undo_edit ────────────────────────────────────────────────

def test_apply_edit_first_only() -> None:
    out, ok = _apply_edit("aa aa", "aa", "bb", replace_all=False)
    assert ok and out == "bb aa"


def test_apply_edit_replace_all() -> None:
    out, ok = _apply_edit("aa aa", "aa", "bb", replace_all=True)
    assert ok and out == "bb bb"


def test_apply_edit_missing_returns_unchanged() -> None:
    out, ok = _apply_edit("hello", "zzz", "yyy", replace_all=False)
    assert not ok and out == "hello"


def test_apply_edit_empty_old_returns_unchanged() -> None:
    out, ok = _apply_edit("hello", "", "yyy", replace_all=False)
    assert not ok and out == "hello"


def test_undo_edit_first_only() -> None:
    out, ok = _undo_edit("bb aa", "aa", "bb", replace_all=False)
    assert ok and out == "aa aa"


def test_undo_edit_replace_all() -> None:
    out, ok = _undo_edit("bb bb", "aa", "bb", replace_all=True)
    assert ok and out == "aa aa"


def test_undo_edit_missing_returns_unchanged() -> None:
    out, ok = _undo_edit("hello", "aa", "zzz", replace_all=False)
    assert not ok and out == "hello"


def test_undo_edit_empty_new_returns_unchanged() -> None:
    out, ok = _undo_edit("hello", "aa", "", replace_all=False)
    assert not ok and out == "hello"


# ── _op_pre_from_post ───────────────────────────────────────────────────────

def test_op_pre_from_post_read_is_identity() -> None:
    assert _op_pre_from_post({"tool_name": "read", "args": {}}, "abc") == "abc"


def test_op_pre_from_post_write_is_unrecoverable() -> None:
    assert _op_pre_from_post({"tool_name": "write", "args": {}}, "abc") is None


def test_op_pre_from_post_edit_recovers() -> None:
    op = {
        "tool_name": "edit",
        "args": {"old_string": "foo", "new_string": "bar"},
    }
    assert _op_pre_from_post(op, "bar baz") == "foo baz"


def test_op_pre_from_post_edit_returns_none_when_new_missing() -> None:
    op = {
        "tool_name": "edit",
        "args": {"old_string": "foo", "new_string": "bar"},
    }
    assert _op_pre_from_post(op, "qux") is None


def test_op_pre_from_post_edit_replace_all() -> None:
    op = {
        "tool_name": "edit",
        "args": {
            "old_string": "a", "new_string": "b", "replace_all": True,
        },
    }
    assert _op_pre_from_post(op, "bb bb") == "aa aa"


def test_op_pre_from_post_multiedit_reverses_in_order() -> None:
    op = {
        "tool_name": "multiedit",
        "args": {"edits": [
            {"old_string": "a", "new_string": "b"},
            {"old_string": "b", "new_string": "c"},
        ]},
    }
    # Forward: a → b → c. Backward from "c": c→b→a.
    assert _op_pre_from_post(op, "c") == "a"


def test_op_pre_from_post_multiedit_unrecoverable_step() -> None:
    op = {
        "tool_name": "multiedit",
        "args": {"edits": [
            {"old_string": "a", "new_string": "b"},
            {"old_string": "x", "new_string": "y"},  # not in post
        ]},
    }
    assert _op_pre_from_post(op, "b") is None


def test_op_pre_from_post_multiedit_skips_non_dict_edit() -> None:
    op = {"tool_name": "multiedit", "args": {"edits": ["bogus"]}}
    assert _op_pre_from_post(op, "anything") is None


def test_op_pre_from_post_unknown_tool() -> None:
    assert _op_pre_from_post({"tool_name": "bash", "args": {}}, "x") is None


# ── _op_post_from_pre ───────────────────────────────────────────────────────

def test_op_post_from_pre_read_is_identity() -> None:
    assert _op_post_from_pre({"tool_name": "read", "args": {}}, "abc") == "abc"


def test_op_post_from_pre_write_replaces_state() -> None:
    op = {"tool_name": "write", "args": {"content": "new"}}
    assert _op_post_from_pre(op, "old") == "new"


def test_op_post_from_pre_edit() -> None:
    op = {
        "tool_name": "edit",
        "args": {"old_string": "foo", "new_string": "bar"},
    }
    assert _op_post_from_pre(op, "foo baz") == "bar baz"


def test_op_post_from_pre_multiedit_chains() -> None:
    op = {
        "tool_name": "multiedit",
        "args": {"edits": [
            {"old_string": "a", "new_string": "b"},
            {"old_string": "b", "new_string": "c"},
            "skip-non-dict",
        ]},
    }
    assert _op_post_from_pre(op, "a") == "c"


def test_op_post_from_pre_unknown_tool_is_identity() -> None:
    assert _op_post_from_pre({"tool_name": "bash", "args": {}}, "x") == "x"


# ── _reconstruct_states ─────────────────────────────────────────────────────

def test_reconstruct_states_empty() -> None:
    assert _reconstruct_states([], "abc") == []


def test_reconstruct_states_backward_only() -> None:
    ops = [
        {"tool_name": "edit", "args": {"old_string": "a", "new_string": "b"}},
        {"tool_name": "edit", "args": {"old_string": "b", "new_string": "c"}},
    ]
    states = _reconstruct_states(ops, "c")
    assert states == [("a", "b"), ("b", "c")]


def test_reconstruct_states_forward_after_write() -> None:
    # Write erases prior state — backward stops; forward fills the rest.
    ops = [
        {"tool_name": "edit", "args": {"old_string": "x", "new_string": "y"}},
        {"tool_name": "write", "args": {"content": "fresh"}},
        {"tool_name": "edit",
         "args": {"old_string": "fresh", "new_string": "stale"}},
    ]
    states = _reconstruct_states(ops, "stale")
    assert states[1] == (None, "fresh")
    assert states[2] == ("fresh", "stale")


def test_reconstruct_states_no_current_content() -> None:
    ops = [{"tool_name": "write", "args": {"content": "hello"}}]
    states = _reconstruct_states(ops, None)
    assert states == [(None, "hello")]


# ── _structured_diff ────────────────────────────────────────────────────────

def test_structured_diff_identical() -> None:
    assert _structured_diff("a\nb\n", "a\nb\n") == []


def test_structured_diff_replace() -> None:
    hunks = _structured_diff("foo\nbar\n", "foo\nbaz\n")
    assert len(hunks) == 1
    types = [line["type"] for line in hunks[0]["lines"]]
    assert "-" in types and "+" in types


def test_structured_diff_insert_only() -> None:
    hunks = _structured_diff("a\n", "a\nb\n")
    assert len(hunks) == 1
    plus_lines = [line for line in hunks[0]["lines"] if line["type"] == "+"]
    assert plus_lines and plus_lines[0]["text"] == "b"


def test_structured_diff_delete_only() -> None:
    hunks = _structured_diff("a\nb\n", "a\n")
    assert len(hunks) == 1
    minus_lines = [line for line in hunks[0]["lines"] if line["type"] == "-"]
    assert minus_lines and minus_lines[0]["text"] == "b"


# ── _build_op_diff ──────────────────────────────────────────────────────────

def test_build_op_diff_read_returns_none() -> None:
    assert _build_op_diff({"tool_name": "read", "args": {}}, "x", "x") is None


def test_build_op_diff_edit_exact() -> None:
    op = {
        "tool_name": "edit",
        "args": {"old_string": "a", "new_string": "b"},
    }
    out = _build_op_diff(op, "a\n", "b\n")
    assert out is not None
    assert out["kind"] == "edit"
    assert out["reconstruction"] == "exact"
    assert "hunks" in out


def test_build_op_diff_edit_fallback() -> None:
    op = {
        "tool_name": "edit",
        "args": {"old_string": "a", "new_string": "b"},
    }
    out = _build_op_diff(op, None, None)
    assert out is not None
    assert out["reconstruction"] == "fallback"


def test_build_op_diff_write_fallback() -> None:
    op = {"tool_name": "write", "args": {"content": "hello\n"}}
    out = _build_op_diff(op, None, None)
    assert out is not None
    assert out["kind"] == "write"
    assert out["reconstruction"] == "fallback"


def test_build_op_diff_multiedit_exact() -> None:
    op = {
        "tool_name": "multiedit",
        "args": {"edits": [
            {"old_string": "a", "new_string": "b"},
            {"old_string": "b", "new_string": "c"},
        ]},
    }
    out = _build_op_diff(op, "a", "c")
    assert out is not None
    assert out["kind"] == "multiedit"
    assert out["reconstruction"] == "exact"
    assert len(out["sub_edits"]) == 2


def test_build_op_diff_multiedit_fallback() -> None:
    op = {
        "tool_name": "multiedit",
        "args": {"edits": [
            {"old_string": "a", "new_string": "b"},
            "non-dict-edit-skipped",
        ]},
    }
    out = _build_op_diff(op, None, None)
    assert out is not None
    assert out["reconstruction"] == "fallback"
    assert len(out["sub_edits"]) == 1


def test_build_op_diff_unknown_tool_returns_none() -> None:
    assert _build_op_diff({"tool_name": "bash", "args": {}}, None, None) is None


# ── _segment_parts ──────────────────────────────────────────────────────────

def test_segment_parts_single_text() -> None:
    parts = [{"part_type": "text", "content": "hi"}]
    assert _segment_parts(parts) == [parts]


def test_segment_parts_splits_on_text_after_result() -> None:
    parts: list[dict[str, Any]] = [
        {"part_type": "tool_call", "id": "1"},
        {"part_type": "tool_result", "id": "1"},
        {"part_type": "text", "content": "after"},
    ]
    segments = _segment_parts(parts)
    assert len(segments) == 2
    assert segments[0] == parts[:2]
    assert segments[1] == parts[2:]


def test_segment_parts_keeps_consecutive_results_together() -> None:
    parts: list[dict[str, Any]] = [
        {"part_type": "tool_call", "id": "1"},
        {"part_type": "tool_call", "id": "2"},
        {"part_type": "tool_result", "id": "1"},
        {"part_type": "tool_result", "id": "2"},
    ]
    # No text/tool_call follows the results → all in one segment.
    assert _segment_parts(parts) == [parts]


def test_segment_parts_empty() -> None:
    assert _segment_parts([]) == []
