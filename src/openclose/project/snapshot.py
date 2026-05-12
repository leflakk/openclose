"""Git snapshots — periodic GC and reflog cleanup."""

from __future__ import annotations

from pathlib import Path

from openclose.util.git import git
from openclose.log import get_logger

log = get_logger(__name__)


class SnapshotManager:
    """Manages git repository snapshots and cleanup."""

    def __init__(self, repo_root: Path) -> None:
        self._root = repo_root

    async def gc(self, prune_days: int = 7) -> bool:
        """Run git garbage collection."""
        result = await git(
            "gc", "--auto", f"--prune={prune_days}.days.ago",
            cwd=str(self._root),
            timeout=120.0,
        )
        if result.ok:
            log.debug("Git GC completed for %s", self._root)
        else:
            log.warning("Git GC failed: %s", result.stderr)
        return result.ok

    async def cleanup_reflog(self, expire_days: int = 7) -> bool:
        """Clean up the reflog."""
        result = await git(
            "reflog", "expire",
            f"--expire={expire_days}.days.ago",
            "--all",
            cwd=str(self._root),
        )
        return result.ok

    async def create_snapshot(self, message: str = "snapshot") -> str | None:
        """Create a snapshot commit of the current state.

        Returns the commit hash, or None on failure.
        """
        # Stage all changes
        await git("add", "-A", cwd=str(self._root))

        # Check if there are staged changes
        result = await git("diff", "--cached", "--quiet", cwd=str(self._root))
        if result.ok:
            # No changes to snapshot
            return None

        result = await git(
            "commit", "-m", message, "--allow-empty-message",
            cwd=str(self._root),
        )
        if not result.ok:
            log.warning("Snapshot commit failed: %s", result.stderr)
            return None

        # Get the commit hash
        hash_result = await git("rev-parse", "HEAD", cwd=str(self._root))
        if hash_result.ok:
            return hash_result.stdout.strip()
        return None
