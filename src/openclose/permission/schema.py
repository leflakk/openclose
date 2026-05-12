"""Permission request/response schemas."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PermissionRequest:
    """A request to use a tool."""

    tool_name: str
    path: str = ""
    arguments: dict[str, object] = field(default_factory=dict)
    request_id: str = ""


@dataclass
class PermissionResponse:
    """Response to a permission request."""

    allowed: bool
    reason: str = ""
    needs_ask: bool = False
    request_id: str = ""
