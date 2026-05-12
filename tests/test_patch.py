"""Tests for the patch system."""

from __future__ import annotations

from openclose.patch.patch import generate_diff, apply_unified_diff


def test_generate_diff() -> None:
    original = "line1\nline2\nline3\n"
    modified = "line1\nline2_changed\nline3\n"
    diff = generate_diff(original, modified)
    assert "-line2" in diff
    assert "+line2_changed" in diff


def test_apply_diff() -> None:
    original = "line1\nline2\nline3\n"
    modified = "line1\nline2_changed\nline3\n"
    diff = generate_diff(original, modified)
    result = apply_unified_diff(original, diff)
    assert result == modified


def test_apply_add_lines() -> None:
    original = "a\nb\nc\n"
    modified = "a\nb\nnew_line\nc\n"
    diff = generate_diff(original, modified)
    result = apply_unified_diff(original, diff)
    assert result == modified


def test_apply_remove_lines() -> None:
    original = "a\nb\nc\nd\n"
    modified = "a\nc\nd\n"
    diff = generate_diff(original, modified)
    result = apply_unified_diff(original, diff)
    assert result == modified


def test_empty_diff() -> None:
    original = "same\n"
    diff = generate_diff(original, original)
    assert diff == ""
    result = apply_unified_diff(original, diff)
    assert result == original
