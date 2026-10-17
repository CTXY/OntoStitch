from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from cortex.capabilities.sql_training.artifacts.jsonl import read_jsonl, write_jsonl
from cortex.capabilities.sql_training.contracts.qa import QAInput


def load_qa_sql_records(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    qa_path = Path(path)
    if not qa_path.exists():
        raise FileNotFoundError(qa_path)
    if qa_path.suffix.lower() == ".xlsx":
        return _load_xlsx_records(qa_path)
    if qa_path.suffix.lower() == ".jsonl":
        return read_jsonl(qa_path)
    data = json.loads(qa_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [_require_object(item, qa_path) for item in data]
    if isinstance(data, dict):
        if isinstance(data.get("records"), list):
            return [_require_object(item, qa_path) for item in data["records"]]
        if isinstance(data.get("queries"), list):
            return [_require_object(item, qa_path) for item in data["queries"]]
        return [data]
    raise ValueError(f"Unsupported QA SQL file shape: {qa_path}")


def load_standard_qa_sql_records(path: str | Path | None) -> list[QAInput]:
    records = load_qa_sql_records(path)
    return [_require_standard_record(record, index=index) for index, record in enumerate(records)]


def standardize_qa_sql_records(records: list[dict[str, Any]], *, dataset: str) -> list[QAInput]:
    if not dataset or not dataset.strip():
        raise ValueError("dataset is required")
    clean_dataset = dataset.strip()
    width = max(3, len(str(len(records))))
    normalized: list[QAInput] = []
    for index, record in enumerate(records, start=1):
        if "evaluation" in record:
            raise ValueError("per-record evaluation options are not supported")
        question = _raw_question(record).strip()
        sql = _raw_sql(record).strip()
        if not question:
            raise ValueError(f"QA SQL record {index} missing question")
        if not sql:
            raise ValueError(f"QA SQL record {index} missing sql")
        generated_sql = _raw_generated_sql(record).strip()
        source_id = str(record.get("source_id") or "").strip()
        normalized.append(
            QAInput(
                question=question,
                sql=sql,
                source_id=source_id or f"{clean_dataset}_{index:0{width}d}",
                evidence=record.get("evidence"),
                generated_sql=generated_sql or None,
                assets=list(record.get("assets") or []),
                metrics=list(record.get("metrics") or []),
                semantic_mappings=list(record.get("semantic_mappings") or []),
                generation_rules=list(record.get("generation_rules") or []),
            )
        )
    return normalized


def standardize_qa_sql_file(
    input_path: str | Path,
    *,
    dataset: str,
    output_path: str | Path,
) -> list[QAInput]:
    records = standardize_qa_sql_records(load_qa_sql_records(input_path), dataset=dataset)
    write_jsonl(output_path, [record.to_record() for record in records])
    return records


def _require_object(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain JSON objects")
    return value


def _require_standard_record(record: dict[str, Any], *, index: int) -> QAInput:
    expected = {"question", "sql", "source_id"}
    actual = set(record)
    missing = sorted(expected - actual)
    unsupported_source_fields = sorted({"id", "sourceid"} & actual)
    record_number = index + 1
    if missing:
        raise ValueError(
            f"Standard QA SQL record {record_number} missing required fields: {missing}"
        )
    if unsupported_source_fields:
        raise ValueError(
            "Standard QA SQL record "
            f"{record_number} contains unsupported source identity fields: "
            f"{unsupported_source_fields}"
        )
    out = dict(record)
    for key in ("question", "sql", "source_id"):
        out[key] = str(record[key]).strip()
    empty = [key for key in ("question", "sql", "source_id") if not out[key]]
    if empty:
        raise ValueError(f"Standard QA SQL record {record_number} has empty fields: {empty}")
    return QAInput.model_validate(out)


def _raw_question(record: dict[str, Any]) -> str:
    value = record.get("question") or record.get("task") or record.get("query")
    return str(value) if value is not None else ""


def _raw_sql(record: dict[str, Any]) -> str:
    value = (
        record.get("truth_sql")
        or record.get("gold_sql")
        or record.get("gt_sql")
        or record.get("sql_gt")
        or record.get("sql")
    )
    return str(value) if value is not None else ""


def _raw_generated_sql(record: dict[str, Any]) -> str:
    value = record.get("generated_sql") or record.get("predicted_sql")
    return str(value) if value is not None else ""


def _load_xlsx_records(path: Path) -> list[dict[str, Any]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for worksheet in workbook.worksheets:
            rows = worksheet.iter_rows(values_only=True)
            header = next(rows, None)
            if not header:
                continue
            names = [str(value).strip() if value is not None else "" for value in header]
            question_column = _first_present(names, ("question", "问题描述"))
            gold_columns = tuple(
                name
                for name in (
                    "真值 MySQL（为空则直接用D列执行）",
                    "sql_gt",
                    "gt_sql",
                    "gold_sql",
                    "sql",
                    "真值 GT SQL",
                )
                if name in names
            )
            if question_column is None or not gold_columns:
                continue
            records: list[dict[str, Any]] = []
            for values in rows:
                record = {
                    name: value
                    for name, value in zip(names, values, strict=False)
                    if name and value is not None
                }
                question = str(record.get(question_column) or "").strip()
                sql = next(
                    (
                        str(record.get(column) or "").strip()
                        for column in gold_columns
                        if str(record.get(column) or "").strip()
                    ),
                    "",
                )
                if question:
                    if question_column == "question":
                        output = dict(record)
                    else:
                        output = {"question": question, "sql": sql}
                    records.append(output)
            return records
    finally:
        workbook.close()
    raise ValueError(f"XLSX has no worksheet with question and gold SQL columns: {path}")


def _first_present(names: list[str], candidates: tuple[str, ...]) -> str | None:
    return next((candidate for candidate in candidates if candidate in names), None)


