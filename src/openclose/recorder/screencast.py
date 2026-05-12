"""CDP screencast capture → JPEG frames → mp4 via ffmpeg."""

from __future__ import annotations

import asyncio
import base64
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openclose.log import get_logger
from openclose.util.process import run as run_process

log = get_logger(__name__)

# Encode at the same rate the VLM will sample at, so we can disable
# server-side frame sampling (which has known bugs in Qwen3-VL's
# transformers processor) and let vLLM use every frame as-is.
_OUTPUT_FPS = 3


@dataclass
class _Frame:
    monotonic_ts: float  # seconds since recording start
    data: bytes


@dataclass
class Screencast:
    """Receives Page.screencastFrame events from a CDP session."""

    cdp: Any  # playwright CDPSession
    started_at: float = 0.0
    frames: list[_Frame] = field(default_factory=list)
    _running: bool = False
    _handler: Any = None

    async def start(self) -> None:
        self.started_at = time.monotonic()
        self._running = True

        def on_frame(payload: dict[str, Any]) -> None:
            if not self._running:
                return
            try:
                data = base64.b64decode(payload["data"])
                self.frames.append(_Frame(
                    monotonic_ts=time.monotonic() - self.started_at,
                    data=data,
                ))
            except Exception as e:  # noqa: BLE001
                log.warning("screencast frame decode failed: %s", e)
            # Always ack so the next frame is sent.
            session_id = payload.get("sessionId")
            asyncio.create_task(self._ack(session_id))

        self._handler = on_frame
        self.cdp.on("Page.screencastFrame", on_frame)
        await self.cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 70,
            "everyNthFrame": 1,
        })

    async def _ack(self, session_id: int | None) -> None:
        try:
            params: dict[str, Any] = {}
            if session_id is not None:
                params["sessionId"] = session_id
            await self.cdp.send("Page.screencastFrameAck", params)
        except Exception:
            pass

    async def stop(self) -> None:
        self._running = False
        try:
            await self.cdp.send("Page.stopScreencast")
        except Exception as e:  # noqa: BLE001
            log.debug("Page.stopScreencast failed: %s", e)
        if self._handler is not None:
            try:
                self.cdp.remove_listener("Page.screencastFrame", self._handler)
            except Exception:
                pass

    async def encode_mp4(self, out_path: Path) -> None:
        """Encode collected frames into an mp4 at the configured FPS."""
        await encode_frames_to_mp4(self.frames, out_path)


async def encode_frames_to_mp4(
    frames: list[_Frame],
    out_path: Path,
    fps: int = _OUTPUT_FPS,
    t0: float = 0.0,
    t1: float | None = None,
) -> None:
    """Encode `frames` into an mp4 at `fps`, covering the window [t0, t1].

    Slot times run relative to `t0`; for each slot, the most recent frame
    at-or-before slot time is chosen (frames are duplicated/dropped to
    match a constant output FPS). Output frame count is always even —
    Qwen3-VL's temporal patching requires it.

    `frames[i].monotonic_ts` values are interpreted as absolute (same
    clock the window was planned in). `t1` defaults to the last frame's
    timestamp when omitted.
    """
    if not frames:
        raise RecorderEncodeError("no frames to encode")
    if shutil.which("ffmpeg") is None:
        raise RecorderEncodeError(
            "ffmpeg not found on PATH — install ffmpeg to encode recordings"
        )

    if t1 is None:
        t1 = frames[-1].monotonic_ts
    window = max(t1 - t0, 0.5)
    n_out = max(4, int(window * fps))
    if n_out % 2:
        n_out += 1

    tmp_dir = out_path.parent / f"{out_path.stem}_frames"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        cap_idx = 0
        for i in range(n_out):
            slot_abs = t0 + i / fps
            while (
                cap_idx + 1 < len(frames)
                and frames[cap_idx + 1].monotonic_ts <= slot_abs
            ):
                cap_idx += 1
            frame = frames[cap_idx]
            (tmp_dir / f"frame_{i:05d}.jpg").write_bytes(frame.data)

        result = await run_process(
            "ffmpeg",
            "-y",
            "-framerate", str(fps),
            "-i", str(tmp_dir / "frame_%05d.jpg"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
            timeout=120.0,
        )
        if not result.ok:
            raise RecorderEncodeError(
                f"ffmpeg failed (rc={result.returncode}): {result.stderr[-500:]}"
            )
    finally:
        for p in tmp_dir.glob("*.jpg"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


class RecorderEncodeError(RuntimeError):
    """Raised when video encoding cannot proceed."""
