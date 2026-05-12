"""Binary file detection."""

from __future__ import annotations

from pathlib import Path

_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg", ".tiff",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".flac", ".wav", ".ogg", ".aac",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".zst",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".lib",
    ".pyc", ".pyo", ".class", ".jar", ".war",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".sqlite", ".db", ".sqlite3",
    ".bin", ".dat", ".iso", ".img",
    ".wasm",
})


def is_binary(path: Path) -> bool:
    """Check if a file is likely binary based on extension or content."""
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return True

    # Check first 8KB for null bytes
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
            return b"\x00" in chunk
    except (OSError, PermissionError):
        return False
