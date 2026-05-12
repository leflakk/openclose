"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from openclose.config.config import load_config
from openclose.config.schema import OpenCloseConfig
from openclose.storage.db import Database


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory."""
    return tmp_path


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """Provide a fresh in-memory-like SQLite database."""
    db_path = tmp_path / "test.db"
    return Database(db_path)


@pytest.fixture()
def config(tmp_path: Path) -> OpenCloseConfig:
    """Provide a default config with project_dir set to tmp_path."""
    return load_config(project_dir=tmp_path)
