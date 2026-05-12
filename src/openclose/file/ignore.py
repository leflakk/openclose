"""Gitignore/ignore pattern handling."""

from __future__ import annotations

from pathlib import Path

import pathspec


class IgnoreManager:
    """Manages ignore patterns from .gitignore, .ignore, etc."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._spec: pathspec.PathSpec | None = None
        self._load()

    def _load(self) -> None:
        """Load all ignore patterns."""
        patterns: list[str] = []

        # Default ignores
        patterns.extend([
            ".git/", "__pycache__/", "*.pyc", ".venv/", "node_modules/",
            ".mypy_cache/", ".pytest_cache/", ".ruff_cache/",
            ".openclose/",
        ])

        # .gitignore
        gitignore = self._root / ".gitignore"
        if gitignore.is_file():
            patterns.extend(
                gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
            )

        # .ignore (tool-specific)
        ignore_file = self._root / ".ignore"
        if ignore_file.is_file():
            patterns.extend(
                ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
            )

        self._spec = pathspec.PathSpec.from_lines("gitignore", patterns)

    def is_ignored(self, path: Path) -> bool:
        """Check if a path should be ignored."""
        if self._spec is None:
            return False
        try:
            rel = path.relative_to(self._root)
            return self._spec.match_file(str(rel))
        except ValueError:
            return False

    def reload(self) -> None:
        """Reload patterns from disk."""
        self._load()
