from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def read_json(path: str | Path, *, default: Any = None) -> Any:
    json_path = Path(path)
    if not json_path.exists():
        return default
    content = json_path.read_text(encoding="utf-8")
    if not content.strip():
        return default
    return json.loads(content)


def write_json(path: str | Path, data: Any) -> Path:
    json_path = Path(path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False, default=str) + "\n",
        encoding="utf-8",
    )
    return json_path


def write_json_atomic(path: str | Path, data: Any) -> Path:
    """Write JSON atomically: write to a temp file then ``os.replace`` onto ``path``.

    Survives a crash mid-write — either the old version or the new version is
    present at ``path`` afterward, never a truncated file. Used by merge to keep
    the watermark consistent with flushed drafts.
    """
    json_path = Path(path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False, default=str) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{json_path.name}.", suffix=".tmp", dir=str(json_path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, json_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return json_path


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    jsonl_path = Path(path)
    if not jsonl_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            value = json.loads(stripped)
            if not isinstance(value, dict):
                raise ValueError(f"{jsonl_path}:{line_number} must contain a JSON object")
            rows.append(value)
    return rows


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]], *, append: bool = False) -> Path:
    jsonl_path = Path(path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with jsonl_path.open(mode, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=False, default=str) + "\n")
    return jsonl_path
