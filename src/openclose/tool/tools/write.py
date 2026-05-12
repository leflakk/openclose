"""File writing tool."""

from __future__ import annotations

from pathlib import Path

from openclose.tool.tool import Tool, ToolResult, ToolParameter


def make_write_tool(project_dir: str = ".") -> Tool:
    """Create the file write tool."""

    async def execute(
        file_path: str = "",
        content: str = "",
        **kwargs: object,
    ) -> ToolResult:
        p = Path(file_path)
        if not p.is_absolute():
            p = Path(project_dir) / p

        p = p.resolve()
        project = Path(project_dir).resolve()
        if not str(p).startswith(str(project)):
            return ToolResult(error=f"Cannot write outside project directory: {p}")

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except Exception as e:
            return ToolResult(error=f"Failed to write {p}: {e}")

        lines = content.count("\n") + (1 if content else 0)
        return ToolResult(
            output=f"Wrote {lines} lines to {p}",
            metadata={"file_path": str(p), "lines": lines},
        )

    return Tool(
        name="write",
        description=(
            "USE IT TO CREATE A NEW FILE or fully overwrite an existing one. "
            "Writes `content` to `file_path`, creating any missing "
            "parent directories. If the file already exists, it is replaced "
            "wholesale."
        ),
        parameters=[
            ToolParameter(
                name="file_path",
                description=(
                    "Path to the file to write. Absolute or relative to the "
                    "project working directory. Must resolve inside the project; "
                    "writes outside are rejected. Missing parent directories are "
                    "created automatically."
                ),
            ),
            ToolParameter(
                name="content",
                description=(
                    "Full text of the file. Written exactly as given (no trailing "
                    "newline added). Pass an empty string to clear an existing "
                    "file."
                ),
            ),
        ],
        execute_fn=execute,
    )
