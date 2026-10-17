from __future__ import annotations

from pathlib import Path

from cortex.schema.models import SchemaDefinition

DEFAULT_ROOT_SCHEMA_PATH = Path(__file__).with_name("cortex_root.json")


def load_root_schema(path: str | Path | None = None) -> SchemaDefinition:
    root_path = Path(path).expanduser().resolve() if path else DEFAULT_ROOT_SCHEMA_PATH
    return SchemaDefinition.model_validate_json(root_path.read_text(encoding="utf-8"))
