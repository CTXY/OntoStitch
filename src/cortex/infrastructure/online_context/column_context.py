from __future__ import annotations

from pathlib import Path

from cortex.application.online_context import ColumnSemanticContext
from cortex.infrastructure.bird_descriptions import (
    decode_description,
    parse_bird_descriptions,
)


class DatabaseDescriptionLoader:
    """Adapt shared BIRD records to the existing online/training context contract."""

    def load(self, directory: str | Path) -> tuple[ColumnSemanticContext, ...]:
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"database description directory does not exist: {root}")
        result = []
        for path in sorted(root.glob("*.csv")):
            records, _ = parse_bird_descriptions(decode_description(path.read_bytes()))
            # Preserve the legacy consumer's skip-invalid-records behavior.
            for record in records:
                result.append(
                    ColumnSemanticContext(
                        column_id=f"column.{path.stem}.{record['column_name']}",
                        table_name=path.stem,
                        **{
                            key: value for key, value in record.items() if key != "line"
                        },
                    )
                )
        return tuple(result)


__all__ = ["DatabaseDescriptionLoader"]
