"""Tests for project detection and worktree management."""

from __future__ import annotations

from pathlib import Path

import pytest

from openclose.project.project import detect_project
from openclose.project.worktree import WorktreeManager
from openclose.project.snapshot import SnapshotManager
from openclose.util.process import run


@pytest.mark.asyncio
async def test_detect_project_with_git(tmp_path: Path) -> None:
    await run("git", "init", cwd=str(tmp_path))
    proj = await detect_project(tmp_path)
    assert proj.vcs == "git"
    assert proj.root == tmp_path


@pytest.mark.asyncio
async def test_detect_project_with_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    proj = await detect_project(tmp_path)
    assert proj.name == tmp_path.name


@pytest.mark.asyncio
async def test_detect_project_no_vcs(tmp_path: Path) -> None:
    proj = await detect_project(tmp_path)
    assert proj.vcs == ""


@pytest.mark.asyncio
async def test_worktree_list_empty(tmp_path: Path) -> None:
    await run("git", "init", cwd=str(tmp_path))
    await run("git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
              "commit", "--allow-empty", "-m", "init", cwd=str(tmp_path))
    mgr = WorktreeManager(tmp_path)
    wts = await mgr.list_worktrees()
    assert len(wts) >= 1  # Main worktree


@pytest.mark.asyncio
async def test_snapshot_gc(tmp_path: Path) -> None:
    await run("git", "init", cwd=str(tmp_path))
    await run("git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
              "commit", "--allow-empty", "-m", "init", cwd=str(tmp_path))
    snap = SnapshotManager(tmp_path)
    assert await snap.gc()


@pytest.mark.asyncio
async def test_snapshot_create(tmp_path: Path) -> None:
    await run("git", "init", cwd=str(tmp_path))
    await run("git", "config", "user.email", "test@test.com", cwd=str(tmp_path))
    await run("git", "config", "user.name", "Test", cwd=str(tmp_path))
    await run("git", "commit", "--allow-empty", "-m", "init", cwd=str(tmp_path))
    (tmp_path / "newfile.txt").write_text("content")
    snap = SnapshotManager(tmp_path)
    commit = await snap.create_snapshot("test snapshot")
    assert commit is not None


@pytest.mark.asyncio
async def test_snapshot_create_no_changes(tmp_path: Path) -> None:
    await run("git", "init", cwd=str(tmp_path))
    await run("git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
              "commit", "--allow-empty", "-m", "init", cwd=str(tmp_path))
    snap = SnapshotManager(tmp_path)
    commit = await snap.create_snapshot()
    assert commit is None  # No changes
