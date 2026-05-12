"""File watching using watchfiles."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Awaitable

from openclose.log import get_logger

log = get_logger(__name__)

ChangeCallback = Callable[..., Awaitable[None]]


class FileWatcher:
    """Watches a directory for file changes."""

    def __init__(self, root: Path, callback: ChangeCallback) -> None:
        self._root = root
        self._callback = callback
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start watching for changes."""
        try:
            from watchfiles import awatch
        except ImportError:
            log.warning("watchfiles not installed; file watcher disabled")
            return

        self._task = asyncio.create_task(self._watch(awatch))

    async def _watch(self, awatch: Callable[..., Any]) -> None:
        """Internal watch loop. ``awatch`` is injected by ``start()`` so tests
        can substitute a fake async iterator."""
        try:
            async for changes in awatch(str(self._root)):
                try:
                    await self._callback(changes)
                except Exception:
                    log.exception("Error in file watcher callback")
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("File watcher error")

    async def stop(self) -> None:
        """Stop watching."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
