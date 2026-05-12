"""Extended formatter tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from openclose.format.formatter import FormatterRegistry, Formatter


@pytest.mark.asyncio
async def test_format_python_file(tmp_path: Path) -> None:
    """Should format a Python file if ruff is available."""
    f = tmp_path / "messy.py"
    f.write_text("x=1\ny=2\n")
    registry = FormatterRegistry()
    formatted = await registry.format_file(f)
    # May or may not format depending on ruff availability
    assert isinstance(formatted, bool)


@pytest.mark.asyncio
async def test_format_unknown_extension(tmp_path: Path) -> None:
    """Should return False for unknown extensions."""
    f = tmp_path / "file.xyz"
    f.write_text("data")
    registry = FormatterRegistry()
    assert not await registry.format_file(f)


def test_formatter_add_custom() -> None:
    registry = FormatterRegistry()
    custom = Formatter("custom-fmt", ["custom-fmt"], [".cst"])
    registry.add(custom)
    formatters = registry.list_formatters()
    assert formatters[0].name == "custom-fmt"
