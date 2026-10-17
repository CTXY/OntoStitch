"""Reuse corrected physical literals from the main archive, verified read-only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .io import read_json, sha256, write_json


def complete_physical_values(main: Path, physical: Path, database: Path, required_ids: set[str]):
    values_path = physical / "assets" / "draft" / "dimension_values.json"
    values = {r["id"]: r for r in read_json(values_path)}
    tables = {
        r["id"]: r["properties"]["table_name"]
        for r in read_json(physical / "assets" / "draft" / "tables.json")
    }
    column_names = {}
    for row in read_json(physical / "assets" / "draft" / "columns.json"):
        owner = next(r for r in row["properties"]["references"] if r.startswith("table."))
        column_names[row["id"]] = (tables[owner], row["properties"]["column_name"])
    authoring_columns = {".".join(names): sid for sid, names in column_names.items()}
    repair = main / "dimension_value_repair_manifest.json"
    corrections = read_json(repair).get("changes", []) if repair.exists() else []
    for change in corrections:
        sid = change["node_id"]
        if not sid.startswith("dimension_value."):
            continue
        value = change.get("after", {}).get("value")
        column = change.get("column")
        if column is None or value is None:
            continue
        values[sid] = {
            "id": sid,
            "node_type": "dimension_value",
            "name": str(value),
            "properties": {"references": [authoring_columns[column]], "value": value},
            "sources": [{"source_type": "database", "source_id": column}],
        }
    if not database.is_file():
        raise FileNotFoundError(f"missing frozen BIRD database: {database}")
    problems = []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        for row in values.values():
            refs = row["properties"]["references"]
            column = next((r[7:] for r in refs if r.startswith("column.")), None)
            if column is None:
                problems.append({"id": row["id"], "reason": "missing owner Column"})
                continue
            table, field = column_names["column." + column]
            quoted_table = '"' + table.replace('"', '""') + '"'
            quoted_field = '"' + field.replace('"', '""') + '"'
            found = connection.execute(
                f"SELECT 1 FROM {quoted_table} WHERE {quoted_field} = ? LIMIT 1",
                (str(row["properties"]["value"]),),
            ).fetchone()
            if found is None and row["id"] in required_ids:
                problems.append({"id": row["id"], "reason": "literal absent from owner Column"})
            elif found is None:
                row["_unused_unverified"] = True
    values = {sid: r for sid, r in values.items() if not r.pop("_unused_unverified", False)}
    write_json(values_path, list(values.values()))
    report = {
        "database": str(database),
        "database_sha256": sha256(database),
        "verified_count": len(values) - len(problems),
        "problems": problems,
        "correction_source": str(repair) if repair.exists() else None,
    }
    write_json(physical / "verification.json", report)
    # A Gold-supported literal can legitimately match no current rows. Preserve
    # that observed training representation and report it; never invent a replacement.
    return report
