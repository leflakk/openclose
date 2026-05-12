"""Extended route tests for coverage of server/routes.py."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openclose.server.app import create_app
from openclose.config.config import load_config


@pytest.fixture()
def client(tmp_path: object) -> TestClient:
    load_config()
    app = create_app()
    return TestClient(app)


def _create_session(client: TestClient, title: str = "Test") -> str:
    resp = client.post("/api/sessions", json={"title": title, "agent": "build"})
    result: str = resp.json()["id"]
    return result


# ── search files ────────────────────────────────────────────────────────────

def test_search_files_api(client: TestClient) -> None:
    resp = client.get("/api/files?q=&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_search_files_with_query(client: TestClient) -> None:
    resp = client.get("/api/files?q=py&limit=3")
    assert resp.status_code == 200


# ── bash ────────────────────────────────────────────────────────────────────

def test_run_bash(client: TestClient) -> None:
    resp = client.post("/api/bash", json={"command": "echo hello"})
    assert resp.status_code == 200
    data = resp.json()
    assert "stdout" in data
    assert "hello" in data["stdout"]
    assert "returncode" in data


# ── interrupt ───────────────────────────────────────────────────────────────

def test_interrupt_session(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(f"/api/sessions/{sid}/interrupt")
    assert resp.status_code == 200
    data = resp.json()
    assert "ok" in data


# ── permissions ─────────────────────────────────────────────────────────────

def test_permission_reply_invalid(client: TestClient) -> None:
    resp = client.post("/api/permissions/fake-id/reply", json={"reply": "invalid"})
    assert resp.status_code == 400


def test_permission_reply_not_found(client: TestClient) -> None:
    resp = client.post("/api/permissions/fake-id/reply", json={"reply": "once"})
    assert resp.status_code == 404


def test_toggle_skip_permissions(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(f"/api/sessions/{sid}/skip-permissions")
    assert resp.status_code == 200
    data = resp.json()
    assert "skip_all" in data


def test_get_skip_permissions(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.get(f"/api/sessions/{sid}/skip-permissions")
    assert resp.status_code == 200
    assert "skip_all" in resp.json()


# ── plan reply ──────────────────────────────────────────────────────────────

def test_plan_reply_not_found(client: TestClient) -> None:
    resp = client.post("/api/plan/fake-id/reply", json={"action": "execute"})
    assert resp.status_code == 404


def test_plan_reply_invalid_action(client: TestClient) -> None:
    resp = client.post("/api/plan/fake-id/reply", json={"action": "invalid"})
    assert resp.status_code == 400


def test_plan_reply_execute_clear_not_found(client: TestClient) -> None:
    resp = client.post("/api/plan/fake-id/reply", json={"action": "execute_clear"})
    assert resp.status_code == 404


# ── rename session ──────────────────────────────────────────────────────────

def test_rename_session(client: TestClient) -> None:
    sid = _create_session(client, "Old Name")
    resp = client.patch(f"/api/sessions/{sid}/rename", json={"title": "New Name"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["title"] == "New Name"


# ── fork session ────────────────────────────────────────────────────────────

def test_fork_session(client: TestClient) -> None:
    sid = _create_session(client, "Original")
    resp = client.post(f"/api/sessions/{sid}/fork", json={"agent": "build"})
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["id"] != sid


def test_fork_session_not_found(client: TestClient) -> None:
    resp = client.post("/api/sessions/nonexistent/fork", json={"agent": "build"})
    assert resp.status_code == 404


def test_fork_session_up_to_message_id(client: TestClient) -> None:
    from openclose.storage.db import get_db
    from openclose.session.session import SessionManager
    from openclose.session.message import MessageRole

    sid = _create_session(client, "Original")
    db = get_db()
    mgr = SessionManager(db)
    mgr.add_message(sid, MessageRole.USER, content="first")
    cut = mgr.add_message(sid, MessageRole.ASSISTANT, content="second")
    mgr.add_message(sid, MessageRole.USER, content="third")

    resp = client.post(
        f"/api/sessions/{sid}/fork",
        json={"up_to_message_id": cut.id},
    )
    assert resp.status_code == 200
    new_id = resp.json()["id"]

    fork_msgs = mgr.get_messages(new_id)
    assert len(fork_msgs) == 2
    assert [m.content for m in fork_msgs] == ["first", "second"]
    assert all(m.id != cut.id for m in fork_msgs)  # IDs remapped


def test_fork_session_up_to_message_id_cross_session(client: TestClient) -> None:
    from openclose.storage.db import get_db
    from openclose.session.session import SessionManager
    from openclose.session.message import MessageRole

    db = get_db()
    mgr = SessionManager(db)
    # Add a message to A right away so the empty-session reuse in
    # /api/sessions can't make B an alias of A.
    sid_a = _create_session(client, "A")
    mgr.add_message(sid_a, MessageRole.USER, content="hi A")
    sid_b = _create_session(client, "B")
    assert sid_a != sid_b
    msg_b = mgr.add_message(sid_b, MessageRole.USER, content="hi B")

    resp = client.post(
        f"/api/sessions/{sid_a}/fork",
        json={"up_to_message_id": msg_b.id},
    )
    assert resp.status_code == 404


def test_fork_session_up_to_part_id(client: TestClient) -> None:
    """A single DB assistant message can contain many parts (text +
    tool_calls + results across multiple segments). Fork-from-bubble must
    truncate parts within the target message at the last part of the
    clicked segment — otherwise later segments leak into the fork.
    """
    import json as jsonmod
    from openclose.storage.db import get_db
    from openclose.session.session import SessionManager
    from openclose.session.message import MessageRole, MessagePartType

    sid = _create_session(client, "Multi-segment")
    db = get_db()
    mgr = SessionManager(db)
    mgr.add_message(sid, MessageRole.USER, content="please")
    asst = mgr.add_message(sid, MessageRole.ASSISTANT, content="t1 t2")

    mgr.add_message_part(asst.id, MessagePartType.TEXT, content="t1 ")
    mgr.add_message_part(
        asst.id, MessagePartType.TOOL_CALL,
        content=jsonmod.dumps({"file_path": "/tmp/a.py", "content": "a"}),
        tool_name="write", tool_call_id="tc-A",
    )
    p3 = mgr.add_message_part(
        asst.id, MessagePartType.TOOL_RESULT,
        content="ok", tool_call_id="tc-A",
    )
    # Beyond the cut: subsequent segment of the same message.
    mgr.add_message_part(asst.id, MessagePartType.TEXT, content="t2")
    mgr.add_message_part(
        asst.id, MessagePartType.TOOL_CALL,
        content=jsonmod.dumps({"file_path": "/tmp/b.py", "content": "b"}),
        tool_name="write", tool_call_id="tc-B",
    )
    mgr.add_message_part(
        asst.id, MessagePartType.TOOL_RESULT,
        content="ok", tool_call_id="tc-B",
    )

    resp = client.post(
        f"/api/sessions/{sid}/fork",
        json={"up_to_message_id": asst.id, "up_to_part_id": p3.id},
    )
    assert resp.status_code == 200
    new_id = resp.json()["id"]

    new_msgs = mgr.get_messages(new_id)
    assert len(new_msgs) == 2
    new_asst = new_msgs[-1]
    new_parts = mgr.get_message_parts(new_asst.id)
    # Only [text "t1 ", tool_call A, tool_result A] kept
    assert [p.part_type for p in new_parts] == ["text", "tool_call", "tool_result"]
    assert new_parts[0].content == "t1 "
    assert new_parts[1].tool_name == "write"
    assert jsonmod.loads(new_parts[1].content)["file_path"] == "/tmp/a.py"
    # Assistant message content is recomputed from kept TEXT parts only
    assert new_asst.content == "t1 "


def test_fork_session_up_to_part_id_wrong_message(client: TestClient) -> None:
    """Cross-message part-id must 404 — guard against tampering."""
    from openclose.storage.db import get_db
    from openclose.session.session import SessionManager
    from openclose.session.message import MessageRole, MessagePartType

    sid = _create_session(client, "Guard")
    db = get_db()
    mgr = SessionManager(db)
    a = mgr.add_message(sid, MessageRole.ASSISTANT, content="hello A")
    b = mgr.add_message(sid, MessageRole.ASSISTANT, content="hello B")
    pb = mgr.add_message_part(b.id, MessagePartType.TEXT, content="hello B")

    resp = client.post(
        f"/api/sessions/{sid}/fork",
        json={"up_to_message_id": a.id, "up_to_part_id": pb.id},
    )
    assert resp.status_code == 404


def test_fork_session_inherits_agent(client: TestClient) -> None:
    from openclose.storage.db import get_db
    from openclose.session.session import SessionManager

    resp = client.post("/api/sessions", json={"title": "Plan src", "agent": "plan"})
    sid = resp.json()["id"]

    fork_resp = client.post(f"/api/sessions/{sid}/fork", json={})
    assert fork_resp.status_code == 200
    new_id = fork_resp.json()["id"]

    db = get_db()
    mgr = SessionManager(db)
    forked = mgr.get_session(new_id)
    assert forked is not None
    assert forked.agent == "plan"


# ── undo message ────────────────────────────────────────────────────────────

def test_undo_message(client: TestClient) -> None:
    sid = _create_session(client)
    # Add messages directly via session manager
    from openclose.storage.db import get_db
    from openclose.session.session import SessionManager
    from openclose.session.message import MessageRole

    db = get_db()
    mgr = SessionManager(db)
    mgr.add_message(sid, MessageRole.USER, content="hello")
    mgr.add_message(sid, MessageRole.ASSISTANT, content="hi")

    resp = client.post(f"/api/sessions/{sid}/undo")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["removed"] == 2


def test_undo_message_empty(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(f"/api/sessions/{sid}/undo")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 0


# ── export session ──────────────────────────────────────────────────────────

def test_export_session(client: TestClient) -> None:
    sid = _create_session(client, "Export Test")
    resp = client.get(f"/api/sessions/{sid}/export")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session"]["id"] == sid
    assert data["session"]["title"] == "Export Test"
    assert isinstance(data["messages"], list)


def test_export_session_not_found(client: TestClient) -> None:
    resp = client.get("/api/sessions/nonexistent/export")
    assert resp.status_code == 404


# ── compact session ─────────────────────────────────────────────────────────

def test_compact_session(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(f"/api/sessions/{sid}/compact")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "compacted" in data


# ── list agents ─────────────────────────────────────────────────────────────

def test_list_agents(client: TestClient) -> None:
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    agents = resp.json()
    assert isinstance(agents, list)
    assert any(a["name"] for a in agents)
    # `delegate` is a tool, not a switchable agent — must never appear.
    names = [a["name"] for a in agents]
    assert "delegate" not in names


# ── agent validation on session endpoints ───────────────────────────────────

def test_fork_session_rejects_unknown_agent(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(f"/api/sessions/{sid}/fork", json={"agent": "nope"})
    assert resp.status_code == 400


def test_fork_session_rejects_delegate(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(f"/api/sessions/{sid}/fork", json={"agent": "delegate"})
    assert resp.status_code == 400


def test_switch_agent_rejects_delegate(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(f"/api/sessions/{sid}/agent", json={"agent": "delegate"})
    assert resp.status_code == 400


def test_create_session_rejects_delegate(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={"title": "t", "agent": "delegate"})
    assert resp.status_code == 400
