"""Code editing tool — find and replace within a file."""

from __future__ import annotations

from pathlib import Path

from openclose.tool.tool import Tool, ToolResult, ToolParameter


def make_edit_tool(project_dir: str = ".") -> Tool:
    """Create the code edit tool."""

    async def execute(
        file_path: str = "",
        old_string: str = "",
        new_string: str = "",
        replace_all: bool = False,
        **kwargs: object,
    ) -> ToolResult:
        p = Path(file_path)
        if not p.is_absolute():
            p = Path(project_dir) / p

        p = p.resolve()
        project = Path(project_dir).resolve()
        if not str(p).startswith(str(project)):
            return ToolResult(error=f"Cannot edit outside project directory: {p}")

        if not p.is_file():
            return ToolResult(error=f"File not found: {p}")

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(error=f"Failed to read {p}: {e}")

        count = content.count(old_string)
        if count == 0:
            return ToolResult(
                error=f"old_string not found in {p}. Make sure it matches exactly."
            )
        if count > 1 and not replace_all:
            return ToolResult(
                error=(
                    f"old_string found {count} times in {p}. "
                    f"Provide more context to make it unique, or use replace_all=true."
                )
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
            replaced = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replaced = 1
        try:
            p.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return ToolResult(error=f"Failed to write {p}: {e}")

        return ToolResult(
            output=f"Edited {p}: replaced {replaced} occurrence{'s' if replaced != 1 else ''}",
            metadata={"file_path": str(p), "replaced": replaced},
        )

    return Tool(
        name="edit",
        description='edit(file_path="src/main.py", old_string="def foo():", new_string="def bar():", replace_all=False)',
        parameters=[
            ToolParameter(
                name="file_path",
                description='"src/main.py"',
            ),
            ToolParameter(
                name="old_string",
                description='"def foo():"',
            ),
            ToolParameter(
                name="new_string",
                description='"def bar():"',
            ),
            ToolParameter(
                name="replace_all",
                type="boolean",
                description="False",
                required=False,
                default=False,
            ),
        ],
        execute_fn=execute,
    )
