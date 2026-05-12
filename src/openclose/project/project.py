"""Project discovery and VCS detection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openclose.util.git import is_git_repo, get_repo_root
from openclose.util.fs import find_project_root


@dataclass
class ProjectInfo:
    """Information about a detected project."""

    root: Path
    name: str
    vcs: str  # "git", "hg", "pijul", ""


async def detect_project(start: Path | None = None) -> ProjectInfo:
    """Detect the project from the given directory."""
    if start is None:
        start = Path.cwd()

    root = find_project_root(start)
    name = root.name

    # Detect VCS
    vcs = ""
    if await is_git_repo(root):
        vcs = "git"
        git_root = await get_repo_root(root)
        if git_root:
            root = git_root
            name = root.name
    elif (root / ".hg").is_dir():
        vcs = "hg"
    elif (root / ".pijul").is_dir():
        vcs = "pijul"

    return ProjectInfo(root=root, name=name, vcs=vcs)
