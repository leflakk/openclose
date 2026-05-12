"""Tests for utility modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from openclose.util.process import run, ProcessResult
from openclose.util.fs import load_gitignore, is_binary_extension, find_project_root
from openclose.util.git import is_git_repo, get_repo_root, get_current_branch


# --- process ---

@pytest.mark.asyncio
async def test_run_echo() -> None:
    result = await run("echo", "hello")
    assert result.ok
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_run_failure() -> None:
    result = await run("false")
    assert not result.ok


@pytest.mark.asyncio
async def test_run_timeout() -> None:
    result = await run("sleep", "10", timeout=0.1)
    assert not result.ok
    assert result.timed_out
    assert result.duration > 0
    assert "timed out" in result.stderr.lower()


@pytest.mark.asyncio
async def test_run_timeout_partial_stdout() -> None:
    result = await run(
        "bash", "-c", "echo partial_output && sleep 10",
        timeout=1.0,
    )
    assert result.timed_out
    assert "partial_output" in result.stdout


@pytest.mark.asyncio
async def test_run_with_cwd(tmp_path: Path) -> None:
    result = await run("pwd", cwd=str(tmp_path))
    assert result.ok
    assert str(tmp_path) in result.stdout


def test_process_result_ok() -> None:
    r = ProcessResult(returncode=0, stdout="out", stderr="")
    assert r.ok
    assert not r.timed_out
    r2 = ProcessResult(returncode=1, stdout="", stderr="err")
    assert not r2.ok


# --- fs ---

def test_load_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\nbuild/\n")
    spec = load_gitignore(tmp_path)
    assert spec is not None
    assert spec.match_file("app.log")
    assert not spec.match_file("main.py")


def test_load_gitignore_missing(tmp_path: Path) -> None:
    spec = load_gitignore(tmp_path)
    assert spec is None


def test_is_binary_extension() -> None:
    assert is_binary_extension(Path("image.png"))
    assert is_binary_extension(Path("archive.zip"))
    assert not is_binary_extension(Path("code.py"))
    assert not is_binary_extension(Path("readme.md"))


def test_find_project_root(tmp_path: Path) -> None:
    # Create a project marker
    (tmp_path / "pyproject.toml").write_text("")
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    root = find_project_root(sub)
    assert root == tmp_path


def test_find_project_root_no_marker(tmp_path: Path) -> None:
    sub = tmp_path / "deep" / "nested"
    sub.mkdir(parents=True)
    root = find_project_root(sub)
    assert root == sub.resolve()


# --- git ---

@pytest.mark.asyncio
async def test_is_git_repo_false(tmp_path: Path) -> None:
    assert not await is_git_repo(tmp_path)


@pytest.mark.asyncio
async def test_is_git_repo_true(tmp_path: Path) -> None:
    await run("git", "init", cwd=str(tmp_path))
    assert await is_git_repo(tmp_path)


@pytest.mark.asyncio
async def test_get_repo_root(tmp_path: Path) -> None:
    await run("git", "init", cwd=str(tmp_path))
    root = await get_repo_root(tmp_path)
    assert root is not None
    assert root == tmp_path


@pytest.mark.asyncio
async def test_get_repo_root_not_repo(tmp_path: Path) -> None:
    root = await get_repo_root(tmp_path)
    assert root is None


@pytest.mark.asyncio
async def test_get_current_branch(tmp_path: Path) -> None:
    await run("git", "init", cwd=str(tmp_path))
    await run("git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
              "commit", "--allow-empty", "-m", "init", cwd=str(tmp_path))
    branch = await get_current_branch(cwd=str(tmp_path))
    assert branch is not None
