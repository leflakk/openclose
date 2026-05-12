"""Tests for the FastAPI server."""

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


def test_index_page(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "OpenClose" in resp.text


def test_create_session_api(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={"title": "Test", "agent": "build"})
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["title"] == "Test"


def test_list_sessions_api(client: TestClient) -> None:
    # Create one
    client.post("/api/sessions", json={"title": "S1"})
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    sessions = resp.json()
    assert isinstance(sessions, list)


def test_session_page(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={"title": "Page Test"})
    sid = resp.json()["id"]
    resp = client.get(f"/session/{sid}")
    assert resp.status_code == 200
    assert "Page Test" in resp.text


def test_get_messages_api(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={"title": "Msg Test"})
    sid = resp.json()["id"]
    resp = client.get(f"/api/sessions/{sid}/messages")
    assert resp.status_code == 200
    assert resp.json() == []


def test_delete_session_api(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={"title": "Delete Me"})
    sid = resp.json()["id"]
    resp = client.delete(f"/api/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


def test_config_api(client: TestClient) -> None:
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "providers" in data
