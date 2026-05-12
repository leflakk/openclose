"""File glob pattern matching tool."""

from __future__ import annotations

import glob as globmod
from pathlib import Path

from openclose.file.ignore import IgnoreManager
from openclose.tool.tool import Tool, ToolResult, ToolParameter
from openclose.tool.truncation import truncate_output


def make_glob_tool(project_dir: str = ".") -> Tool:
    """Create the glob tool."""

    async def execute(
        pattern: str = "",
        path: str = "",
        **kwargs: object,
    ) -> ToolResult:
        base = Path(path) if path else Path(project_dir)
        if not base.is_absolute():
            base = Path(project_dir) / base

        try:
            ignore = IgnoreManager(base)
            raw = globmod.glob(pattern, root_dir=str(base), recursive=True)
            matches = sorted(
                m for m in raw if not ignore.is_ignored(base / m)
            )
        except Exception as e:
            return ToolResult(error=f"Glob error: {e}")

        if not matches:
            return ToolResult(output="No files matched the pattern.")

        output = "\n".join(matches)
        return ToolResult(
            output=truncate_output(output),
            metadata={"count": len(matches)},
        )

    return Tool(
        name="glob",
        description=(
            "USE IT TO LIST FILES whose paths match a known shell-style glob "
            "pattern (e.g., all `.py` files, every config under a folder). "
            "Returns file paths matching the pattern, sorted "
            "alphabetically, with ignored files (per `.gitignore` and equivalents) "
            "excluded."
        ),
        parameters=[
            ToolParameter(
                name="pattern",
                description=(
                    "Shell-style glob pattern matched against file paths, e.g., "
                    "`**/*.py` for every Python file at any depth, "
                    "`src/**/test_*.ts` for test files under `src/`."
                ),
            ),
            ToolParameter(
                name="path",
                description=(
                    "Base directory to search from. Absolute or relative to the "
                    "project working directory. Use to scope a search to a subtree "
                    "(e.g., `src/`)."
                ),
                required=False,
            ),
        ],
        execute_fn=execute,
    )
