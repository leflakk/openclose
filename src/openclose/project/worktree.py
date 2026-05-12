"""Git worktree management."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openclose.util.git import git
from openclose.log import get_logger

log = get_logger(__name__)


@dataclass
class Worktree:
    """A git worktree."""

    path: Path
    branch: str
    head: str


class WorktreeManager:
    """Manages git worktrees for parallel work."""

    def __init__(self, repo_root: Path) -> None:
        self._root = repo_root

    async def list_worktrees(self) -> list[Worktree]:
        """List all worktrees."""
        result = await git("worktree", "list", "--porcelain", cwd=str(self._root))
        if not result.ok:
            return []

        worktrees: list[Worktree] = []
        current: dict[str, str] = {}

        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                if current:
                    worktrees.append(
                        Worktree(
                            path=Path(current.get("worktree", "")),
                            branch=current.get("branch", ""),
                            head=current.get("HEAD", ""),
                        )
                    )
                current = {"worktree": line.split(" ", 1)[1]}
            elif line.startswith("HEAD "):
                current["HEAD"] = line.split(" ", 1)[1]
            elif line.startswith("branch "):
                current["branch"] = line.split(" ", 1)[1]

        if current:
            worktrees.append(
                Worktree(
                    path=Path(current.get("worktree", "")),
                    branch=current.get("branch", ""),
                    head=current.get("HEAD", ""),
                )
            )

        return worktrees

    async def create(self, path: Path, branch: str) -> bool:
        """Create a new worktree."""
        result = await git(
            "worktree", "add", str(path), "-b", branch,
            cwd=str(self._root),
        )
        if result.ok:
            log.info("Created worktree at %s on branch %s", path, branch)
        else:
            log.error("Failed to create worktree: %s", result.stderr)
        return result.ok

    async def remove(self, path: Path) -> bool:
        """Remove a worktree."""
        result = await git(
            "worktree", "remove", str(path),
            cwd=str(self._root),
        )
        return result.ok
