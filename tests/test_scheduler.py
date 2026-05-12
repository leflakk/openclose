"""Tests for the scheduler."""

from __future__ import annotations

import asyncio

import pytest

from openclose.scheduler.scheduler import Scheduler


@pytest.mark.asyncio
async def test_scheduler_runs_task() -> None:
    calls: list[int] = []

    async def my_task() -> None:
        calls.append(1)

    scheduler = Scheduler()
    scheduler.add("test", 0.05, my_task)
    await scheduler.start()
    await asyncio.sleep(0.15)
    await scheduler.stop()
    assert len(calls) >= 2


@pytest.mark.asyncio
async def test_scheduler_stop() -> None:
    async def noop() -> None:
        pass

    scheduler = Scheduler()
    scheduler.add("noop", 1.0, noop)
    await scheduler.start()
    await scheduler.stop()
    # Should not hang
