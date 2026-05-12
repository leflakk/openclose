"""Async subprocess helpers."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any


def find_bash() -> str | None:
    """Locate a bash executable.

    Git for Windows ships bash but the installer adds only git.exe to PATH,
    so `shutil.which("bash")` misses it. Probe the standard Git Bash install
    locations (system-wide and per-user) before giving up.
    """
    found = shutil.which("bash")
    if found:
        return found
    if sys.platform != "win32":
        return None
    env = os.environ
    candidates: list[str] = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        root = env.get(var)
        if root:
            candidates.append(os.path.join(root, "Git", "bin", "bash.exe"))
            candidates.append(os.path.join(root, "Git", "usr", "bin", "bash.exe"))
    local = env.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "Programs", "Git", "bin", "bash.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


@dataclass
class ProcessResult:
    """Result of a subprocess execution."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate the process and its group (best-effort, cross-platform)."""
    if sys.platform == "win32":
        # Windows: signal the new process group, then hard-kill if it lingers.
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (ProcessLookupError, OSError, ValueError):
            pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def run(
    *args: str,
    cwd: str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    """Run a subprocess asynchronously and return the result.

    Spawns the child in its own process group (``start_new_session`` on POSIX,
    ``CREATE_NEW_PROCESS_GROUP`` on Windows) so background children
    (e.g. ``cmd &``) can be reaped together when the main shell exits.

    On timeout, partial stdout/stderr captured before the kill are preserved.
    """
    extra_kwargs: dict[str, Any]
    if sys.platform == "win32":
        extra_kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        extra_kwargs = {"start_new_session": True}
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        **extra_kwargs,
    )

    # Read streams via tasks so we can collect partial output on timeout.
    assert proc.stdout is not None and proc.stderr is not None
    stdout_task = asyncio.create_task(proc.stdout.read())
    stderr_task = asyncio.create_task(proc.stderr.read())
    wait_task = asyncio.create_task(proc.wait())

    start = time.monotonic()

    done, pending = await asyncio.wait(
        {stdout_task, stderr_task, wait_task},
        timeout=timeout,
    )

    elapsed = time.monotonic() - start

    if wait_task not in done:
        # Timeout — kill process, then drain whatever was buffered.
        _kill_process_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

        # Collect partial output (short drain timeout).
        partial_stdout = b""
        partial_stderr = b""
        if stdout_task.done():
            partial_stdout = stdout_task.result()
        else:
            stdout_task.cancel()
            try:
                partial_stdout = await asyncio.wait_for(
                    proc.stdout.read(), timeout=1.0,
                )
            except (asyncio.TimeoutError, Exception):
                pass
        if stderr_task.done():
            partial_stderr = stderr_task.result()
        else:
            stderr_task.cancel()
            try:
                partial_stderr = await asyncio.wait_for(
                    proc.stderr.read(), timeout=1.0,
                )
            except (asyncio.TimeoutError, Exception):
                pass

        stderr_text = partial_stderr.decode("utf-8", errors="replace")
        if stderr_text:
            stderr_text += "\n"
        stderr_text += f"Process timed out after {timeout}s"

        return ProcessResult(
            returncode=-1,
            stdout=partial_stdout.decode("utf-8", errors="replace"),
            stderr=stderr_text,
            timed_out=True,
            duration=elapsed,
        )

    # Normal completion — cancel any still-pending stream reads and collect.
    for task in pending:
        task.cancel()

    # Ensure stream tasks have finished.
    await asyncio.wait({stdout_task, stderr_task}, timeout=5.0)

    # If stream tasks were still pending (background children holding pipes),
    # kill the process group to clean up orphans.
    if pending:
        _kill_process_group(proc)

    stdout_bytes = stdout_task.result() if stdout_task.done() and not stdout_task.cancelled() else b""
    stderr_bytes = stderr_task.result() if stderr_task.done() and not stderr_task.cancelled() else b""

    return ProcessResult(
        returncode=proc.returncode or 0,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        duration=elapsed,
    )
