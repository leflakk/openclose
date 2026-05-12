"""Tool system — registry, execution pipeline, built-in tools."""

from openclose.tool.tool import Tool, ToolResult
from openclose.tool.registry import ToolRegistry
from openclose.tool.truncation import truncate_output

__all__ = [
    "Tool",
    "ToolResult",
    "ToolRegistry",
    "truncate_output",
]
