"""Tests for jobs.scheduler — tick loop, due logic, one-shot expiry, fire state."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from openclose.config.config import load_config
from openclose.config.paths import ConfigPaths
from openclose.jobs.scheduler import (
    JobScheduler,
    _parse_iso,
    _tz,
    get_scheduler,
)
from openclose.jobs.schema import JobConfig, JobNotification, JobTiming
from openclose.jobs.storage import read_job, write_job


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(
        ConfigPaths, "project_runtime_dir",
        classmethod(lambda cls, project_dir: tmp_path),
    )
    load_config(project_dir=tmp_path)
    yield tmp_path


def _recurring_job(job_id: str = "j1", cron: str = "0 9 * * *") -> JobConfig:
    return JobConfig(
        id=job_id,
        name=f"job-{job_id}",
        skills=["s"],
        timing=JobTiming(mode="recurring", cron=cron, timezone="UTC"),
        notification=JobNotification(),
    )


def _oneshot_job(job_id: str, run_at: str, executed: bool = False) -> JobConfig:
    return JobConfig(
        id=job_id,
        name=f"oneshot-{job_id}",
        skills=["s"],
        timing=JobTiming(mode="one_shot", run_at=run_at, executed=executed),
        notification=JobNotification(),
    )


# ───────────────────────── _tz / _parse_iso ──────────────────────

def test_tz_known() -> None:
    assert isinstance(_tz("Europe/Paris"), ZoneInfo)


def test_tz_unknown_falls_back_to_utc() -> None:
    tz = _tz("Not/Real")
    assert str(tz) == "UTC"


def test_parse_iso_valid_aware() -> None:
    tz = ZoneInfo("UTC")
    dt = _parse_iso("2025-06-15T10:00:00+00:00", tz)
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_naive_gets_tz_attached() -> None:
    tz = ZoneInfo("Europe/Paris")
    dt = _parse_iso("2025-06-15T10:00:00", tz)
    assert dt is not None
    assert dt.tzinfo == tz


def test_parse_iso_empty_returns_none() -> None:
    assert _parse_iso("", ZoneInfo("UTC")) is None


def test_parse_iso_invalid_returns_none() -> None:
    assert _parse_iso("not a date", ZoneInfo("UTC")) is None


# ───────────────────────── _due ──────────────────────────────────

def test_due_oneshot_past_fires(runtime: Path) -> None:
    past = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(minutes=5)).isoformat()
    job = _oneshot_job("j1", run_at=past)
    sched = JobScheduler()
    assert sched._due(job) is True


def test_due_oneshot_future_waits(runtime: Path) -> None:
    future = (datetime.now(tz=ZoneInfo("UTC")) + timedelta(hours=1)).isoformat()
    job = _oneshot_job("j1", run_at=future)
    sched = JobScheduler()
    assert sched._due(job) is False


def test_due_oneshot_already_executed_false(runtime: Path) -> None:
    past = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(minutes=5)).isoformat()
    job = _oneshot_job("j1", run_at=past, executed=True)
    sched = JobScheduler()
    assert sched._due(job) is False


def test_due_oneshot_bad_run_at_false(runtime: Path) -> None:
    job = _oneshot_job("j1", run_at="garbage")
    sched = JobScheduler()
    assert sched._due(job) is False


def test_due_recurring_empty_cron_false(runtime: Path) -> None:
    job = JobConfig(
        id="j1", name="n", skills=[],
        timing=JobTiming(mode="recurring", cron=""),
    )
    sched = JobScheduler()
    assert sched._due(job) is False


def test_due_recurring_first_tick_primes_cache(runtime: Path) -> None:
    """First _due call for a recurring job should not fire — just prime next_fire."""
    job = _recurring_job("j1", cron="0 9 * * *")
    sched = JobScheduler()
    assert sched._due(job) is False
    assert "j1" in sched._next_fire


def test_due_recurring_fires_when_cached_past(runtime: Path) -> None:
    job = _recurring_job("j1")
    sched = JobScheduler()
    # Directly seed the cache with a past time.
    sched._next_fire["j1"] = datetime.now(tz=ZoneInfo("UTC")) - timedelta(minutes=1)
    assert sched._due(job) is True


def test_due_recurring_invalid_cron_false(runtime: Path) -> None:
    """Malformed cron should log a warning and return False, not crash."""
    job = _recurring_job("j1", cron="bogus")
    sched = JobScheduler()
    assert sched._due(job) is False


# ───────────────────────── _expire_past_due_oneshots ─────────────

def test_expire_past_due_oneshot(runtime: Path) -> None:
    past = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
    job = _oneshot_job("expired", run_at=past)
    write_job(job)
    sched = JobScheduler()
    sched._expire_past_due_oneshots()
    refreshed = read_job("expired")
    assert refreshed is not None
    assert refreshed.timing.executed is True


def test_expire_ignores_future_oneshot(runtime: Path) -> None:
    future = (datetime.now(tz=ZoneInfo("UTC")) + timedelta(days=1)).isoformat()
    job = _oneshot_job("future", run_at=future)
    write_job(job)
    sched = JobScheduler()
    sched._expire_past_due_oneshots()
    refreshed = read_job("future")
    assert refreshed is not None
    assert refreshed.timing.executed is False


def test_expire_ignores_recurring(runtime: Path) -> None:
    write_job(_recurring_job("recurring"))
    sched = JobScheduler()
    sched._expire_past_due_oneshots()
    # Should not crash and should leave the job untouched.
    assert read_job("recurring") is not None


def test_expire_ignores_already_executed(runtime: Path) -> None:
    past = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
    job = _oneshot_job("done", run_at=past, executed=True)
    write_job(job)
    # Sentinel: existing executed value should stay True without re-write.
    sched = JobScheduler()
    sched._expire_past_due_oneshots()
    assert read_job("done").timing.executed is True  # type: ignore[union-attr]


def test_expire_ignores_bad_run_at(runtime: Path) -> None:
    job = _oneshot_job("bad", run_at="not-a-date")
    write_job(job)
    sched = JobScheduler()
    sched._expire_past_due_oneshots()
    assert read_job("bad").timing.executed is False  # type: ignore[union-attr]


# ───────────────────────── _advance ──────────────────────────────

def test_advance_oneshot_marks_executed(runtime: Path) -> None:
    future = (datetime.now(tz=ZoneInfo("UTC")) + timedelta(hours=1)).isoformat()
    job = _oneshot_job("j1", run_at=future)
    write_job(job)
    sched = JobScheduler()
    sched._advance(job)
    refreshed = read_job("j1")
    assert refreshed is not None
    assert refreshed.timing.executed is True


def test_advance_recurring_computes_next_fire(runtime: Path) -> None:
    job = _recurring_job("j1", cron="0 * * * *")
    sched = JobScheduler()
    sched._advance(job)
    assert "j1" in sched._next_fire
    assert sched._next_fire["j1"] > datetime.now(tz=ZoneInfo("UTC"))


def test_advance_recurring_bad_cron_clears_cache(runtime: Path) -> None:
    job = _recurring_job("j1", cron="nonsense")
    sched = JobScheduler()
    sched._next_fire["j1"] = datetime.now(tz=ZoneInfo("UTC"))
    sched._advance(job)
    assert "j1" not in sched._next_fire


# ───────────────────────── invalidate / running ──────────────────

def test_invalidate_clears_state(runtime: Path) -> None:
    sched = JobScheduler()
    sched._next_fire["j1"] = datetime.now(tz=ZoneInfo("UTC"))
    sched._locks["j1"] = asyncio.Lock()
    sched.invalidate("j1")
    assert "j1" not in sched._next_fire
    assert "j1" not in sched._locks


def test_is_running_false_without_lock(runtime: Path) -> None:
    sched = JobScheduler()
    assert sched._is_running("j1") is False


@pytest.mark.asyncio
async def test_is_running_true_when_locked(runtime: Path) -> None:
    sched = JobScheduler()
    lock = asyncio.Lock()
    sched._locks["j1"] = lock
    await lock.acquire()
    try:
        assert sched._is_running("j1") is True
    finally:
        lock.release()


# ───────────────────────── trigger_now ───────────────────────────

@pytest.mark.asyncio
async def test_trigger_now_missing_job(runtime: Path) -> None:
    sched = JobScheduler()
    result = await sched.trigger_now("ghost")
    assert result["ok"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_trigger_now_rejects_if_running(runtime: Path) -> None:
    write_job(_recurring_job("j1"))
    sched = JobScheduler()
    lock = asyncio.Lock()
    sched._locks["j1"] = lock
    await lock.acquire()
    try:
        result = await sched.trigger_now("j1")
    finally:
        lock.release()
    assert result["ok"] is False
    assert "already" in result["error"]


@pytest.mark.asyncio
async def test_trigger_now_fires(runtime: Path) -> None:
    write_job(_recurring_job("j1"))
    sched = JobScheduler()
    fake_run = AsyncMock()

    with patch("openclose.jobs.scheduler.run_job", fake_run):
        result = await sched.trigger_now("j1")
        # Let the created task run.
        await asyncio.sleep(0)
        # Wait for the fire task to complete
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending, return_exceptions=True)

    assert result["ok"] is True
    assert result["job_id"] == "j1"
    fake_run.assert_awaited_once()


# ───────────────────────── _fire ─────────────────────────────────

@pytest.mark.asyncio
async def test_fire_swallows_exceptions(runtime: Path) -> None:
    job = _recurring_job("j1")
    write_job(job)
    sched = JobScheduler()
    fake_run = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("openclose.jobs.scheduler.run_job", fake_run):
        # Should not raise
        await sched._fire(job)
    # Recurring job → next_fire should be recomputed in _advance
    assert "j1" in sched._next_fire


@pytest.mark.asyncio
async def test_fire_skips_if_locked(runtime: Path) -> None:
    job = _recurring_job("j1")
    sched = JobScheduler()
    lock = asyncio.Lock()
    sched._locks["j1"] = lock
    await lock.acquire()
    try:
        fake_run = AsyncMock()
        with patch("openclose.jobs.scheduler.run_job", fake_run):
            await sched._fire(job)
        fake_run.assert_not_awaited()
    finally:
        lock.release()


# ───────────────────────── lifecycle ─────────────────────────────

@pytest.mark.asyncio
async def test_start_stop_idempotent(runtime: Path) -> None:
    sched = JobScheduler()
    await sched.start()
    # Calling start twice should not spawn two tasks.
    await sched.start()
    await sched.stop()
    # Stop when not running should be a no-op.
    await sched.stop()


@pytest.mark.asyncio
async def test_tick_once_fires_due_jobs(runtime: Path) -> None:
    past = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(minutes=5)).isoformat()
    job = _oneshot_job("due", run_at=past)
    write_job(job)

    sched = JobScheduler()
    fake_run = AsyncMock()
    with patch("openclose.jobs.scheduler.run_job", fake_run):
        await sched._tick_once()
        # Let the fire task run.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending, return_exceptions=True)

    fake_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_tick_once_skips_disabled(runtime: Path) -> None:
    past = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(minutes=5)).isoformat()
    job = _oneshot_job("disabled", run_at=past)
    job.enabled = False
    write_job(job)

    sched = JobScheduler()
    fake_run = AsyncMock()
    with patch("openclose.jobs.scheduler.run_job", fake_run):
        await sched._tick_once()

    fake_run.assert_not_awaited()


# ───────────────────────── singleton ─────────────────────────────

def test_get_scheduler_is_singleton() -> None:
    import openclose.jobs.scheduler as mod
    # Clear any prior state
    mod._scheduler = None
    a = get_scheduler()
    b = get_scheduler()
    assert a is b
