"""Extract paths from tool arguments for permission matching."""

from __future__ import annotations

from pathlib import Path

# Maps tool names to the argument key that contains the primary path.
_TOOL_PATH_KEYS: dict[str, str] = {
    "read": "file_path",
    "write": "file_path",
    "edit": "file_path",
    "glob": "path",
    "grep": "path",
}

# Tools that write to the filesystem — must stay inside project_dir.
_WRITE_TOOLS: frozenset[str] = frozenset({"write", "edit"})


def extract_path(
    tool_name: str,
    arguments: dict[str, object],
    project_dir: str = ".",
) -> str:
    """Extract and resolve the file path from tool arguments.

    Returns ``"*"`` for tools that have no meaningful path argument
    (e.g. ``bash``, ``webfetch``, ``question``).
    """
    key = _TOOL_PATH_KEYS.get(tool_name)
    if key is None:
        return "*"

    raw = arguments.get(key)
    if not raw or not isinstance(raw, str):
        return "*"

    p = Path(raw)
    if not p.is_absolute():
        p = Path(project_dir) / p
    return str(p.resolve())


def check_path_sandbox(
    tool_name: str,
    arguments: dict[str, object],
    project_dir: str = ".",
) -> str | None:
    """Check if a write tool targets a path outside the project directory.

    Returns an error message if the path is outside the project dir,
    or ``None`` if the path is allowed (or the tool doesn't write files).
    This runs *before* the permission dialog so the user isn't asked
    to approve an operation that will be rejected anyway.
    """
    if tool_name not in _WRITE_TOOLS:
        return None

    key = _TOOL_PATH_KEYS.get(tool_name)
    if key is None:
        return None

    raw = arguments.get(key)
    if not raw or not isinstance(raw, str):
        return None

    p = Path(raw)
    if not p.is_absolute():
        p = Path(project_dir) / p

    resolved = p.resolve()
    project = Path(project_dir).resolve()
    if not str(resolved).startswith(str(project)):
        return f"Cannot {tool_name} outside project directory: {resolved}"

    return None
