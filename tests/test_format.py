"""Tests for the formatter system."""

from __future__ import annotations

from pathlib import Path

from openclose.format.formatter import FormatterRegistry, Formatter


def test_formatter_supports() -> None:
    fmt = Formatter("ruff", ["ruff", "format"], [".py"])
    assert fmt.supports(Path("code.py"))
    assert not fmt.supports(Path("code.js"))


def test_registry_get_formatter() -> None:
    registry = FormatterRegistry()
    fmt = registry.get_formatter(Path("test.go"))
    # gofmt may or may not be installed, but the lookup should work
    if fmt is not None:
        assert fmt.name == "gofmt"


def test_registry_list() -> None:
    registry = FormatterRegistry()
    formatters = registry.list_formatters()
    assert len(formatters) > 0
    names = [f.name for f in formatters]
    assert "ruff" in names or "black" in names
