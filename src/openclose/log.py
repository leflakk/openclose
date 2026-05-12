"""Structured logging with rich output."""

import logging

from rich.logging import RichHandler

from openclose import flag


def setup_logging() -> None:
    """Configure root logger with rich handler."""
    level = logging.DEBUG if flag.DEBUG else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=flag.DEBUG)],
    )


def get_logger(name: str) -> logging.Logger:
    """Get a named logger."""
    return logging.getLogger(name)
