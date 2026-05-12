"""Tests for jobs.runner — pure helpers + end-to-end run orchestration."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from openclose.config.config import load_config
from openclose.config.paths import ConfigPaths
from openclose.jobs.runner import (
    _build_notification_text,
    _derive_overall_status,
    _now_iso,
    _run_folder,
    _should_notify,
    run_job,
)
from openclose.jobs.schema import (
    JobConfig,
    JobNotification,
    JobRunSummary,
    JobTiming,
    SkillRunSummary,
)
from openclose.skills.schema import RequiredTool, Skill


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(
        ConfigPaths, "project_runtime_dir",
        classmethod(lambda cls, project_dir: tmp_path),
    )
    load_config(project_dir=tmp_path)
    yield tmp_path


# ───────────────────────── small helpers ──────────────────────────

def test_now_iso_roundtrip() -> None:
    from datetime import datetime
    s = _now_iso()
    parsed = datetime.fromisoformat(s)
    assert parsed.tzinfo is not None


def test_run_folder_shape() -> None:
    s = _run_folder("ULID123")
    assert s.endswith("-ULID123")
    # colons must have been replaced
    assert ":" not in s


# ───────────────────────── _derive_overall_status ────────────────

def test_derive_status_empty_is_passed() -> None:
    assert _derive_overall_status([]) == "passed"


def test_derive_status_all_passed() -> None:
    slots = [
        SkillRunSummary(slug="a", status="passed"),
        SkillRunSummary(slug="b", status="passed"),
    ]
    assert _derive_overall_status(slots) == "passed"


def test_derive_status_all_failed_is_failed() -> None:
    slots = [
        SkillRunSummary(slug="a", status="failed"),
        SkillRunSummary(slug="b", status="failed"),
    ]
    assert _derive_overall_status(slots) == "failed"


def test_derive_status_mixed_is_partial() -> None:
    slots = [
        SkillRunSummary(slug="a", status="passed"),
        SkillRunSummary(slug="b", status="failed"),
    ]
    assert _derive_overall_status(slots) == "partial"


def test_derive_status_passed_with_skipped_is_partial() -> None:
    slots = [
        SkillRunSummary(slug="a", status="passed"),
        SkillRunSummary(slug="b", status="skipped"),
    ]
    assert _derive_overall_status(slots) == "partial"


def test_derive_status_only_skipped_is_failed() -> None:
    slots = [SkillRunSummary(slug="a", status="skipped")]
    assert _derive_overall_status(slots) == "failed"


# ───────────────────────── _should_notify ────────────────────────

def _job_with(channel: str = "me", notify_on: str = "failure") -> JobConfig:
    return JobConfig(
        id="j",
        name="n",
        skills=[],
        timing=JobTiming(mode="recurring", cron="* * * * *"),
        notification=JobNotification(channel=channel, notify_on=notify_on),
    )


def _summary_with(status: str) -> JobRunSummary:
    return JobRunSummary(
        job_id="j", job_name="n", run_id="r", status=status,
    )


def test_should_notify_false_when_no_channel() -> None:
    job = _job_with(channel="")
    assert _should_notify(job, _summary_with("failed")) is False


def test_should_notify_always() -> None:
    job = _job_with(notify_on="always")
    assert _should_notify(job, _summary_with("passed")) is True
    assert _should_notify(job, _summary_with("failed")) is True


def test_should_notify_failure_only_fires_on_failed() -> None:
    job = _job_with(notify_on="failure")
    assert _should_notify(job, _summary_with("passed")) is False
    assert _should_notify(job, _summary_with("failed")) is True
    assert _should_notify(job, _summary_with("partial")) is True


def test_should_notify_verification_fail_treats_partial_as_fail() -> None:
    job = _job_with(notify_on="verification_fail")
    assert _should_notify(job, _summary_with("failed")) is True
    assert _should_notify(job, _summary_with("partial")) is True
    assert _should_notify(job, _summary_with("passed")) is False


# ───────────────────────── _build_notification_text ───────────────

def test_build_notification_text_has_all_sections() -> None:
    job = JobConfig(
        id="j", name="Daily",
        skills=[],
        timing=JobTiming(mode="recurring", cron="* * * * *"),
        notification=JobNotification(channel="me"),
    )
    summary = JobRunSummary(
        job_id="j", job_name="Daily", run_id="r",
        started_at="2025-01-01T09:00:00+00:00",
        finished_at="2025-01-01T09:00:10+00:00",
        duration_s=10.5,
        status="failed",
        skills=[
            SkillRunSummary(
                slug="a", status="passed", duration_s=3.0,
                output_preview="hello world",
            ),
            SkillRunSummary(
                slug="b", status="failed", duration_s=7.5,
                error="boom because of X",
            ),
        ],
    )
    text = _build_notification_text(job, summary, include_output=True)
    assert "Daily" in text
    assert "FAILED" in text
    assert "10.5s" in text
    assert "✓ a" in text
    assert "✗ b" in text
    assert "boom" in text
    assert "Outputs:" in text
    assert "hello world" in text


def test_build_notification_text_exclude_output_skips_outputs_section() -> None:
    job = _job_with()
    summary = JobRunSummary(
        job_id="j", job_name="n", run_id="r", status="passed",
        skills=[
            SkillRunSummary(slug="a", status="passed", output_preview="hello"),
        ],
    )
    text = _build_notification_text(job, summary, include_output=False)
    assert "Outputs:" not in text
    assert "hello" not in text


def test_build_notification_text_failure_error_truncated_at_120() -> None:
    job = _job_with()
    long_error = "x" * 200
    summary = JobRunSummary(
        job_id="j", job_name="n", run_id="r", status="failed",
        skills=[SkillRunSummary(slug="a", status="failed", error=long_error)],
    )
    text = _build_notification_text(job, summary, include_output=True)
    assert "x" * 120 in text
    assert "x" * 121 not in text


def test_build_notification_text_unknown_status_uses_info_icon() -> None:
    job = _job_with()
    summary = JobRunSummary(
        job_id="j", job_name="n", run_id="r", status="running",
    )
    text = _build_notification_text(job, summary, include_output=True)
    assert "ℹ" in text


# ───────────────────────── run_job ────────────────────────────────

@pytest.mark.asyncio
async def test_run_job_missing_skill_fails_and_stops(runtime: Path) -> None:
    job = JobConfig(
        id="j1",
        name="missing-skill-test",
        skills=["does-not-exist", "also-not"],
        timing=JobTiming(mode="recurring", cron="* * * * *"),
        on_failure="stop",
    )

    with patch("openclose.jobs.runner.read_skill", return_value=None):
        summary = await run_job(job)

    assert summary.status == "failed"
    assert summary.skills[0].status == "failed"
    assert "not found" in summary.skills[0].error.lower()
    assert summary.skills[1].status == "skipped"


@pytest.mark.asyncio
async def test_run_job_passes_when_skills_succeed(runtime: Path) -> None:
    job = JobConfig(
        id="j2",
        name="ok-test",
        skills=["s1"],
        timing=JobTiming(mode="recurring", cron="* * * * *"),
    )
    fake_skill = Skill(
        name="s1", slug="s1", version=1,
        required_tools=[RequiredTool(name="read", sensitive=False)],
        goal="do thing", procedure="do it",
    )
    fake_execute = AsyncMock(return_value={
        "status": "done",
        "final_text": "ok",
        "error": "",
        "started_at": "",
        "finished_at": "2025-01-01T09:00:00+00:00",
        "output_preview": "ok",
    })

    with patch("openclose.jobs.runner.read_skill", return_value=fake_skill), \
         patch("openclose.jobs.runner.execute_skill_to_files", fake_execute):
        summary = await run_job(job)

    assert summary.status == "passed"
    assert summary.skills[0].status == "passed"
    assert summary.skills[0].output_preview == "ok"


@pytest.mark.asyncio
async def test_run_job_crashed_skill_stops(runtime: Path) -> None:
    job = JobConfig(
        id="j3",
        name="crash-test",
        skills=["s1", "s2"],
        timing=JobTiming(mode="recurring", cron="* * * * *"),
        on_failure="stop",
    )
    fake_skill = Skill(name="s", slug="s", version=1, goal="", procedure="")
    fake_execute = AsyncMock(side_effect=RuntimeError("kaboom"))

    with patch("openclose.jobs.runner.read_skill", return_value=fake_skill), \
         patch("openclose.jobs.runner.execute_skill_to_files", fake_execute):
        summary = await run_job(job)

    assert summary.status == "failed"
    assert summary.skills[0].status == "failed"
    assert "kaboom" in summary.skills[0].error
    assert summary.skills[1].status == "skipped"


@pytest.mark.asyncio
async def test_run_job_notification_triggered(runtime: Path) -> None:
    job = JobConfig(
        id="j4",
        name="notify-test",
        skills=["s1"],
        timing=JobTiming(mode="recurring", cron="* * * * *"),
        notification=JobNotification(channel="me", notify_on="always"),
    )
    fake_skill = Skill(name="s1", slug="s1", version=1, goal="", procedure="")
    fake_execute = AsyncMock(return_value={
        "status": "done", "final_text": "", "error": "",
        "started_at": "", "finished_at": "2025-01-01T09:00:00+00:00",
        "output_preview": "",
    })
    fake_send = AsyncMock(return_value=(True, ""))

    with patch("openclose.jobs.runner.read_skill", return_value=fake_skill), \
         patch("openclose.jobs.runner.execute_skill_to_files", fake_execute), \
         patch("openclose.jobs.runner.send_job_notification", fake_send):
        summary = await run_job(job)

    assert summary.notification_sent is True
    fake_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_job_notification_error_recorded(runtime: Path) -> None:
    job = JobConfig(
        id="j5",
        name="notify-fail-test",
        skills=["s1"],
        timing=JobTiming(mode="recurring", cron="* * * * *"),
        notification=JobNotification(channel="me", notify_on="always"),
    )
    fake_skill = Skill(name="s1", slug="s1", version=1, goal="", procedure="")
    fake_execute = AsyncMock(return_value={
        "status": "done", "final_text": "", "error": "",
        "started_at": "", "finished_at": "2025-01-01T09:00:00+00:00",
        "output_preview": "",
    })
    fake_send = AsyncMock(side_effect=RuntimeError("network down"))

    with patch("openclose.jobs.runner.read_skill", return_value=fake_skill), \
         patch("openclose.jobs.runner.execute_skill_to_files", fake_execute), \
         patch("openclose.jobs.runner.send_job_notification", fake_send):
        summary = await run_job(job)

    assert summary.notification_sent is False
    assert "network down" in summary.notification_error


@pytest.mark.asyncio
async def test_run_job_skill_returns_error_status(runtime: Path) -> None:
    """Execute returns status != 'done' → marked failed, no crash."""
    job = JobConfig(
        id="j6",
        name="err-status-test",
        skills=["s1"],
        timing=JobTiming(mode="recurring", cron="* * * * *"),
    )
    fake_skill = Skill(name="s1", slug="s1", version=1, goal="", procedure="")
    fake_execute = AsyncMock(return_value={
        "status": "error", "final_text": "",
        "error": "skill said nope",
        "started_at": "", "finished_at": "",
        "output_preview": "",
    })

    with patch("openclose.jobs.runner.read_skill", return_value=fake_skill), \
         patch("openclose.jobs.runner.execute_skill_to_files", fake_execute):
        summary = await run_job(job)

    assert summary.status == "failed"
    assert summary.skills[0].status == "failed"
    assert summary.skills[0].error == "skill said nope"
