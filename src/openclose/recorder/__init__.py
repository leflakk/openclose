"""Browser task recorder.

Captures a manual browser session over CDP (video + event log), splits it
into overlapping windows annotated by a VLM in parallel, merges the
per-chunk procedures, and stores the resulting procedure as a markdown
task file. See plan: Browser Task Recorder.
"""

from openclose.recorder.recorder import (
    RecorderError,
    start_recording,
    stop_recording,
    annotate_recording,
    get_active_recording,
)
from openclose.recorder.storage import (
    list_tasks,
    read_task,
    delete_task,
    Task,
)

__all__ = [
    "RecorderError",
    "start_recording",
    "stop_recording",
    "annotate_recording",
    "get_active_recording",
    "list_tasks",
    "read_task",
    "delete_task",
    "Task",
]
