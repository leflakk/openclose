"""Plan time-windowed chunks over a recording and slice frames + events per chunk.

The recording is split into overlapping windows so each window can be sent to
the VLM in parallel. Windows are expressed in the screencast (global) clock.
Events — which use their own monotonic clock started slightly earlier — are
translated via `clock_offset = events.started_at - screencast.started_at`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openclose.recorder.screencast import _Frame


@dataclass
class Chunk:
    index: int
    t_start: float           # global screencast time, seconds
    t_end: float
    frames: list[_Frame] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


def plan_chunks(
    total_duration: float,
    window_s: float = 12.0,
    overlap_s: float = 2.0,
) -> list[tuple[float, float]]:
    """Plan overlapping windows covering [0, total_duration].

    Each window is exactly `window_s` long except when the recording is
    shorter than one window. Stride is `window_s - overlap_s`. The final
    window is always anchored so it ends at `total_duration` — this can
    introduce extra overlap with the previous window but guarantees no
    frames are dropped off the end.
    """
    if total_duration <= 0:
        return []
    if total_duration <= window_s:
        return [(0.0, round(float(total_duration), 3))]
    if overlap_s >= window_s:
        raise ValueError("overlap_s must be strictly less than window_s")

    stride = window_s - overlap_s
    windows: list[tuple[float, float]] = []
    t = 0.0
    while t + window_s < total_duration:
        windows.append((round(t, 3), round(t + window_s, 3)))
        t += stride
    last_start = round(max(0.0, total_duration - window_s), 3)
    last_end = round(float(total_duration), 3)
    if not windows or windows[-1] != (last_start, last_end):
        windows.append((last_start, last_end))
    return windows


def slice_chunk(
    frames: list[_Frame],
    events: list[dict[str, Any]],
    index: int,
    t_start: float,
    t_end: float,
    clock_offset: float,
) -> Chunk:
    """Select frames + events falling inside [t_start, t_end].

    Events' local `t` is translated to the screencast clock via
    `t_global = t - clock_offset` (events' clock started `clock_offset`
    seconds before the screencast's). Each kept event is copied and
    augmented with a `t_global` field (3-decimal seconds).
    """
    chunk_frames = [f for f in frames if t_start <= f.monotonic_ts <= t_end]
    chunk_events: list[dict[str, Any]] = []
    for ev in events:
        local_t = ev.get("t", 0.0)
        t_global = round(local_t - clock_offset, 3)
        if t_start <= t_global <= t_end:
            new_ev = dict(ev)
            new_ev["t_global"] = t_global
            chunk_events.append(new_ev)
    return Chunk(
        index=index,
        t_start=t_start,
        t_end=t_end,
        frames=chunk_frames,
        events=chunk_events,
    )
