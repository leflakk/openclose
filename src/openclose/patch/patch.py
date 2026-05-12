"""Unified diff generation and application."""

from __future__ import annotations

import difflib


def generate_diff(
    original: str,
    modified: str,
    from_file: str = "a",
    to_file: str = "b",
) -> str:
    """Generate a unified diff between two strings."""
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=from_file,
            tofile=to_file,
        )
    )


def apply_unified_diff(original: str, diff: str) -> str:
    """Apply a unified diff to the original text.

    Parses hunk headers and applies additions/removals line-by-line.
    """
    orig_lines = original.splitlines(keepends=True)
    diff_lines = diff.splitlines(keepends=True)

    result: list[str] = []
    orig_idx = 0
    i = 0

    while i < len(diff_lines):
        line = diff_lines[i]

        # Skip file headers
        if line.startswith("---") or line.startswith("+++"):
            i += 1
            continue

        # Parse hunk header: @@ -start,count +start,count @@
        if line.startswith("@@"):
            parts = line.split()
            old_spec = parts[1]  # e.g., -1,5
            old_start = int(old_spec.split(",")[0].lstrip("-"))

            # Copy unchanged lines before this hunk
            while orig_idx < old_start - 1 and orig_idx < len(orig_lines):
                result.append(orig_lines[orig_idx])
                orig_idx += 1

            i += 1
            while i < len(diff_lines):
                dline = diff_lines[i]
                if dline.startswith("@@") or dline.startswith("---") or dline.startswith("+++"):
                    break
                if dline.startswith("-"):
                    orig_idx += 1
                elif dline.startswith("+"):
                    result.append(dline[1:])
                elif dline.startswith(" "):
                    result.append(dline[1:])
                    orig_idx += 1
                elif dline.startswith("\\"):
                    pass  # "No newline at end of file"
                else:
                    break
                i += 1
            continue

        i += 1

    # Append remaining original lines
    while orig_idx < len(orig_lines):
        result.append(orig_lines[orig_idx])
        orig_idx += 1

    return "".join(result)
