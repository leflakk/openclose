"""Content search tool — wraps ripgrep or falls back to Python."""

from __future__ import annotations

import shutil
from pathlib import Path

from openclose.tool.tool import Tool, ToolResult, ToolParameter
from openclose.tool.truncation import truncate_output
from openclose.util.process import run


def make_grep_tool(project_dir: str = ".") -> Tool:
    """Create the grep/search tool."""

    async def execute(
        pattern: str = "",
        path: str = "",
        include: str = "",
        **kwargs: object,
    ) -> ToolResult:
        base = path if path else project_dir
        if not Path(base).is_absolute():
            base = str(Path(project_dir) / base)

        # Prefer ripgrep
        rg = shutil.which("rg")
        if rg:
            args = [rg, "--no-heading", "--line-number", "--color=never", "-m", "200",
                    "--glob", "!.openclose/"]
            if include:
                args.extend(["--glob", include])
            args.extend([pattern, base])
            result = await run(*args, timeout=30.0)
            if result.returncode == 0:
                return ToolResult(output=truncate_output(result.stdout))
            if result.returncode == 1:
                return ToolResult(output="No matches found.")
            return ToolResult(error=result.stderr or "ripgrep failed")

        # Fallback: Python grep
        return await _python_grep(pattern, base, include)

    return Tool(
        name="grep",
        description='grep(pattern="def foo", path="src/", include="*.py")',
        parameters=[
            ToolParameter(
                name="pattern",
                description='"def foo"',
            ),
            ToolParameter(
                name="path",
                description='"src/"',
                required=False,
            ),
            ToolParameter(
                name="include",
                description='"*.py"',
                required=False,
            ),
        ],
        execute_fn=execute,
    )


async def _python_grep(pattern: str, base: str, include: str) -> ToolResult:
    """Fallback grep using Python re module."""
    import re
    from openclose.file.ignore import IgnoreManager

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return ToolResult(error=f"Invalid regex: {e}")

    base_path = Path(base)
    matches: list[str] = []

    try:
        if base_path.is_file():
            files = [base_path]
        elif base_path.is_dir():
            ignore = IgnoreManager(base_path)
            glob_pattern = include or "**/*"
            files = [
                f for f in base_path.glob(glob_pattern)
                if f.is_file() and not ignore.is_ignored(f)
            ][:1000]
        else:
            return ToolResult(error=f"Path not found: {base}")
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append(f"{f}:{i}:{line}")
                        if len(matches) >= 200:
                            break
            except (OSError, UnicodeDecodeError):
                continue
            if len(matches) >= 200:
                break
    except Exception as e:
        return ToolResult(error=f"Search error: {e}")

    if not matches:
        return ToolResult(output="No matches found.")

    return ToolResult(output=truncate_output("\n".join(matches)))
