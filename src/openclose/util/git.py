"""Git subprocess wrapper."""

from __future__ import annotations

from pathlib import Path

from openclose.util.process import ProcessResult, run


async def git(
    *args: str,
    cwd: str | None = None,
    timeout: float = 30.0,
) -> ProcessResult:
    """Run a git command."""
    return await run("git", *args, cwd=cwd, timeout=timeout)


async def is_git_repo(path: Path) -> bool:
    """Check if a directory is inside a git repository."""
    result = await git("rev-parse", "--is-inside-work-tree", cwd=str(path))
    return result.ok and result.stdout.strip() == "true"


async def get_repo_root(path: Path) -> Path | None:
    """Get the root directory of the git repository containing path."""
    result = await git("rev-parse", "--show-toplevel", cwd=str(path))
    if result.ok:
        return Path(result.stdout.strip())
    return None


async def get_current_branch(cwd: str | None = None) -> str | None:
    """Get the current branch name."""
    result = await git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    if result.ok:
        return result.stdout.strip()
    return None
