"""File reading tool."""

from __future__ import annotations

from pathlib import Path

from openclose.file.binary import is_binary
from openclose.tool.tool import Tool, ToolResult, ToolParameter
from openclose.tool.truncation import truncate_output


def make_read_tool(project_dir: str = ".") -> Tool:
    """Create the file read tool."""

    async def execute(
        file_path: str = "",
        offset: int = 0,
        limit: int = 2000,
        **kwargs: object,
    ) -> ToolResult:
        p = Path(file_path)
        if not p.is_absolute():
            p = Path(project_dir) / p

        if not p.is_file():
            return ToolResult(error=f"File not found: {p}")

        if is_binary(p):
            return ToolResult(error=f"File appears to be binary: {p}")

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(error=f"Failed to read {p}: {e}")

        lines = text.splitlines()
        total = len(lines)

        # Apply offset and limit
        selected = lines[offset : offset + limit]
        numbered = []
        for i, line in enumerate(selected, start=offset + 1):
            numbered.append(f"{i:>6}\t{line}")

        output = "\n".join(numbered)
        if offset + limit < total:
            output += f"\n... [{total - offset - limit} more lines]"

        return ToolResult(
            output=truncate_output(output),
            metadata={"file_path": str(p), "total_lines": total},
        )

    return Tool(
        name="read",
        description='read(file_path="src/main.py", offset=0, limit=2000)',
        parameters=[
            ToolParameter(
                name="file_path",
                description='"src/main.py"',
            ),
            ToolParameter(
                name="offset",
                type="integer",
                description="0",
                required=False,
                default=0,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="2000",
                required=False,
                default=2000,
            ),
        ],
        execute_fn=execute,
    )
