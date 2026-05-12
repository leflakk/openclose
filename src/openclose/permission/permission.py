"""Permission evaluation engine."""

from __future__ import annotations

from openclose.permission.rules import (
    PermissionRule,
    PermissionAction,
    READONLY_TOOLS,
)
from openclose.permission.schema import PermissionRequest, PermissionResponse
from openclose.config.config import get_config
from openclose.log import get_logger

log = get_logger(__name__)


class PermissionEngine:
    """Evaluates permission requests against rules.

    Rules are checked using last-match-wins semantics.
    If no rules match, default is ASK.
    """

    # Per-session engine cache so session grants persist across messages.
    _session_engines: dict[str, "PermissionEngine"] = {}

    def __init__(self, rules: list[PermissionRule] | None = None) -> None:
        self._rules: list[PermissionRule] = rules or []
        self._session_grants: set[str] = set()
        self._skip_all: bool = False

    @classmethod
    def from_config(cls) -> "PermissionEngine":
        """Create from the current config.

        Read-only tools (see ``READONLY_TOOLS``) are auto-allowed by
        default. User rules from ``config.permissions`` are appended
        after, so last-match-wins lets users opt back in to prompts by
        adding an ``ask`` (or ``deny``) rule for those tools.
        """
        config = get_config()
        rules: list[PermissionRule] = [
            PermissionRule(tool=name, path="*", action=PermissionAction.ALLOW)
            for name in READONLY_TOOLS
        ]
        for rule_cfg in config.permissions:
            rules.append(
                PermissionRule(
                    tool=rule_cfg.tool,
                    path=rule_cfg.path,
                    action=PermissionAction(rule_cfg.action),
                )
            )
        return cls(rules)

    @classmethod
    def for_session(cls, session_id: str) -> "PermissionEngine":
        """Get or create a PermissionEngine for a session.

        Reuses the same engine across messages so that session grants
        (from "Allow always") persist for the lifetime of the session.
        """
        if session_id not in cls._session_engines:
            cls._session_engines[session_id] = cls.from_config()
        return cls._session_engines[session_id]

    @classmethod
    def remove_session(cls, session_id: str) -> None:
        """Remove the cached engine for a session."""
        cls._session_engines.pop(session_id, None)

    def add_rule(self, rule: PermissionRule) -> None:
        """Add a permission rule."""
        self._rules.append(rule)

    def grant_session(self, tool_name: str) -> None:
        """Grant a tool permission for the current session."""
        self._session_grants.add(tool_name)

    def set_skip_all(self, skip: bool) -> None:
        """Enable or disable skip-all mode for this session."""
        self._skip_all = skip

    @property
    def skip_all(self) -> bool:
        """Whether skip-all mode is active."""
        return self._skip_all

    def check(self, request: PermissionRequest) -> PermissionResponse:
        """Check if a tool invocation is permitted.

        Evaluation order:
        1. Find last matching rule (last-match-wins, aligns with OpenCode's findLast).
        2. DENY → absolute, session grants cannot override.
        3. ALLOW → permitted.
        4. ASK or no match → check session grants, then return needs_ask.
        """
        # Find last matching rule
        matched_rule: PermissionRule | None = None
        for rule in self._rules:
            if rule.matches(request.tool_name, request.path):
                matched_rule = rule

        # DENY is absolute — session grants cannot override
        if matched_rule and matched_rule.action == PermissionAction.DENY:
            return PermissionResponse(
                allowed=False,
                reason=f"Denied by rule: {matched_rule.tool}:{matched_rule.path}",
            )

        # ALLOW — permitted
        if matched_rule and matched_rule.action == PermissionAction.ALLOW:
            return PermissionResponse(allowed=True, reason="rule: allow")

        # skip_all — auto-approve when in skip mode (DENY already handled above)
        if self._skip_all:
            return PermissionResponse(allowed=True, reason="skip_all active")

        # ASK or no match — check session grants first
        if request.tool_name in self._session_grants:
            return PermissionResponse(allowed=True, reason="session grant")

        # Needs user approval
        reason = (
            f"Requires approval: {request.tool_name}"
            if matched_rule
            else f"No rule matched for {request.tool_name}; approval required"
        )
        return PermissionResponse(
            allowed=False,
            needs_ask=True,
            reason=reason,
        )
