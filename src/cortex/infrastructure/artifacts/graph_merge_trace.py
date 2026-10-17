from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class FileSystemGraphMergeTraceSink:
    """Append-only JSONL trace adapter for live Graph Merge Agent calls."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8"):
            pass

    def record(self, event: dict[str, Any]) -> None:
        payload = {
            "sequence": self._line_count() + 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            **event,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _line_count(self) -> int:
        with self.path.open(encoding="utf-8") as handle:
            return sum(1 for _ in handle)


__all__ = ["FileSystemGraphMergeTraceSink"]
