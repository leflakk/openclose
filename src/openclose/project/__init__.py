"""Project and worktree management."""

from openclose.project.project import ProjectInfo, detect_project
from openclose.project.worktree import WorktreeManager
from openclose.project.snapshot import SnapshotManager

__all__ = ["ProjectInfo", "detect_project", "WorktreeManager", "SnapshotManager"]
