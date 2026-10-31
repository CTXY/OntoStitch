"""Shared BIRD CSV parsing, independent of its metadata/online consumers."""

import csv
import io


def decode_description(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AssertionError("latin-1 decodes every byte")


def parse_bird_descriptions(text: str) -> tuple[list[dict], list[str]]:
    records, issues = [], []
    reader = csv.DictReader(io.StringIO(text))
    if not {"original_column_name", "column_description"} <= set(
        reader.fieldnames or []
    ):
        return [], [
            "Missing BIRD CSV headers: original_column_name, column_description"
        ]
    try:
        for row in reader:
            line = reader.line_num
            if None in row or any(value is None for value in row.values()):
                issues.append(f"row {line}: malformed CSV record")
                continue
            name = row["original_column_name"]
            if not name.strip():
                issues.append(
                    f"row {line}: unknown or missing original_column_name {name!r}"
                )
                continue
            records.append(
                {
                    "line": line,
                    "column_name": name,
                    "business_name": row.get("column_name", "").strip(),
                    "description": row["column_description"].strip(),
                    "data_format": row.get("data_format", "").strip(),
                    "value_description": row.get("value_description", "").strip(),
                }
            )
    except csv.Error as exc:
        issues.append(f"Invalid CSV: {exc}")
    return records, issues
