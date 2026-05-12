"""Cross-platform compatibility tests.

Exercises Windows-specific code paths from Linux/macOS via ``monkeypatch``
so the matrix CI catches regressions even on a single OS.
"""

from __future__ import annotations

import asyncio
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from openclose.config.config import load_config
from openclose.server.app import create_app
from openclose.tool.tools.bash import make_bash_tool
from openclose.util import process as process_mod


@pytest.fixture()
def client() -> TestClient:
    load_config()
    return TestClient(create_app())


def _stub_windows_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject Windows-only signal/subprocess constants on POSIX so tests run."""
    if not hasattr(signal, "CTRL_BREAK_EVENT"):
        monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 1, raising=False)
    if not hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)


# --- process.py: Windows branches ------------------------------------------

def test_kill_process_group_windows_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, CTRL_BREAK_EVENT is sent before kill(); SIGKILL is not used."""
    _stub_windows_constants(monkeypatch)
    monkeypatch.setattr("sys.platform", "win32")

    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.pid = 1234
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()

    process_mod._kill_process_group(proc)

    proc.send_signal.assert_called_once()
    proc.kill.assert_called_once()
    # The signal sent must be a Windows control event constant, not SIGKILL.
    sent_signal = proc.send_signal.call_args.args[0]
    assert int(sent_signal) == 1  # signal.CTRL_BREAK_EVENT == 1


def test_kill_process_group_posix_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """On POSIX, os.killpg is attempted; falls back to proc.kill on error."""
    monkeypatch.setattr("sys.platform", "linux")

    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.pid = 1234
    proc.kill = MagicMock()

    # Force killpg to raise so we exercise the fallback path.
    def raising_killpg(*_args: Any, **_kwargs: Any) -> None:
        raise ProcessLookupError

    monkeypatch.setattr("os.killpg", raising_killpg)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    process_mod._kill_process_group(proc)

    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_run_uses_creationflags_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows, run() passes creationflags and does NOT pass start_new_session."""
    _stub_windows_constants(monkeypatch)
    monkeypatch.setattr("sys.platform", "win32")

    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        captured["kwargs"] = kwargs
        # Return a minimal mock process that completes immediately.
        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await process_mod.run("echo", "hello")

    assert "creationflags" in captured["kwargs"]
    assert "start_new_session" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_run_uses_start_new_session_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On POSIX, run() passes start_new_session and does NOT pass creationflags."""
    monkeypatch.setattr("sys.platform", "linux")

    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["kwargs"] = kwargs
        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await process_mod.run("echo", "hello")

    assert captured["kwargs"].get("start_new_session") is True
    assert "creationflags" not in captured["kwargs"]


# --- bash tool: missing-bash guard -----------------------------------------

@pytest.mark.asyncio
async def test_bash_tool_missing_returns_helpful_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When bash is absent, the tool returns a guided error mentioning Git Bash/WSL."""
    monkeypatch.setattr(shutil, "which", lambda _: None)

    tool = make_bash_tool(str(tmp_path))
    result = await tool.execute(command="echo hello")

    assert not result.ok
    assert "bash not found" in result.error.lower()
    assert "git bash" in result.error.lower() or "wsl" in result.error.lower()


@pytest.mark.asyncio
async def test_bash_endpoint_missing_returns_127(
    monkeypatch: pytest.MonkeyPatch, client: TestClient,
) -> None:
    """The /api/bash endpoint returns 127 with a helpful stderr when bash is missing."""
    monkeypatch.setattr(shutil, "which", lambda _: None)

    resp = client.post("/api/bash", json={"command": "echo hello"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["returncode"] == 127
    assert "bash not found" in data["stderr"].lower()


# --- JSON path output: forward slashes only --------------------------------

def test_search_files_returns_posix_paths(client: TestClient) -> None:
    """File search results must use forward slashes in JSON paths."""
    resp = client.get("/api/files?q=&limit=50")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for entry in data:
        path = entry["path"]
        assert "\\" not in path, f"backslash leaked into JSON path: {path!r}"


def test_resolve_file_returns_posix_path(client: TestClient, tmp_path: Path) -> None:
    """/api/files/resolve must also return forward-slash paths."""
    resp = client.get("/api/files/resolve?name=pyproject.toml")
    assert resp.status_code == 200
    path = resp.json()["path"]
    if path:
        assert "\\" not in path


# --- Sandbox containment: Path.relative_to works for both separators -------

def test_sandbox_relative_to_containment_inside(tmp_path: Path) -> None:
    """Paths inside the project root pass the containment check."""
    root = tmp_path
    child = tmp_path / "subdir" / "file.txt"
    child.parent.mkdir()
    child.write_text("hi")

    try:
        child.resolve().relative_to(root.resolve())
        inside = True
    except ValueError:
        inside = False

    assert inside is True


def test_sandbox_relative_to_containment_outside(tmp_path: Path) -> None:
    """Paths outside the project root fail the containment check."""
    root = tmp_path / "project"
    root.mkdir()
    sibling = tmp_path / "other_file.txt"
    sibling.write_text("hi")

    try:
        sibling.resolve().relative_to(root.resolve())
        inside = True
    except ValueError:
        inside = False

    assert inside is False
