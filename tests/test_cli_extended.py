"""Extended CLI tests for coverage."""

from __future__ import annotations

import subprocess
import sys


def test_cli_no_command() -> None:
    """Should print help when no command given."""
    result = subprocess.run(
        [sys.executable, "-m", "openclose"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "OpenClose" in result.stdout or "usage" in result.stdout.lower()


def test_cli_serve_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "openclose", "serve", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "host" in result.stdout


def test_cli_run_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "openclose", "run", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "prompt" in result.stdout
