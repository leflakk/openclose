"""Background task scheduling using asyncio."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Awaitable

from openclose.log import get_logger

log = get_logger(__name__)


@dataclass
class ScheduledTask:
    """A scheduled recurring task."""

    name: str
    interval_seconds: float
    callback: Callable[[], Awaitable[None]]
    _task: asyncio.Task[None] | None = None


class Scheduler:
    """Manages background scheduled tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}

    def add(
        self,
        name: str,
        interval_seconds: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """Register a scheduled task."""
        self._tasks[name] = ScheduledTask(
            name=name,
            interval_seconds=interval_seconds,
            callback=callback,
        )

    async def start(self) -> None:
        """Start all scheduled tasks."""
        for task in self._tasks.values():
            task._task = asyncio.create_task(self._run_loop(task))
            log.info(
                "Scheduled task '%s' every %.0fs", task.name, task.interval_seconds
            )

    async def _run_loop(self, task: ScheduledTask) -> None:
        """Run a task on its interval."""
        while True:
            try:
                await asyncio.sleep(task.interval_seconds)
                await task.callback()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Error in scheduled task '%s'", task.name)

    async def stop(self) -> None:
        """Stop all scheduled tasks."""
        for task in self._tasks.values():
            if task._task and not task._task.done():
                task._task.cancel()
                try:
                    await task._task
                except asyncio.CancelledError:
                    pass
        log.info("All scheduled tasks stopped")
