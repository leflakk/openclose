"""Tests for jobs.storage — on-disk CRUD for jobs and run summaries."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from openclose.config.config import load_config
from openclose.config.paths import ConfigPaths
from openclose.jobs.schema import (
    JobConfig,
    JobNotification,
    JobRunSummary,
    JobTiming,
    SkillRunSummary,
)
from openclose.jobs.storage import (
    delete_job,
    job_run_dir,
    jobs_dir,
    list_job_runs,
    list_jobs,
    read_job,
    read_summary,
    write_job,
    write_summary,
)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect project_runtime_dir to tmp_path so jobs/ lives in tmp."""
    monkeypatch.setattr(
        ConfigPaths, "project_runtime_dir",
        classmethod(lambda cls, project_dir: tmp_path),
    )
    load_config(project_dir=tmp_path)
    yield tmp_path


def _sample_job(job_id: str = "j1") -> JobConfig:
    return JobConfig(
        id=job_id,
        name=f"Sample {job_id}",
        skills=["my-skill"],
        timing=JobTiming(mode="recurring", cron="0 9 * * *", timezone="UTC"),
        notification=JobNotification(),
    )


def _sample_summary(run_id: str = "r1") -> JobRunSummary:
    return JobRunSummary(
        job_id="j1",
        job_name="Sample j1",
        run_id=run_id,
        started_at="2025-01-01T09:00:00+00:00",
        finished_at="2025-01-01T09:01:00+00:00",
        duration_s=60.0,
        status="passed",
        skills=[SkillRunSummary(slug="my-skill", status="passed", duration_s=60.0)],
    )


# ───────────────────────── dirs ─────────────────────────

def test_jobs_dir_is_created(runtime: Path) -> None:
    d = jobs_dir()
    assert d.is_dir()
    assert d == runtime / "jobs"


def test_job_run_dir_is_created(runtime: Path) -> None:
    d = job_run_dir("j1", "2025-01-01T09-00-00-ABC")
    assert d.is_dir()


# ───────────────────────── job CRUD ─────────────────────

def test_write_and_read_job(runtime: Path) -> None:
    job = _sample_job()
    write_job(job)
    loaded = read_job("j1")
    assert loaded is not None
    assert loaded.id == "j1"
    assert loaded.name == "Sample j1"
    assert loaded.timing.cron == "0 9 * * *"


def test_read_missing_job_returns_none(runtime: Path) -> None:
    assert read_job("does-not-exist") is None


def test_read_job_with_invalid_json_returns_none(runtime: Path) -> None:
    path = jobs_dir() / "broken.json"
    path.write_text("{not valid", encoding="utf-8")
    assert read_job("broken") is None


def test_read_job_with_invalid_schema_returns_none(runtime: Path) -> None:
    path = jobs_dir() / "badschema.json"
    path.write_text('{"id": "x", "name": "n"}', encoding="utf-8")
    # Missing required `timing` → validation error
    assert read_job("badschema") is None


def test_write_job_creates_run_folder(runtime: Path) -> None:
    write_job(_sample_job())
    assert (jobs_dir() / "j1").is_dir()


def test_list_jobs_sorted_newest_first(runtime: Path) -> None:
    import os
    import time
    write_job(_sample_job("old"))
    # Backdate the first write to ensure ordering by mtime.
    old_path = jobs_dir() / "old.json"
    past = time.time() - 60
    os.utime(old_path, (past, past))
    write_job(_sample_job("new"))
    jobs = list_jobs()
    slugs = [j.id for j in jobs]
    assert slugs[0] == "new"
    assert "old" in slugs


def test_list_jobs_skips_invalid(runtime: Path) -> None:
    write_job(_sample_job())
    (jobs_dir() / "broken.json").write_text("not json", encoding="utf-8")
    jobs = list_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "j1"


def test_delete_job_removes_config_and_runs(runtime: Path) -> None:
    write_job(_sample_job())
    # Add a run artifact
    job_run_dir("j1", "run1")
    (jobs_dir() / "j1" / "run1" / "summary.json").write_text("{}", encoding="utf-8")

    assert delete_job("j1") is True
    assert read_job("j1") is None
    assert not (jobs_dir() / "j1").exists()


def test_delete_missing_job_returns_false(runtime: Path) -> None:
    assert delete_job("never-existed") is False


# ───────────────────────── summaries ─────────────────────

def test_write_and_read_summary(runtime: Path) -> None:
    summary = _sample_summary()
    write_summary("j1", "run-folder", summary)
    loaded = read_summary("j1", "run-folder")
    assert loaded is not None
    assert loaded.run_id == "r1"
    assert loaded.status == "passed"
    assert len(loaded.skills) == 1


def test_read_missing_summary_returns_none(runtime: Path) -> None:
    assert read_summary("j1", "nope") is None


def test_read_corrupt_summary_returns_none(runtime: Path) -> None:
    d = job_run_dir("j1", "broken-run")
    (d / "summary.json").write_text("nope", encoding="utf-8")
    assert read_summary("j1", "broken-run") is None


# ───────────────────────── list_job_runs ─────────────────

def test_list_job_runs_empty_if_no_job_dir(runtime: Path) -> None:
    assert list_job_runs("never-existed") == []


def test_list_job_runs_returns_sorted(runtime: Path) -> None:
    import os
    import time

    summary = _sample_summary("r1")
    write_summary("j1", "2025-01-01-a", summary)

    older_path = jobs_dir() / "j1" / "2025-01-01-a"
    past = time.time() - 120
    os.utime(older_path, (past, past))

    summary2 = _sample_summary("r2")
    write_summary("j1", "2025-01-02-b", summary2)

    runs = list_job_runs("j1")
    assert len(runs) == 2
    assert runs[0]["run_id"] == "r2"
    assert runs[1]["run_id"] == "r1"
    assert runs[0]["status"] == "passed"
    assert runs[0]["skills"][0]["slug"] == "my-skill"


def test_list_job_runs_limit(runtime: Path) -> None:
    for i in range(5):
        write_summary("j1", f"run-{i}", _sample_summary(f"r{i}"))
    runs = list_job_runs("j1", limit=2)
    assert len(runs) == 2


def test_list_job_runs_tolerates_missing_summary(runtime: Path) -> None:
    """A run folder without summary.json should still list with status=unknown."""
    job_run_dir("j1", "orphan-run")
    runs = list_job_runs("j1")
    assert len(runs) == 1
    assert runs[0]["status"] == "unknown"
    assert runs[0]["run_folder"] == "orphan-run"
