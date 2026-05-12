"""Background tick loop that fires cron / one-shot jobs.

Design choices (per user):
- **Missed cron fires**: skipped. Next fire is computed strictly forward from now.
- **One-shot past due on startup**: marked `executed=true` without running.
- **Overlap**: per-job `asyncio.Lock`; if a run is in progress when a fire
  comes, the fire is dropped with a warning.

The scheduler reads jobs from disk on every tick — no in-memory cache of
configs. That keeps the "edit a job → scheduler picks it up" story simple
and correct without explicit reload calls, at the cost of re-parsing a
few JSON files every N seconds. For realistic job counts this is fine.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from openclose.jobs.runner import run_job
from openclose.jobs.schema import JobConfig
from openclose.jobs.storage import list_jobs, read_job, write_job
from openclose.log import get_logger

log = get_logger(__name__)

# Check every 20s. Minute-granular cron means we never miss a fire window
# by more than `_TICK_INTERVAL`s, which is acceptable for non-second cron.
_TICK_INTERVAL_S = 20.0


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _now_in(tz: ZoneInfo) -> datetime:
    return datetime.now(tz=tz)


def _parse_iso(s: str, tz: ZoneInfo) -> datetime | None:
    """Parse an ISO-ish string, attaching tz if naive."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


class JobScheduler:
    """Owns a single background task that checks job fire times on interval."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._locks: dict[str, asyncio.Lock] = {}
        # Per-job next fire time (recurring only). Computed lazily on first tick.
        self._next_fire: dict[str, datetime] = {}

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        # Startup sweep: expire past-due one-shots.
        self._expire_past_due_oneshots()
        self._task = asyncio.create_task(self._tick_forever())
        log.info("JobScheduler started (tick=%.1fs)", _TICK_INTERVAL_S)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None
        log.info("JobScheduler stopped")

    # ── startup recovery ─────────────────────────────────────────────

    def _expire_past_due_oneshots(self) -> None:
        """Mark `executed=true` on one-shot jobs whose run_at has passed.

        Per user choice: don't run them late. Just disarm.
        """
        for job in list_jobs():
            if job.timing.mode != "one_shot":
                continue
            if job.timing.executed:
                continue
            tz = _tz(job.timing.timezone)
            run_at = _parse_iso(job.timing.run_at, tz)
            if run_at is None:
                continue
            if run_at < _now_in(tz):
                job.timing.executed = True
                write_job(job)
                log.info("One-shot job %s (%s) was past due on startup; marked expired",
                         job.id, job.name)

    # ── tick loop ────────────────────────────────────────────────────

    async def _tick_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._tick_once()
            except Exception:  # noqa: BLE001
                log.exception("JobScheduler tick crashed; continuing")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=_TICK_INTERVAL_S,
                )
                return  # stop requested
            except asyncio.TimeoutError:
                continue

    async def _tick_once(self) -> None:
        for job in list_jobs():
            if not job.enabled:
                continue
            if self._is_running(job.id):
                continue
            if not self._due(job):
                continue

            lock = self._locks.setdefault(job.id, asyncio.Lock())
            if lock.locked():
                log.warning("Job %s fire skipped: previous run still holds lock", job.id)
                continue

            asyncio.create_task(self._fire(job))

    def _is_running(self, job_id: str) -> bool:
        lock = self._locks.get(job_id)
        return lock is not None and lock.locked()

    def _due(self, job: JobConfig) -> bool:
        """Is the job's next fire time at or before `now`?"""
        tz = _tz(job.timing.timezone)
        now = _now_in(tz)

        if job.timing.mode == "one_shot":
            if job.timing.executed:
                return False
            run_at = _parse_iso(job.timing.run_at, tz)
            if run_at is None:
                return False
            return now >= run_at

        # recurring
        cron_expr = job.timing.cron.strip()
        if not cron_expr:
            return False
        cached = self._next_fire.get(job.id)
        if cached is None:
            # Compute next fire strictly after `now` — implements "skip missed".
            try:
                it = croniter(cron_expr, now)
                cached = it.get_next(datetime)
            except Exception:  # noqa: BLE001
                log.warning("Job %s has invalid cron %r; skipping", job.id, cron_expr)
                return False
            self._next_fire[job.id] = cached
            return False  # don't fire the newly-computed slot until it elapses

        return now >= cached

    async def _fire(self, job: JobConfig) -> None:
        """Acquire the per-job lock and run; advance state on completion."""
        lock = self._locks.setdefault(job.id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            try:
                log.info("JobScheduler firing job %s (%s)", job.id, job.name)
                await run_job(job)
            except Exception:  # noqa: BLE001
                log.exception("Job %s fire crashed", job.id)
            finally:
                # Advance state: one-shot → disarm; recurring → compute next.
                self._advance(job)

    def _advance(self, job: JobConfig) -> None:
        if job.timing.mode == "one_shot":
            fresh = read_job(job.id)
            if fresh is not None:
                fresh.timing.executed = True
                write_job(fresh)
            return
        # Recurring: compute next fire strictly after now.
        tz = _tz(job.timing.timezone)
        try:
            it = croniter(job.timing.cron.strip(), _now_in(tz))
            self._next_fire[job.id] = it.get_next(datetime)
        except Exception:  # noqa: BLE001
            self._next_fire.pop(job.id, None)

    # ── external triggers ────────────────────────────────────────────

    async def trigger_now(self, job_id: str) -> dict[str, Any]:
        """Run a job on demand (sidebar "Run now"). Queued if locked."""
        job = read_job(job_id)
        if job is None:
            return {"ok": False, "error": "job not found"}
        if self._is_running(job_id):
            return {"ok": False, "error": "a run is already in progress"}
        asyncio.create_task(self._fire(job))
        return {"ok": True, "job_id": job_id}

    def invalidate(self, job_id: str) -> None:
        """Drop cached state for `job_id` (call after edit/delete)."""
        self._next_fire.pop(job_id, None)
        self._locks.pop(job_id, None)


# Module-level singleton reused from lifespan + request handlers.
_scheduler: JobScheduler | None = None


def get_scheduler() -> JobScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = JobScheduler()
    return _scheduler
