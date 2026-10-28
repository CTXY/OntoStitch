"""Optional, process-local progress sink shared by CLI and training workers.

Events contain counters and identifiers, never prompts, SQL, model payloads or keys.
Context propagation is provided by the existing ContextThreadPoolExecutor/LangGraph.
"""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

_sink = ContextVar("cortex_progress_sink", default=None)


class ProgressFile:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        self.sequence = 0

    def emit(self, event, **fields):
        with self.lock:
            self.sequence += 1
            row = {
                "sequence": self.sequence,
                "event": event,
                "at": datetime.now(UTC).isoformat(),
                **fields,
            }
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")


@contextmanager
def progress_file(path: Path | None):
    token = _sink.set(ProgressFile(path) if path else None)
    try:
        yield
    finally:
        _sink.reset(token)


def emit_progress(event: str, **fields):
    sink = _sink.get()
    if sink is not None:
        sink.emit(event, **fields)
