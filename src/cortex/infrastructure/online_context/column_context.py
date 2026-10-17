from __future__ import annotations

import csv
from pathlib import Path

from cortex.application.online_context import ColumnSemanticContext


class DatabaseDescriptionLoader:
    """Load optional per-table column semantics from database_description CSV files."""

    def load(self, directory: str | Path) -> tuple[ColumnSemanticContext, ...]:
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"database description directory does not exist: {root}")
        result = []
        for path in sorted(root.glob("*.csv")):
            table_name = path.stem
            with _open_description_csv(path) as handle:
                for row in csv.DictReader(handle):
                    column_name = str(row.get("original_column_name") or "").strip()
                    if not column_name:
                        continue
                    result.append(
                        ColumnSemanticContext(
                            column_id=f"column.{table_name}.{column_name}",
                            table_name=table_name,
                            column_name=column_name,
                            business_name=str(row.get("column_name") or "").strip(),
                            description=str(row.get("column_description") or "").strip(),
                            data_format=str(row.get("data_format") or "").strip(),
                            value_description=str(row.get("value_description") or "").strip(),
                        )
                    )
        return tuple(result)


def _open_description_csv(path: Path):
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            handle = path.open(encoding=encoding, newline="")
            handle.read(4096)
            handle.seek(0)
            return handle
        except UnicodeDecodeError:
            handle.close()
            continue
    return path.open(encoding="utf-8-sig", errors="replace", newline="")


__all__ = ["DatabaseDescriptionLoader"]
