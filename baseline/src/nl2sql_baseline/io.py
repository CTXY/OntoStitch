from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nl2sql_baseline.models import QARecord


def load_qa_records(path: Path) -> list[QARecord]:
    rows: list[Any]
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        rows = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("queries", [])
    records: list[QARecord] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: row {index} must be an object")
        if "evaluation" in row:
            raise ValueError(f"{path}: row {index} does not support per-record evaluation options")
        question = str(row.get("question") or row.get("query") or "").strip()
        sql = str(row.get("sql") or row.get("gt_sql") or row.get("SQL") or "").strip()
        if not question or not sql:
            raise ValueError(f"{path}: row {index} requires question and sql")
        records.append(
            QARecord(
                question=question,
                sql=sql,
                source_id=str(_source_id(row, index)),
                database_id=str(row.get("database_id") or row.get("db_id") or "default"),
                evidence=str(row.get("evidence") or "").strip(),
                difficulty=str(row.get("difficulty") or "").strip(),
            )
        )
    if not records:
        raise ValueError(f"{path}: no QA-SQL records found")
    return records


def _source_id(row: dict[str, Any], index: int) -> Any:
    for key in ("source_id", "id", "question_id"):
        if row.get(key) is not None and str(row[key]).strip():
            return row[key]
    return f"case-{index:04d}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(content, encoding="utf-8")
