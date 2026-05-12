"""Diff generation and file change tracking."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FileChange:
    """A tracked file change."""

    path: str
    original: str = ""
    modified: str = ""
    is_new: bool = False
    is_deleted: bool = False

    def unified_diff(self) -> str:
        """Generate unified diff."""
        return "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                self.modified.splitlines(keepends=True),
                fromfile=f"a/{self.path}",
                tofile=f"b/{self.path}",
            )
        )


class DiffTracker:
    """Tracks file changes during a session."""

    def __init__(self) -> None:
        self._originals: dict[str, str] = {}
        self._changes: dict[str, FileChange] = {}

    def snapshot(self, path: str) -> None:
        """Capture the original state of a file before modification."""
        if path in self._originals:
            return
        p = Path(path)
        if p.is_file():
            try:
                self._originals[path] = p.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                self._originals[path] = ""
        else:
            self._originals[path] = ""

    def record_change(self, path: str) -> None:
        """Record that a file was modified."""
        p = Path(path)
        original = self._originals.get(path, "")
        is_new = path not in self._originals

        if p.is_file():
            try:
                modified = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                modified = ""
            self._changes[path] = FileChange(
                path=path,
                original=original,
                modified=modified,
                is_new=is_new,
            )
        else:
            # File was deleted
            self._changes[path] = FileChange(
                path=path,
                original=original,
                modified="",
                is_deleted=True,
            )

    def get_changes(self) -> list[FileChange]:
        """Get all recorded changes."""
        return list(self._changes.values())

    def get_diff(self, path: str) -> str | None:
        """Get the unified diff for a specific file."""
        change = self._changes.get(path)
        if change:
            return change.unified_diff()
        return None

    def get_all_diffs(self) -> str:
        """Get unified diffs for all changed files."""
        diffs: list[str] = []
        for change in self._changes.values():
            d = change.unified_diff()
            if d:
                diffs.append(d)
        return "\n".join(diffs)

    def clear(self) -> None:
        """Reset tracking."""
        self._originals.clear()
        self._changes.clear()
