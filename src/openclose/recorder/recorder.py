"""RecorderSession orchestration — start/stop CDP capture + annotate to task."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openclose.log import get_logger
from openclose.recorder.chunk_annotator import annotate_chunk
from openclose.recorder.chunker import plan_chunks, slice_chunk
from openclose.recorder.events import EventLog
from openclose.recorder.merger import merge_chunk_procedures
from openclose.recorder.screencast import (
    RecorderEncodeError,
    Screencast,
    encode_frames_to_mp4,
)
from openclose.recorder.task_builder import (
    TaskBuilderError,
    build_intelligent_task,
)
from openclose.recorder.storage import (
    Task,
    chunks_dir,
    recording_dir,
    rename_recording_artifacts,
    reserve_task_slug,
    write_chunk_events,
    write_chunk_procedure,
    write_recording_procedure,
    write_task,
)
from openclose.tool.tools.browser_automation_shared import (
    BROWSER_AUTOMATION_LOCK,
    connect_browser,
)

# Chunked annotation tunables.
_CHUNK_WINDOW_S = 12.0
_CHUNK_OVERLAP_S = 2.0
_VLM_CONCURRENCY = 4

log = get_logger(__name__)


class RecorderError(RuntimeError):
    """Raised by recorder API when a state transition is invalid."""


@dataclass
class _Session:
    id: str
    started_at: float  # = screencast.started_at (global clock reference)
    events_started_at: float
    pw: Any
    browser: Any
    context: Any
    page: Any
    cdp: Any
    screencast: Screencast
    events: EventLog
    lock_holder: asyncio.Task[None]
    lock_release: asyncio.Event
    video_path: Path | None = None
    events_path: Path | None = None
    raw_events: list[dict[str, Any]] = field(default_factory=list)


_active: _Session | None = None
_state_lock = asyncio.Lock()


async def _hold_browser_lock(release_evt: asyncio.Event) -> None:
    """Background task: hold BROWSER_AUTOMATION_LOCK until release_evt is set."""
    async with BROWSER_AUTOMATION_LOCK:
        await release_evt.wait()


def get_active_recording() -> dict[str, Any] | None:
    if _active is None:
        return None
    return {
        "recording_id": _active.id,
        "started_at": _active.started_at,
        "events_count": len(_active.events.events),
        "frames_count": len(_active.screencast.frames),
    }


async def start_recording() -> dict[str, Any]:
    """Begin a recording on the CDP-attached browser."""
    global _active
    async with _state_lock:
        if _active is not None:
            raise RecorderError("a recording is already in progress")

        # Hold the browser-automation lock for the whole recording so
        # the agent's browser tools can't run concurrently.
        release_evt = asyncio.Event()
        lock_holder = asyncio.create_task(_hold_browser_lock(release_evt))
        # Wait until the lock is actually held — give it a moment.
        # (We don't have an "acquired" signal; if another browser tool is
        # running, the user just has to wait or stop it.)
        await asyncio.sleep(0.05)

        try:
            pw, browser, context, page = await connect_browser()
        except Exception as e:
            release_evt.set()
            raise RecorderError(f"failed to attach to browser: {e}") from e

        try:
            cdp = await context.new_cdp_session(page)
        except Exception as e:
            release_evt.set()
            await pw.stop()
            raise RecorderError(f"failed to open CDP session: {e}") from e

        screencast = Screencast(cdp=cdp)
        events = EventLog(page=page, cdp=cdp)

        try:
            await events.start()
            await screencast.start()
        except Exception as e:
            release_evt.set()
            try:
                await pw.stop()
            except Exception:
                pass
            raise RecorderError(f"failed to start capture: {e}") from e

        rec_id = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        _active = _Session(
            id=rec_id,
            started_at=screencast.started_at,
            events_started_at=events.started_at,
            pw=pw,
            browser=browser,
            context=context,
            page=page,
            cdp=cdp,
            screencast=screencast,
            events=events,
            lock_holder=lock_holder,
            lock_release=release_evt,
        )
        log.info("recorder: started %s", rec_id)
        return {"recording_id": rec_id}


async def stop_recording() -> dict[str, Any]:
    """Stop capture, encode mp4, keep recording in memory until annotate is called."""
    global _active
    async with _state_lock:
        if _active is None:
            raise RecorderError("no recording in progress")
        sess = _active

        try:
            await sess.events.stop()
            await sess.screencast.stop()
        finally:
            # Detach Playwright but keep the lock held (released after annotate
            # or on cancel). Actually release here — annotation uses provider,
            # not the browser lock. Otherwise a long VLM call would hold the
            # browser hostage.
            try:
                await sess.pw.stop()
            except Exception:
                pass
            sess.lock_release.set()
            try:
                await asyncio.wait_for(sess.lock_holder, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass

        # Persist artifacts in the project runtime dir so the user can
        # inspect them afterwards (the UI also surfaces the path).
        out_dir = recording_dir(sess.id)
        sess.video_path = out_dir / f"{sess.id}.mp4"
        sess.events_path = out_dir / f"{sess.id}.events.json"

        duration_s = (
            sess.screencast.frames[-1].monotonic_ts
            if sess.screencast.frames else 0.0
        )
        log.info(
            "recorder: encoding %s — %d captured frames over %.2fs",
            sess.id, len(sess.screencast.frames), duration_s,
        )

        try:
            await sess.screencast.encode_mp4(sess.video_path)
        except RecorderEncodeError as e:
            _active = None
            raise RecorderError(str(e)) from e

        sess.raw_events = list(sess.events.events)
        sess.events_path.write_text(
            json.dumps(sess.raw_events, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        try:
            video_size = sess.video_path.stat().st_size
        except OSError:
            video_size = 0
        log.info(
            "recorder: stopped %s — %d captured frames, %.2fs, "
            "%d events, video %s (%.1f MiB)",
            sess.id, len(sess.screencast.frames), duration_s,
            len(sess.raw_events), sess.video_path,
            video_size / (1024 * 1024),
        )
        return {
            "recording_id": sess.id,
            "events_count": len(sess.raw_events),
            "frames_count": len(sess.screencast.frames),
            "duration_s": round(duration_s, 2),
            "video_path": str(sess.video_path),
            "events_path": str(sess.events_path),
            "video_size_bytes": video_size,
        }


def _fallback_body(raw_procedure: str, description: str) -> str:
    """Build a degraded-but-usable task body when the second LLM pass fails."""
    goal = (description or "").strip() or "Reproduce the recorded procedure."
    return (
        f"## Task goal\n{goal}\n\n"
        f"## Task workflow\n{raw_procedure.strip()}\n"
    )


def _events_with_global_time(
    events: list[dict[str, Any]], clock_offset: float,
) -> list[dict[str, Any]]:
    """Translate event-local `t` to the screencast clock via `clock_offset`.

    `clock_offset = events.started_at - screencast.started_at`. The returned
    events are copies with a `t_global` key injected.
    """
    out: list[dict[str, Any]] = []
    for ev in events:
        new_ev = dict(ev)
        new_ev["t_global"] = round(ev.get("t", 0.0) - clock_offset, 3)
        out.append(new_ev)
    return out


async def _annotate_chunked(
    sess: _Session,
    windows: list[tuple[float, float]],
    clock_offset: float,
    duration: float,
) -> str:
    """Slice → encode → parallel-VLM → merge. Returns the merged procedure."""
    total = len(windows)
    chunks = [
        slice_chunk(
            sess.screencast.frames, sess.raw_events,
            index=i, t_start=t0, t_end=t1, clock_offset=clock_offset,
        )
        for i, (t0, t1) in enumerate(windows)
    ]

    chunk_dir = chunks_dir(sess.id)
    chunk_paths: list[Path] = []
    for chunk in chunks:
        p = chunk_dir / f"{chunk.index:03d}.mp4"
        if not chunk.frames:
            raise RecorderError(
                f"chunk {chunk.index} [{chunk.t_start:.1f}s-{chunk.t_end:.1f}s] "
                f"has no frames — cannot encode"
            )
        await encode_frames_to_mp4(
            chunk.frames, p, t0=chunk.t_start, t1=chunk.t_end,
        )
        write_chunk_events(sess.id, chunk.index, chunk.events)
        chunk_paths.append(p)
    log.info("recorder: encoded %d chunk mp4s under %s", total, chunk_dir)

    sem = asyncio.Semaphore(_VLM_CONCURRENCY)

    async def bounded(chunk: Any, path: Path) -> tuple[int, str]:
        async with sem:
            return await annotate_chunk(chunk, path, total)

    results = await asyncio.gather(
        *(bounded(c, p) for c, p in zip(chunks, chunk_paths))
    )
    # gather preserves input order, so (chunks, results) stay aligned.

    chunk_procedures: list[tuple[float, float, str]] = []
    for chunk, (_idx, text) in zip(chunks, results):
        write_chunk_procedure(sess.id, chunk.index, text)
        chunk_procedures.append((chunk.t_start, chunk.t_end, text))

    global_events = _events_with_global_time(sess.raw_events, clock_offset)
    return await merge_chunk_procedures(chunk_procedures, global_events, duration)


async def annotate_recording(recording_id: str, name: str, description: str) -> Task:
    """Run the VLM annotator + intelligent task builder, save both artifacts.

    The video is split into overlapping windows (one window if duration is
    below `_CHUNK_WINDOW_S`), each annotated by the VLM in parallel, then
    merged by a text-only LLM pass into a single procedure. The raw
    (merged) procedure is persisted under recordings/artifacts/ alongside
    the mp4 and events.json. The intelligent task lands in recordings/.
    """
    global _active
    if _active is None or _active.id != recording_id:
        raise RecorderError("recording not found (stop and annotate must be sequential)")
    if _active.video_path is None:
        raise RecorderError("recording not yet stopped")
    sess = _active
    video_path = sess.video_path
    assert video_path is not None

    duration = (
        sess.screencast.frames[-1].monotonic_ts
        if sess.screencast.frames else 0.0
    )
    clock_offset = sess.events_started_at - sess.started_at
    windows = plan_chunks(duration, _CHUNK_WINDOW_S, _CHUNK_OVERLAP_S)
    if not windows:
        raise RecorderError("recording has no frames to annotate")

    log.info(
        "recorder: annotation — %d window(s) "
        "(window=%.1fs, overlap=%.1fs, duration=%.2fs)",
        len(windows), _CHUNK_WINDOW_S, _CHUNK_OVERLAP_S, duration,
    )
    try:
        raw_procedure = await _annotate_chunked(
            sess, windows, clock_offset, duration,
        )
    except Exception as e:
        raise RecorderError(f"annotation failed: {e}") from e

    procedure_path = write_recording_procedure(sess.id, raw_procedure)
    log.info("recorder: raw procedure saved to %s", procedure_path)

    recorded_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    try:
        body = await build_intelligent_task(
            recording_id=sess.id,
            name=name or "Untitled task",
            description=description or "",
            recorded_at=recorded_at,
            raw_procedure=raw_procedure,
            events=sess.raw_events,
        )
    except TaskBuilderError as e:
        log.warning(
            "recorder: task_builder failed (%s); writing raw-procedure fallback", e,
        )
        body = _fallback_body(raw_procedure, description)

    # Prefix all artifacts with the task slug so related files cluster
    # together on disk: `<slug>_<date_time>.{mp4,events.json,procedure.md,
    # task_builder_raw.md}` and the `<slug>_<date_time>/chunks/` subdir.
    task_name = name or "Untitled task"
    slug = reserve_task_slug(task_name)
    new_rec_id = f"{slug}_{sess.id}"
    rename_recording_artifacts(sess.id, new_rec_id)
    new_rec_dir = recording_dir(new_rec_id)
    renamed_video = new_rec_dir / f"{new_rec_id}.mp4"
    renamed_events = new_rec_dir / f"{new_rec_id}.events.json"
    renamed_procedure = new_rec_dir / f"{new_rec_id}.procedure.md"

    task = write_task(
        name=task_name,
        description=description or "",
        body=body,
        recorded_at=recorded_at,
        recording_id=new_rec_id,
        slug=slug,
    )

    _active = None
    log.info(
        "recorder: saved task %s (%s); artifacts kept at %s, %s, %s",
        task.slug, task.path, renamed_video, renamed_events, renamed_procedure,
    )
    return task


async def cancel_recording() -> None:
    """Discard the active recording without saving."""
    global _active
    async with _state_lock:
        if _active is None:
            return
        sess = _active
        try:
            await sess.events.stop()
            await sess.screencast.stop()
        except Exception:
            pass
        try:
            await sess.pw.stop()
        except Exception:
            pass
        sess.lock_release.set()
        try:
            await asyncio.wait_for(sess.lock_holder, timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass
        _active = None
