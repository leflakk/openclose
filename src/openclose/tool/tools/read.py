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
        description=(
            "USE IT TO READ THE CONTENT OF A FILE when its path is already "
            "known. Read enough lines in a single call to avoid "
            "wasteful iteration; binary files are rejected."
        ),
        parameters=[
            ToolParameter(
                name="file_path",
                description=(
                    "Path to the file to read. Can be absolute or relative to the "
                    "project working directory. The file must exist and be a text "
                    "(non-binary) file."
                ),
            ),
            ToolParameter(
                name="offset",
                type="integer",
                description=(
                    "0-based line index to start reading from. Use together with "
                    "`limit` to page through files too large to read in one call; "
                    "otherwise leave at default."
                ),
                required=False,
                default=0,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description=(
                    "Maximum number of lines to return starting from `offset`. "
                    "Increase upfront when you expect a larger file and want it in "
                    "one call; only lower it when explicitly paging."
                ),
                required=False,
                default=2000,
            ),
        ],
        execute_fn=execute,
    )
