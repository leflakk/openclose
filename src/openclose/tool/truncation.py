"""Output truncation for tool results."""

from __future__ import annotations

_DEFAULT_MAX_LINES = 500
_DEFAULT_MAX_BYTES = 100_000


def truncate_output(
    text: str,
    max_lines: int = _DEFAULT_MAX_LINES,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> str:
    """Truncate output to fit within limits."""
    # Byte limit
    if len(text.encode("utf-8", errors="replace")) > max_bytes:
        text = text[:max_bytes]
        text += "\n... [output truncated at byte limit]"

    # Line limit
    lines = text.splitlines()
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        text = "\n".join(kept)
        text += f"\n... [{len(lines) - max_lines} more lines truncated]"

    return text
