"""Per-chunk VLM annotation — one `chat.completions.create` per time window.

Sends one chunk (video clip + local events) to the vision model, with a
prompt extension telling the model where the clip sits in the global
recording and that overlapping chunks are intentional.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from openclose.config.config import get_config
from openclose.log import get_logger
from openclose.provider.provider import get_provider
from openclose.recorder.chunker import Chunk

log = get_logger(__name__)


_PROMPT_HEADER = (
    "You are watching a recording of a user performing a task in a web "
    "browser. Using the video and the timestamped events log below, write a "
    "precise, numbered procedure that another agent — which will only see "
    "this text — can follow to reproduce the task.\n\n"
    "For each step, describe the target element by its visible label, role, "
    "distinctive color, and position relative to nearby elements; give exact "
    "typed values verbatim; preserve order. Do not invent steps that aren't "
    "visible in the recording. Output the procedure only — no preamble.\n\n"
    "`navigate` entries with `\"origin\": \"external\"` mean the user "
    "navigated from outside the page — typing in the address bar, clicking a "
    "browser bookmark/favorite, or using back/forward. Describe the step in "
    "those terms; do not invent an on-page click to explain it. "
    "`\"origin\": \"in_page_click\"` means the navigation followed a click "
    "already in the events log.\n\n"
    "`select` entries record text the user highlighted on the page. "
    "`copy` / `cut` entries record what was placed on the clipboard. "
    "`paste` entries record text pasted into an element (with `target_label` "
    "naming the destination field). Use these to reproduce the exact text "
    "flow — quote the `text` field verbatim when describing what was copied "
    "or pasted.\n\n"
    "Events log (JSON, timestamps in seconds since recording start):\n"
)


def _chunk_header(chunk: Chunk, total: int) -> str:
    return (
        f"\nThis clip is chunk {chunk.index + 1} of {total}, covering global "
        f"time [{chunk.t_start:.1f}s, {chunk.t_end:.1f}s] of a longer "
        f"recording. Prefix each step with its global timestamp in seconds "
        f"(use the `t_global` field from EVENTS), e.g. `[14.3s]`. Overlap "
        f"with neighboring chunks is intentional — describe what you see "
        f"in this clip without speculating about what happened outside this "
        f"window.\n"
    )


def _resolve_model() -> str:
    config = get_config()
    if config.providers and config.providers[0].default_model:
        return config.providers[0].default_model
    return "local"


async def annotate_chunk(
    chunk: Chunk,
    video_path: Path,
    total_chunks: int,
) -> tuple[int, str]:
    """Send one chunk to the VLM. Return (chunk.index, procedure_text)."""
    provider = get_provider()
    model = _resolve_model()

    events_text = json.dumps(chunk.events, ensure_ascii=False, indent=2)
    text_part = (
        _PROMPT_HEADER
        + _chunk_header(chunk, total_chunks)
        + "```json\n"
        + events_text
        + "\n```"
    )

    video_bytes = video_path.read_bytes()
    video_b64 = base64.b64encode(video_bytes).decode("ascii")
    video_url = f"data:video/mp4;base64,{video_b64}"

    messages = [{
        "role": "user",
        "content": [
            {"type": "video_url", "video_url": {"url": video_url}},
            {"type": "text", "text": text_part},
        ],
    }]

    log.info(
        "chunk_annotator: chunk %d/%d [%.1fs-%.1fs], %d events, video %.1f MiB",
        chunk.index + 1, total_chunks, chunk.t_start, chunk.t_end,
        len(chunk.events), len(video_bytes) / (1024 * 1024),
    )

    extra_body = {
        "mm_processor_kwargs": {"do_sample_frames": False},
    }

    response = await provider.client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        temperature=get_config().temperatures.recorder_chunk_annotator,
        max_tokens=4096,
        extra_body=extra_body,
    )

    choice = response.choices[0]
    message = choice.message
    content = message.content or ""

    if not content.strip():
        reasoning = (
            getattr(message, "reasoning", None)
            or getattr(message, "reasoning_content", None)
            or ""
        )
        if reasoning.strip():
            log.warning(
                "chunk_annotator: chunk %d content empty, using reasoning "
                "(finish_reason=%s, %d chars)",
                chunk.index, choice.finish_reason, len(reasoning),
            )
            content = reasoning

    if not content.strip():
        raise RuntimeError(
            f"chunk {chunk.index}: VLM returned empty content "
            f"(finish_reason={choice.finish_reason})"
        )
    return chunk.index, content.strip()
