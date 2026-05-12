"""Tool discovery and registration."""

from __future__ import annotations

from typing import Any

from openclose.tool.tool import Tool, ToolResult
from openclose.log import get_logger

log = get_logger(__name__)


class ToolRegistry:
    """Registry of available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        log.debug("Registered tool: %s", tool.name)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """List all registered tools."""
        return list(self._tools.values())

    def get_schemas(self) -> list[dict[str, Any]]:
        """Get flat tool schemas for all tools."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute a tool by name. Returns the ToolResult."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(error=f"Unknown tool '{name}'")
        try:
            return await tool.execute(**arguments)
        except Exception as e:
            log.error("Tool '%s' execution error: %s", name, e)
            return ToolResult(error=f"Error executing tool '{name}': {e}")
