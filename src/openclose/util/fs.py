"""File system utility functions."""

from __future__ import annotations

from pathlib import Path

import pathspec


def load_gitignore(directory: Path) -> pathspec.PathSpec | None:
    """Load .gitignore patterns from a directory."""
    gitignore = directory / ".gitignore"
    if not gitignore.is_file():
        return None
    patterns = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    return pathspec.PathSpec.from_lines("gitignore", patterns)


def is_binary_extension(path: Path) -> bool:
    """Check if a file has a known binary extension."""
    binary_extensions = {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
        ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".flac", ".wav", ".ogg",
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".lib",
        ".pyc", ".pyo", ".class", ".jar", ".war",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".sqlite", ".db", ".sqlite3",
        ".bin", ".dat", ".iso", ".img",
    }
    return path.suffix.lower() in binary_extensions


def find_project_root(start: Path) -> Path:
    """Walk up from start to find a project root (has .git, pyproject.toml, etc.)."""
    markers = {".git", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "package.json"}
    current = start.resolve()
    while current != current.parent:
        if any((current / m).exists() for m in markers):
            return current
        current = current.parent
    return start.resolve()
