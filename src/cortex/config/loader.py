from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from cortex.config.models import CortexConfig, DatabaseConfig, SQLiteDatabaseConfig

_ENV_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_MISSING = object()


class ConfigError(ValueError):
    pass


def load_config(path: str | Path = "configs/cortex.yaml") -> CortexConfig:
    config_path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Cortex config must be a YAML mapping")
    expanded = _expand(raw)
    config = CortexConfig.model_validate(expanded)
    base = config_path.parent.parent if config_path.parent.name == "configs" else config_path.parent
    root_path = _resolve_optional_path(config.root_schema.external_path, base)
    sql_training = config.services.sql_training
    online_context = config.services.online_context
    retrieval = config.services.retrieval
    workspace = _resolve_path(sql_training.workspace, base)
    retrieval_workspace = _resolve_path(retrieval.workspace, base)
    dimension_values_path = _resolve_optional_path(sql_training.dimension_values_path, base)
    return config.model_copy(
        update={
            "root_schema": config.root_schema.model_copy(update={"external_path": root_path}),
            "database": _resolve_database(config.database, base),
            "services": config.services.model_copy(
                update={
                    "online_context": online_context.model_copy(
                        update={
                            "query_example_source_root": _resolve_optional_path(
                                online_context.query_example_source_root, base
                            ),
                        }
                    ),
                    "retrieval": retrieval.model_copy(
                        update={"workspace": retrieval_workspace}
                    ),
                    "sql_training": sql_training.model_copy(
                        update={
                            "workspace": workspace,
                            "dimension_values_path": dimension_values_path,
                        }
                    )
                }
            ),
        }
    )


def _expand(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            expanded = _expand(item)
            if expanded is not _MISSING:
                result[key] = expanded
        return result
    if isinstance(value, list):
        return [expanded for item in value if (expanded := _expand(item)) is not _MISSING]
    if not isinstance(value, str):
        return value
    full = _ENV_PATTERN.fullmatch(value)
    if full:
        return os.environ.get(full.group(1), _MISSING)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(f"environment variable {name} is required")
        return os.environ[name]

    return _ENV_PATTERN.sub(replace, value)


def _resolve_optional_path(value: Path | None, base: Path) -> Path | None:
    return _resolve_path(value, base) if value is not None else None


def _resolve_database(config: DatabaseConfig, base: Path) -> DatabaseConfig:
    if not isinstance(config, SQLiteDatabaseConfig):
        return config
    return config.model_copy(
        update={
            "path": _resolve_path(config.path, base),
            "schema_path": _resolve_optional_path(config.schema_path, base),
            "seed_path": _resolve_optional_path(config.seed_path, base),
        }
    )


def _resolve_path(value: Path, base: Path) -> Path:
    expanded = value.expanduser()
    return expanded.resolve() if expanded.is_absolute() else (base / expanded).resolve()
