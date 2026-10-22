from __future__ import annotations

import json
from pathlib import Path

from cortex.application.online_context import QueryExample


def load_query_example_index(root: Path | None) -> dict[str, QueryExample]:
    if root is None:
        return {}
    if not root.is_dir():
        raise ValueError(f"query example source root is not a directory: {root}")
    cases_root = root / "cases" if (root / "cases").is_dir() else root
    paths = sorted(cases_root.glob("*/case.json"))
    if not paths:
        paths = sorted(cases_root.glob("case.json"))
    if not paths:
        raise ValueError(
            f"query example source root contains no case.json files: {root}"
        )
    index: dict[str, QueryExample] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))["source"]
        source_id = str(payload.get("source_id") or "").strip()
        if not source_id:
            raise ValueError(f"query example source lacks source_id: {path}")
        if source_id in index:
            raise ValueError(f"duplicate query example source_id {source_id!r}: {path}")
        index[source_id] = QueryExample(
            source_id=source_id,
            question=str(payload.get("question") or "").strip(),
            sql=(
                str(payload["sql"]).strip() if payload.get("sql") is not None else None
            ),
        )
    return index


__all__ = ["load_query_example_index"]
