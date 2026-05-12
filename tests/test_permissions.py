"""Tests for the permission system."""

from __future__ import annotations

import pytest

from openclose.config.schema import OpenCloseConfig, PermissionRuleConfig
from openclose.permission.permission import PermissionEngine
from openclose.permission.rules import (
    PermissionRule,
    PermissionAction,
    READONLY_TOOLS,
)
from openclose.permission.schema import PermissionRequest


def test_allow_rule() -> None:
    engine = PermissionEngine(
        rules=[PermissionRule(tool="read", action=PermissionAction.ALLOW)]
    )
    resp = engine.check(PermissionRequest(tool_name="read"))
    assert resp.allowed


def test_deny_rule() -> None:
    engine = PermissionEngine(
        rules=[PermissionRule(tool="bash", action=PermissionAction.DENY)]
    )
    resp = engine.check(PermissionRequest(tool_name="bash"))
    assert not resp.allowed
    assert "Denied" in resp.reason


def test_ask_rule() -> None:
    engine = PermissionEngine(
        rules=[PermissionRule(tool="write", action=PermissionAction.ASK)]
    )
    resp = engine.check(PermissionRequest(tool_name="write"))
    assert not resp.allowed
    assert resp.needs_ask
    assert "approval" in resp.reason.lower()


def test_default_is_ask() -> None:
    engine = PermissionEngine(rules=[])
    resp = engine.check(PermissionRequest(tool_name="anything"))
    assert not resp.allowed
    assert resp.needs_ask


def test_wildcard_tool() -> None:
    engine = PermissionEngine(
        rules=[PermissionRule(tool="*", action=PermissionAction.ALLOW)]
    )
    resp = engine.check(PermissionRequest(tool_name="bash"))
    assert resp.allowed


def test_path_matching() -> None:
    PermissionEngine(
        rules=[
            PermissionRule(
                tool="write",
                path="/safe/*",
                action=PermissionAction.ALLOW,
            ),
            PermissionRule(
                tool="write",
                path="*",
                action=PermissionAction.DENY,
            ),
        ]
    )
    # Last match wins: DENY matches last for /safe/file.txt (path="*" matches too)
    # But /safe/* also matches — both match, last is DENY with path="*"
    # Actually: for /safe/file.txt, both rules match. Last match is DENY (path="*").
    # For /other/file.txt, only DENY matches.
    # We need to keep the test intent: allow writes to /safe, deny elsewhere.
    # With last-match-wins, put the DENY *first* and ALLOW *last* for overrides.
    engine2 = PermissionEngine(
        rules=[
            PermissionRule(
                tool="write",
                path="*",
                action=PermissionAction.DENY,
            ),
            PermissionRule(
                tool="write",
                path="/safe/*",
                action=PermissionAction.ALLOW,
            ),
        ]
    )
    resp = engine2.check(PermissionRequest(tool_name="write", path="/safe/file.txt"))
    assert resp.allowed

    resp = engine2.check(PermissionRequest(tool_name="write", path="/other/file.txt"))
    assert not resp.allowed


def test_session_grant_works_for_ask() -> None:
    """Session grants override ASK rules but not DENY rules."""
    engine = PermissionEngine(
        rules=[PermissionRule(tool="bash", action=PermissionAction.ASK)]
    )
    # Normally needs ask
    resp = engine.check(PermissionRequest(tool_name="bash"))
    assert not resp.allowed
    assert resp.needs_ask

    # Grant for session
    engine.grant_session("bash")
    resp = engine.check(PermissionRequest(tool_name="bash"))
    assert resp.allowed


def test_session_grant_cannot_override_deny() -> None:
    """DENY rules are absolute — session grants cannot override them."""
    engine = PermissionEngine(
        rules=[PermissionRule(tool="bash", action=PermissionAction.DENY)]
    )
    # Denied
    resp = engine.check(PermissionRequest(tool_name="bash"))
    assert not resp.allowed

    # Session grant does NOT override DENY
    engine.grant_session("bash")
    resp = engine.check(PermissionRequest(tool_name="bash"))
    assert not resp.allowed
    assert "Denied" in resp.reason


def test_last_match_wins() -> None:
    """When multiple rules match, the last one wins."""
    engine = PermissionEngine(
        rules=[
            PermissionRule(tool="bash", action=PermissionAction.ALLOW),
            PermissionRule(tool="bash", action=PermissionAction.DENY),
        ]
    )
    # DENY is last → denied
    resp = engine.check(PermissionRequest(tool_name="bash"))
    assert not resp.allowed


def test_from_config_auto_allows_readonly_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_config seeds ALLOW rules for read-only tools by default."""
    monkeypatch.setattr(
        "openclose.permission.permission.get_config",
        lambda: OpenCloseConfig(permissions=[]),
    )
    engine = PermissionEngine.from_config()
    for name in READONLY_TOOLS:
        resp = engine.check(PermissionRequest(tool_name=name))
        assert resp.allowed, f"{name} should be auto-allowed"
    # Non-readonly tools still require approval.
    resp = engine.check(PermissionRequest(tool_name="bash"))
    assert not resp.allowed
    assert resp.needs_ask


def test_from_config_user_rule_overrides_readonly_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user ASK/DENY rule for a read-only tool wins via last-match-wins."""
    monkeypatch.setattr(
        "openclose.permission.permission.get_config",
        lambda: OpenCloseConfig(
            permissions=[PermissionRuleConfig(tool="read", action="ask")],
        ),
    )
    engine = PermissionEngine.from_config()
    resp = engine.check(PermissionRequest(tool_name="read"))
    assert not resp.allowed
    assert resp.needs_ask
