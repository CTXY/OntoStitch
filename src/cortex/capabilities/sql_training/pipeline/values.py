from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

import yaml

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.builders import build_dimension_value_assets
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
from cortex.infrastructure.database import DatabaseConnector


class DimensionValuePipeline:
    def __init__(
        self,
        config: SqlTrainingRuntimeConfig,
        *,
        database: DatabaseConnector,
    ):
        self.config = config
        self.database = database
        self.workspace = Workspace(config.workspace)

    def run(
        self,
        path: str | Path = "configs/dimension_values.yaml",
        *,
        items: list[dict[str, Any]] | None = None,
    ) -> dict:
        config_path: Path | None = None
        if items is None:
            config_path = Path(path)
            if not config_path.exists():
                raise FileNotFoundError(config_path)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            items = raw.get("dimension_values") or []
        if not isinstance(items, list):
            raise ValueError("dimension_values must be a list")
        if not items:
            raise ValueError("At least one dimension value column is required")
        expanded_items = self._expand_distinct_values(items)
        assets = build_dimension_value_assets(expanded_items)
        self.workspace.write_draft_assets(NodeType.DIMENSION_VALUE, assets)
        return {
            "dimension_values": len(assets),
            "columns": len(items),
            "path": str(config_path) if config_path is not None else None,
        }

    def _expand_distinct_values(self, items: list[dict]) -> list[dict[str, Any]]:
        if not self.config.database.url:
            raise ValueError("database.url is required for cortex dim_values build")
        expanded: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("dimension_values entries must be objects")
            table_name = item.get("table_name")
            column_name = item.get("column_name")
            if not table_name or not column_name:
                raise ValueError("dimension_values entries require table_name and column_name")
            for value in self.database.distinct_column_values(
                str(table_name), str(column_name)
            ):
                if _is_punctuation_value(value):
                    continue
                expanded.append(
                    {
                        "table_name": table_name,
                        "column_name": column_name,
                        "value": value,
                        "source_type": "inferred",
                        "source_id": f"{table_name}.{column_name}",
                    }
                )
        return expanded


def _is_punctuation_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return True
    return all(unicodedata.category(char).startswith("P") for char in stripped)
