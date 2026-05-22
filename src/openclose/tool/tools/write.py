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
        description='write(file_path="src/main.py", content="print(\'hello\')\\n")',
        parameters=[
            ToolParameter(
                name="file_path",
                description='"src/main.py"',
            ),
            ToolParameter(
                name="content",
                description='"print(\'hello\')\\n"',
            ),
        ],
        execute_fn=execute,
    )
