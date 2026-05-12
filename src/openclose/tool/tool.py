"""Tool base class and execution pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class ToolResult:
    """Result of a tool execution."""

    output: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error

    def to_string(self) -> str:
        if self.error and self.output:
            return f"{self.output}\nError: {self.error}"
        if self.error:
            return f"Error: {self.error}"
        return self.output


@dataclass
class ToolParameter:
    """A tool parameter definition."""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    default: Any = None
    enum: list[str] | None = None
    items: dict[str, Any] | None = None  # For array types: item schema


class Tool:
    """A tool that agents can invoke."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: list[ToolParameter] | None = None,
        execute_fn: Callable[..., Awaitable[ToolResult]] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters or []
        self._execute_fn = execute_fn

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with given arguments."""
        if self._execute_fn is not None:
            return await self._execute_fn(**kwargs)
        return ToolResult(error=f"Tool '{self.name}' has no execute function")

    def to_schema(self) -> dict[str, Any]:
        """Convert to a flat tool schema dict."""
        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in self.parameters:
            prop: dict[str, Any] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum:
                prop["enum"] = param.enum
            if param.items is not None:
                prop["items"] = param.items
            if param.default is not None:
                prop["default"] = param.default
            properties[param.name] = prop
            if param.required:
                required.append(param.name)

        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }
