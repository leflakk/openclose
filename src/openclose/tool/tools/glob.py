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
        description='glob(pattern="**/*.py", path="src/")',
        parameters=[
            ToolParameter(
                name="pattern",
                description='"**/*.py"',
            ),
            ToolParameter(
                name="path",
                description='"src/"',
                required=False,
            ),
        ],
        execute_fn=execute,
    )
