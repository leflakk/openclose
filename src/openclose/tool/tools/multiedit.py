"""Multi-edit tool — apply multiple sequential edits to a file in one call."""

from __future__ import annotations

from pathlib import Path

from openclose.tool.tool import Tool, ToolResult, ToolParameter


def make_multiedit_tool(project_dir: str = ".") -> Tool:
    """Create the multi-edit tool."""

    async def execute(
        file_path: str = "",
        edits: object = None,
        **kwargs: object,
    ) -> ToolResult:
        import json as jsonmod

        p = Path(file_path)
        if not p.is_absolute():
            p = Path(project_dir) / p

        p = p.resolve()
        project = Path(project_dir).resolve()
        if not str(p).startswith(str(project)):
            return ToolResult(error=f"Cannot edit outside project directory: {p}")

        if not p.is_file():
            return ToolResult(error=f"File not found: {p}")

        # Accept edits as a list (from function calling) or a JSON string
        edit_list: object
        if isinstance(edits, list):
            edit_list = edits
        elif isinstance(edits, str):
            try:
                edit_list = jsonmod.loads(edits)
            except (jsonmod.JSONDecodeError, TypeError) as e:
                return ToolResult(error=f"Invalid edits JSON: {e}")
        else:
            return ToolResult(error="edits must be an array of edit objects")

        if not isinstance(edit_list, list) or not edit_list:
            return ToolResult(error="edits must be a non-empty array")

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(error=f"Failed to read {p}: {e}")

        # Apply edits sequentially
        applied = 0
        for i, edit in enumerate(edit_list):
            if not isinstance(edit, dict):
                return ToolResult(error=f"Edit #{i + 1}: must be an object")

            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            if not old:
                return ToolResult(error=f"Edit #{i + 1}: old_string is required")

            count = content.count(old)
            if count == 0:
                return ToolResult(
                    error=f"Edit #{i + 1}: old_string not found. "
                    f"Applied {applied}/{len(edit_list)} edits before failure."
                )
            if count > 1:
                return ToolResult(
                    error=f"Edit #{i + 1}: old_string found {count} times. "
                    f"Provide more context to make it unique."
                )

            content = content.replace(old, new, 1)
            applied += 1

        try:
            p.write_text(content, encoding="utf-8")
        except Exception as e:
            return ToolResult(error=f"Failed to write {p}: {e}")

        return ToolResult(
            output=f"Applied {applied} edit{'s' if applied != 1 else ''} to {p}",
            metadata={"file_path": str(p), "edits_applied": applied},
        )

    return Tool(
        name="multiedit",
        description=(
            "USE IT TO APPLY SEVERAL CHANGES via literal find/replace edits to the same "
            "file in one batched call. "
        ),
        parameters=[
            ToolParameter(
                name="file_path",
                description=(
                    "Path to the file to edit. Absolute or relative to the project "
                    "working directory. Must point to an existing file inside the "
                    "project; edits outside the project are rejected."
                ),
            ),
            ToolParameter(
                name="edits",
                type="array",
                description=(
                    "Ordered array of edit objects. Edits apply sequentially — "
                    "later edits see the result of earlier ones."
                ),
                items={
                    "type": "object",
                    "properties": {
                        "old_string": {
                            "type": "string",
                            "description": (
                                "Exact substring to find — matched literally, not "
                                "as a regex. Must appear exactly once in the "
                                "file's current state at the time this edit is "
                                "applied (no `replace_all` equivalent here); "
                                "include surrounding context to make the match "
                                "unique."
                            ),
                        },
                        "new_string": {
                            "type": "string",
                            "description": (
                                "Text that replaces the match of `old_string`. "
                                "Use `\"\"` to delete it. Whitespace/indentation "
                                "is taken literally — preserve the surrounding "
                                "style."
                            ),
                        },
                    },
                    "required": ["old_string", "new_string"],
                },
            ),
        ],
        execute_fn=execute,
    )
