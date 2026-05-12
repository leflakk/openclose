"""Permission rule definitions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch


# Tools whose effects are limited to reading local state (no writes, no
# shell, no network, no browser). Auto-allowed by
# PermissionEngine.from_config; user rules in config.toml override via
# last-match-wins.
READONLY_TOOLS: tuple[str, ...] = ("read", "grep", "glob")


class PermissionAction(Enum):
    """What to do when a rule matches."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class PermissionRule:
    """A single permission rule with glob matching."""

    tool: str = "*"
    path: str = "*"
    action: PermissionAction = PermissionAction.ASK
    arguments_pattern: str = "*"

    def matches(
        self,
        tool_name: str,
        path: str = "",
        arguments: dict[str, object] | None = None,
    ) -> bool:
        """Check if this rule matches the given tool, path, and arguments."""
        tool_match = self.tool == "*" or fnmatch(tool_name, self.tool)
        path_match = self.path == "*" or fnmatch(path, self.path)
        args_match = self.arguments_pattern == "*" or fnmatch(
            json.dumps(arguments or {}, sort_keys=True),
            self.arguments_pattern,
        )
        return tool_match and path_match and args_match
