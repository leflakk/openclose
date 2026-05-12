"""Message splitter for the deliver_message tool.

Splits a message into chunks so each one fits under a platform's hard
character cap (Telegram 4096, Discord 2000) while avoiding mid-token
breakage.

Preference order for split points:
fenced-block boundary > paragraph (``\\n\\n``) > line (``\\n``) >
sentence (``. `` / ``? `` / ``! ``) > word (`` ``) > raw slice.

Fenced code blocks that span a chunk boundary are closed with `` ``` ``
in the first chunk and reopened with the same language tag at the start
of the next chunk, so every chunk has balanced fences.

If there is more than one chunk, ``\\n(i/N)`` is appended to each.
"""

from __future__ import annotations

_MARKER_RESERVE = 10
"""Characters reserved at the tail of each chunk for an ``\\n(NN/NN)``
continuation marker. 10 covers ``\\n(99/99)``."""

_CLOSE_RESERVE = 4
"""Characters reserved for ``\\n````\\`\\`\\` (4 chars) in case a chunk
needs to close a code block that spans the boundary."""


def split_message(text: str, hard_limit: int) -> list[str]:
    """Split ``text`` into chunks each ``<= hard_limit`` characters.

    Returns a list of strings. If there is more than one chunk, each has
    ``\\n(i/N)`` appended (i is 1-indexed; N is the total count).
    """
    if hard_limit < 50:
        raise ValueError(f"hard_limit too small (min 50): {hard_limit}")
    if not text:
        return [""]
    if len(text) <= hard_limit:
        return [text]

    budget = hard_limit - _MARKER_RESERVE

    chunks: list[str] = []
    pos = 0
    n = len(text)
    open_lang: str | None = None

    while pos < n:
        reopen = f"```{open_lang}\n" if open_lang is not None else ""
        reopen_len = len(reopen)

        max_content = budget - reopen_len - _CLOSE_RESERVE
        if max_content <= 0:
            raise ValueError(
                f"hard_limit too small for code-block overhead: {hard_limit}"
            )

        remaining = n - pos

        # Fast path: the rest fits as one final chunk.
        if remaining <= max_content + _CLOSE_RESERVE:
            tail = text[pos:n]
            ending_lang = _state_after(tail, open_lang)
            if ending_lang is None:
                final_chunk = reopen + tail
            else:
                final_chunk = reopen + tail + "\n```"
            if len(final_chunk) <= budget:
                chunks.append(final_chunk)
                pos = n
                break

        end = min(pos + max_content, n)
        window = text[pos:end]
        break_idx = _find_break(window)
        chunk_content = window[:break_idx]

        new_open_lang = _state_after(chunk_content, open_lang)

        body = reopen + chunk_content
        if new_open_lang is not None:
            body = body.rstrip("\n") + "\n```"

        chunks.append(body)
        pos += break_idx
        open_lang = new_open_lang

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{c}\n({i + 1}/{total})" for i, c in enumerate(chunks)]

    for c in chunks:
        if len(c) > hard_limit:
            raise AssertionError(
                f"splitter produced {len(c)}-char chunk over limit {hard_limit}"
            )

    return chunks


def _state_after(content: str, initial_lang: str | None) -> str | None:
    """Return the code-block state after consuming ``content``.

    ``initial_lang`` is ``None`` outside a block, else the language tag
    (which may be empty). Returns ``None`` if not in a block at end, else
    the (possibly empty) language tag of the currently open block.
    """
    cur = initial_lang
    for line in content.split("\n"):
        stripped = line.rstrip()
        if stripped.startswith("```"):
            if cur is None:
                cur = stripped[3:].strip()
            else:
                cur = None
    return cur


def _find_break(window: str) -> int:
    """Find the best break point in ``window``; return index.

    The chunk will be ``window[:index]``. Always ``> 0`` when ``window``
    is non-empty, to ensure progress.
    """
    if not window:
        return 0

    idx = window.rfind("\n\n")
    if idx > 0:
        return idx + 2

    idx = window.rfind("\n")
    if idx > 0:
        return idx + 1

    for sep in (". ", "? ", "! "):
        idx = window.rfind(sep)
        if idx > 0:
            return idx + len(sep)

    idx = window.rfind(" ")
    if idx > 0:
        return idx + 1

    return len(window)
