"""File system utilities — watching, diffing, ignore patterns."""

from openclose.file.watcher import FileWatcher
from openclose.file.ignore import IgnoreManager
from openclose.file.diff import DiffTracker
from openclose.file.binary import is_binary

__all__ = ["FileWatcher", "IgnoreManager", "DiffTracker", "is_binary"]
