"""Tests for the CLI."""

from __future__ import annotations

import subprocess
import sys


def test_cli_help() -> None:
    """CLI should print help."""
    result = subprocess.run(
        [sys.executable, "-m", "openclose", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "OpenClose" in result.stdout


def test_cli_sessions_command() -> None:
    """Sessions command should work (may be empty)."""
    result = subprocess.run(
        [sys.executable, "-m", "openclose", "sessions"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
