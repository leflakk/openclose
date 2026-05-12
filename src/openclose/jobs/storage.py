"""On-disk persistence for jobs and their run artifacts.

Layout under `~/.config/openclose/<project>/jobs/`:
- `<job-id>.json`                         → job config
- `<job-id>/<ts>-<run-id>/summary.json`   → run summary (this job)
- `<job-id>/<ts>-<run-id>/<skill>.jsonl`  → event log per skill
- `<job-id>/<ts>-<run-id>/<skill>.out.md` → final text per skill
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from openclose.config.config import get_config
from openclose.config.paths import ConfigPaths
from openclose.jobs.schema import JobConfig, JobRunSummary


_RUN_ID_RE = re.compile(r".+-(?P<ulid>[0-9A-Z]{26})$")


def jobs_dir() -> Path:
    """`~/.config/openclose/<project>/jobs/`."""
    config = get_config()
    d = ConfigPaths.project_runtime_dir(config.project_dir) / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_run_dir(job_id: str, run_folder: str) -> Path:
    """`jobs/<job-id>/<run_folder>/` — run_folder is `<ts>-<run_id>`."""
    d = jobs_dir() / job_id / run_folder
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Job CRUD ─────────────────────────────────────────────────────────


def write_job(job: JobConfig) -> Path:
    path = jobs_dir() / f"{job.id}.json"
    path.write_text(
        json.dumps(job.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (jobs_dir() / job.id).mkdir(parents=True, exist_ok=True)
    return path


def read_job(job_id: str) -> JobConfig | None:
    path = jobs_dir() / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return JobConfig.model_validate(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def list_jobs() -> list[JobConfig]:
    """All valid job configs, newest-modified first."""
    d = jobs_dir()
    out: list[JobConfig] = []
    for path in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            out.append(JobConfig.model_validate(raw))
        except (json.JSONDecodeError, ValueError):
            continue
    return out


def delete_job(job_id: str) -> bool:
    """Remove the config and all run artifacts for a job."""
    d = jobs_dir()
    path = d / f"{job_id}.json"
    runs = d / job_id
    existed = path.is_file()
    if existed:
        path.unlink()
    if runs.is_dir():
        shutil.rmtree(runs, ignore_errors=True)
    return existed


# ── Run summaries ────────────────────────────────────────────────────


def write_summary(job_id: str, run_folder: str, summary: JobRunSummary) -> Path:
    """Write/overwrite the run summary JSON; callers invoke this repeatedly."""
    d = job_run_dir(job_id, run_folder)
    path = d / "summary.json"
    path.write_text(
        json.dumps(summary.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def read_summary(job_id: str, run_folder: str) -> JobRunSummary | None:
    path = jobs_dir() / job_id / run_folder / "summary.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return JobRunSummary.model_validate(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def list_job_runs(job_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """List run folders newest-first with a compact status view."""
    d = jobs_dir() / job_id
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    folders = sorted(
        (p for p in d.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for folder in folders[:limit]:
        summary = read_summary(job_id, folder.name)
        if summary is None:
            out.append({
                "run_folder": folder.name,
                "status": "unknown",
                "started_at": "",
                "finished_at": "",
                "duration_s": 0.0,
                "skills": [],
            })
            continue
        out.append({
            "run_folder": folder.name,
            "run_id": summary.run_id,
            "status": summary.status,
            "started_at": summary.started_at,
            "finished_at": summary.finished_at,
            "duration_s": summary.duration_s,
            "skills": [
                {"slug": s.slug, "status": s.status, "duration_s": s.duration_s}
                for s in summary.skills
            ],
        })
    return out
