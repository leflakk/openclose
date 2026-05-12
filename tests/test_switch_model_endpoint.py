"""Tests for the /model switch and listing endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openclose.config.config import load_config
from openclose.config.schema import OpenCloseConfig, ProviderConfig
from openclose.server.app import create_app
from openclose.storage import db as db_mod


@pytest.fixture()
def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> TestClient:
    # Fresh per-test SQLite to avoid get_empty_session() reusing leftover
    # sessions from earlier tests (which would carry switched provider state).
    fresh = db_mod.Database(tmp_path / "test.db")
    monkeypatch.setattr(db_mod, "_db", fresh)

    load_config()
    fake_config = OpenCloseConfig(
        providers=[
            ProviderConfig(
                name="local",
                base_url="http://localhost:8000/v1",
                default_model="qwen-coder",
                models=["qwen-coder", "deepseek-coder"],
            ),
            ProviderConfig(
                name="openrouter",
                base_url="https://openrouter.ai/api/v1",
                api_key_env="OPENROUTER_API_KEY",
                default_model="anthropic/claude-3.5-sonnet",
                models=[
                    "anthropic/claude-3.5-sonnet",
                    "google/gemini-2.0-flash-001",
                ],
            ),
        ],
        default_provider="local",
    )
    # Routes call get_config() directly inside the request handler, so patch
    # the symbol in the routes module.
    monkeypatch.setattr(
        "openclose.server.routes.get_config", lambda: fake_config,
    )
    return TestClient(create_app())


def _new_session(client: TestClient) -> str:
    resp = client.post("/api/sessions", json={"title": "test", "agent": "build"})
    assert resp.status_code == 200
    sid: str = resp.json()["id"]
    return sid


def test_list_models_returns_flat_entries(client: TestClient) -> None:
    resp = client.get("/api/models")
    assert resp.status_code == 200
    items = resp.json()
    names = [(it["provider"], it["model"]) for it in items]
    assert ("local", "qwen-coder") in names
    assert ("local", "deepseek-coder") in names
    assert ("openrouter", "anthropic/claude-3.5-sonnet") in names
    # Each entry has a human label
    for it in items:
        assert it["label"] == f"{it['provider']} / {it['model']}"


def test_switch_model_persists_provider_and_model(client: TestClient) -> None:
    sid = _new_session(client)
    resp = client.post(
        f"/api/sessions/{sid}/model",
        json={"provider": "openrouter", "model": "google/gemini-2.0-flash-001"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "ok": True,
        "provider": "openrouter",
        "model": "google/gemini-2.0-flash-001",
    }

    # Read back
    got = client.get(f"/api/sessions/{sid}/model")
    assert got.status_code == 200
    g = got.json()
    assert g["provider"] == "openrouter"
    assert g["model"] == "google/gemini-2.0-flash-001"
    assert g["effective_provider"] == "openrouter"
    assert g["effective_model"] == "google/gemini-2.0-flash-001"


def test_switch_model_rejects_unknown_provider(client: TestClient) -> None:
    sid = _new_session(client)
    resp = client.post(
        f"/api/sessions/{sid}/model",
        json={"provider": "ghost", "model": "x"},
    )
    assert resp.status_code == 400
    assert "Unknown provider" in resp.json()["error"]


def test_switch_model_404_on_missing_session(client: TestClient) -> None:
    resp = client.post(
        "/api/sessions/does-not-exist/model",
        json={"provider": "local", "model": "qwen-coder"},
    )
    assert resp.status_code == 404


def test_get_session_model_resolves_defaults_for_fresh_session(
    client: TestClient,
) -> None:
    """A fresh session has empty provider/model — effective_* falls back to
    default_provider and that provider's default_model."""
    sid = _new_session(client)
    got = client.get(f"/api/sessions/{sid}/model")
    assert got.status_code == 200
    g = got.json()
    assert g["provider"] == ""
    assert g["model"] == ""
    assert g["effective_provider"] == "local"
    assert g["effective_model"] == "qwen-coder"
