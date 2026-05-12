"""Feature flags from environment variables."""

import os


def _bool_flag(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "")
    if val.lower() in ("1", "true", "yes"):
        return True
    if val.lower() in ("0", "false", "no"):
        return False
    return default


def _int_flag(name: str, default: int) -> int:
    val = os.environ.get(name, "")
    try:
        return int(val)
    except ValueError:
        return default


DEBUG: bool = _bool_flag("OPENCLOSE_DEBUG")
DEBUG_LLM: bool = _bool_flag("OPENCLOSE_DEBUG_LLM")
BASH_DEFAULT_TIMEOUT_MS: int = _int_flag("OPENCLOSE_BASH_TIMEOUT_MS", 30_000)
DISABLE_FORMATTERS: bool = _bool_flag("OPENCLOSE_DISABLE_FORMATTERS")
DISABLE_FILE_WATCHER: bool = _bool_flag("OPENCLOSE_DISABLE_FILE_WATCHER")
