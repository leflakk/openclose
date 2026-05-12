"""Formatter registry — auto-format files on save."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from openclose.util.process import run
from openclose.log import get_logger

log = get_logger(__name__)


@dataclass
class Formatter:
    """A code formatter."""

    name: str
    command: list[str]
    extensions: list[str]

    def supports(self, path: Path) -> bool:
        """Check if this formatter handles the given file."""
        return path.suffix.lower() in self.extensions

    def is_available(self) -> bool:
        """Check if the formatter binary is installed."""
        return shutil.which(self.command[0]) is not None


# Built-in formatters
_BUILTIN_FORMATTERS = [
    Formatter("ruff", ["ruff", "format"], [".py"]),
    Formatter("black", ["black", "-q"], [".py"]),
    Formatter("gofmt", ["gofmt", "-w"], [".go"]),
    Formatter("rustfmt", ["rustfmt"], [".rs"]),
    Formatter("prettier", ["prettier", "--write"], [
        ".js", ".jsx", ".ts", ".tsx", ".json", ".css", ".html", ".md", ".yaml", ".yml",
    ]),
    Formatter("shfmt", ["shfmt", "-w"], [".sh", ".bash"]),
    Formatter("clang-format", ["clang-format", "-i"], [".c", ".cpp", ".h", ".hpp"]),
]


class FormatterRegistry:
    """Registry and executor for code formatters."""

    def __init__(self) -> None:
        self._formatters: list[Formatter] = list(_BUILTIN_FORMATTERS)

    def add(self, formatter: Formatter) -> None:
        """Add a custom formatter."""
        self._formatters.insert(0, formatter)  # Custom formatters take priority

    def get_formatter(self, path: Path) -> Formatter | None:
        """Find the first available formatter for a file."""
        for fmt in self._formatters:
            if fmt.supports(path) and fmt.is_available():
                return fmt
        return None

    async def format_file(self, path: Path) -> bool:
        """Format a file using the appropriate formatter. Returns True if formatted."""
        fmt = self.get_formatter(path)
        if fmt is None:
            return False

        cmd = fmt.command + [str(path)]
        result = await run(*cmd, timeout=30.0)
        if result.ok:
            log.debug("Formatted %s with %s", path, fmt.name)
            return True
        else:
            log.warning("Formatter %s failed on %s: %s", fmt.name, path, result.stderr)
            return False

    def list_formatters(self) -> list[Formatter]:
        """List all registered formatters."""
        return list(self._formatters)


_registry: FormatterRegistry | None = None


def get_formatter_registry() -> FormatterRegistry:
    """Get the global formatter registry."""
    global _registry
    if _registry is None:
        _registry = FormatterRegistry()
    return _registry
