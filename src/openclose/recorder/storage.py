"""Task file storage — per-project markdown files with YAML frontmatter.

Layout under `~/.config/openclose/<project>/recordings/`:
- `<slug>.md`                                → the task definition (markdown + frontmatter)
- `artifacts/<rec_id>/<rec_id>.mp4`          → raw screencast
- `artifacts/<rec_id>/<rec_id>.events.json`  → raw event log
- `artifacts/<rec_id>/<rec_id>.procedure.md` → VLM-produced procedure
- `artifacts/<rec_id>/<rec_id>.task_builder_raw.md` → raw second-pass LLM output
- `artifacts/<rec_id>/chunks/`               → per-chunk mp4 + events + procedure
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openclose.config.config import get_config
from openclose.config.paths import ConfigPaths


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class Task:
    slug: str
    name: str
    description: str
    body: str
    path: Path
    recording_id: str | None = None

    def to_summary(self) -> dict[str, str]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
        }


def tasks_dir() -> Path:
    """`~/.config/openclose/<project-name>/recordings/` — holds the task .md files."""
    config = get_config()
    d = ConfigPaths.project_runtime_dir(config.project_dir) / "recordings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifacts_dir() -> Path:
    """`~/.config/openclose/<project-name>/recordings/artifacts/` — raw video + events log."""
    d = tasks_dir() / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def recording_dir(recording_id: str) -> Path:
    """`~/.config/openclose/<project>/recordings/artifacts/<recording_id>/`.

    All artifacts for a single recording live in this subfolder: the mp4,
    events.json, procedure.md, task_builder_raw.md, and the chunks/ dir.
    """
    d = artifacts_dir() / recording_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def chunks_dir(recording_id: str) -> Path:
    """`~/.config/openclose/<project>/recordings/artifacts/<recording_id>/chunks/`."""
    d = recording_dir(recording_id) / "chunks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_chunk_procedure(recording_id: str, index: int, body: str) -> Path:
    """Persist a single chunk's VLM-produced procedure (for debugging)."""
    path = chunks_dir(recording_id) / f"{index:03d}.procedure.md"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def write_chunk_events(
    recording_id: str, index: int, events: list[dict[str, Any]],
) -> Path:
    """Persist a chunk's events list — the exact JSON sent to the VLM.

    Uses the same serialization as the chunk annotator's prompt
    (`indent=2`, `ensure_ascii=False`) so the file is byte-identical to
    what the model received.
    """
    path = chunks_dir(recording_id) / f"{index:03d}.events.json"
    path.write_text(
        json.dumps(events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return s or "task"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, m.group(2)


def _format_frontmatter(fm: dict[str, str]) -> str:
    lines = ["---"]
    for k, v in fm.items():
        v_clean = str(v).replace("\n", " ").strip()
        lines.append(f"{k}: {v_clean}")
    lines.append("---")
    return "\n".join(lines)


def reserve_task_slug(name: str) -> str:
    """Return an available (non-colliding) task slug for `name`.

    The caller is expected to write the `<slug>.md` file promptly; this
    helper does not create a placeholder.
    """
    base_slug = slugify(name)
    d = tasks_dir()
    slug = base_slug
    i = 2
    while (d / f"{slug}.md").exists():
        slug = f"{base_slug}-{i}"
        i += 1
    return slug


def write_task(
    name: str,
    description: str,
    body: str,
    recorded_at: str,
    recording_id: str | None = None,
    slug: str | None = None,
) -> Task:
    """Write a new task file. Auto-numbers slug on collision if not provided."""
    if slug is None:
        slug = reserve_task_slug(name)
    d = tasks_dir()
    fm: dict[str, str] = {
        "name": name.strip(),
        "description": description.strip(),
        "recorded_at": recorded_at,
    }
    if recording_id:
        fm["recording_id"] = recording_id
    body = body.strip() + "\n"
    content = f"{_format_frontmatter(fm)}\n\n{body}"
    path = d / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    return Task(
        slug=slug,
        name=fm["name"],
        description=fm["description"],
        body=body,
        path=path,
        recording_id=recording_id,
    )


def rename_recording_artifacts(old_id: str, new_id: str) -> None:
    """Rename a recording's subfolder + the files it contains.

    Moves `artifacts/<old_id>/` to `artifacts/<new_id>/`, then renames
    any file inside that starts with `<old_id>.` to use `<new_id>.` as
    its prefix. The chunks/ subdir is unaffected since it keeps its name.
    No-op when the two ids are equal.
    """
    if old_id == new_id:
        return
    d = artifacts_dir()
    old_dir = d / old_id
    new_dir = d / new_id
    if old_dir.exists():
        old_dir.rename(new_dir)
    if new_dir.is_dir():
        for path in list(new_dir.iterdir()):
            if path.is_file() and path.name.startswith(f"{old_id}."):
                suffix = path.name[len(old_id):]
                path.rename(new_dir / f"{new_id}{suffix}")


def write_recording_procedure(recording_id: str, body: str) -> Path:
    """Persist the raw VLM-produced procedural markdown alongside the video/events."""
    path = recording_dir(recording_id) / f"{recording_id}.procedure.md"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def read_task(slug: str) -> Task | None:
    path = tasks_dir() / f"{slug}.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text)
    return Task(
        slug=slug,
        name=fm.get("name", slug),
        description=fm.get("description", ""),
        body=body,
        path=path,
        recording_id=fm.get("recording_id") or None,
    )


def list_tasks() -> list[Task]:
    d = tasks_dir()
    out: list[Task] = []
    for path in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        slug = path.stem
        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        out.append(Task(
            slug=slug,
            name=fm.get("name", slug),
            description=fm.get("description", ""),
            body=body,
            path=path,
            recording_id=fm.get("recording_id") or None,
        ))
    return out


def delete_task(slug: str) -> bool:
    """Delete a task .md file and its associated artifacts subfolder."""
    path = tasks_dir() / f"{slug}.md"
    if not path.is_file():
        return False
    task = read_task(slug)
    path.unlink()
    if task and task.recording_id:
        rec_dir = artifacts_dir() / task.recording_id
        if rec_dir.is_dir():
            shutil.rmtree(rec_dir, ignore_errors=True)
    return True
