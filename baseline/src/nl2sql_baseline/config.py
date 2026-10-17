from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Settings:
    config_path: Path
    dataset_type: str
    examples_path: Path | None
    guidance_dir: Path | None
    test_set_path: Path
    workspace_root: Path
    database_root: Path | None
    include_tables: tuple[str, ...]
    llm_model: str
    llm_base_url: str | None
    llm_api_key: str
    temperature: float
    timeout: float
    top_k: int
    sql_timeout: float
    reference_time: str
    retriever_type: str
    memory_type: str
    embedding_model: str | None
    schema_linking_type: str
    schema_table_top_k: int
    schema_column_top_k: int
    schema_value_top_k: int
    schema_values_per_column: int


def load_settings(config_path: Path, *, env_file: Path | None = None) -> Settings:
    config_path = config_path.resolve()
    _load_env_file((env_file or config_path.parents[2] / ".env").resolve())
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path}: config must be an object")
    expanded = _expand(raw)
    base = config_path.parent

    def path(value: str) -> Path:
        candidate = Path(value)
        return candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()

    dataset = expanded.get("dataset") or {}
    if not isinstance(dataset, dict):
        raise ValueError("dataset must be an object")
    dataset_type = str(dataset.get("type", "bird")).strip().lower()
    if dataset_type not in {"bird", "spider"}:
        raise ValueError("dataset.type must be bird or spider")
    database = _mapping(expanded, "database")
    llm = _mapping(expanded, "llm")
    evaluation = _mapping(expanded, "evaluation")
    retrieval = expanded.get("retrieval") or {}
    if not isinstance(retrieval, dict):
        raise ValueError("retrieval must be an object")
    memory = expanded.get("memory") or {}
    if not isinstance(memory, dict):
        raise ValueError("memory must be an object")
    schema_linking = expanded.get("schema_linking") or {}
    if not isinstance(schema_linking, dict):
        raise ValueError("schema_linking must be an object")
    database_root_value = database.get("root")
    database_root = path(str(database_root_value)) if database_root_value else None
    if database_root is None:
        raise ValueError("database.root is required")
    api_key = _required(llm, "api_key")
    model = _required(llm, "model")
    include_tables = tuple(str(item) for item in database.get("include_tables", []))
    memory_type = str(memory.get("type", "raw_retrieval"))
    if memory_type not in {
        "none",
        "raw_retrieval",
        "downstream_guidance",
        "reference_guidance",
        "amem_retrieval",
        "tk_boost_repair_rules",
    }:
        raise ValueError(
            "memory.type must be none, raw_retrieval, downstream_guidance, reference_guidance, "
            "amem_retrieval, or tk_boost_repair_rules"
        )
    retriever_type = str(retrieval.get("type", "char_tfidf"))
    if retriever_type == "raw_retrieval":
        retriever_type = "char_tfidf"
    if retriever_type not in {"char_tfidf", "bm25", "embedding"}:
        raise ValueError("retrieval.type must be char_tfidf, bm25, embedding, or raw_retrieval")
    if memory_type in {
        "none",
        "downstream_guidance",
        "reference_guidance",
        "amem_retrieval",
        "tk_boost_repair_rules",
    }:
        retriever_type = "none"
    examples_value = expanded.get("examples_path")
    if memory_type == "raw_retrieval":
        examples_value = examples_value or memory.get("source_path")
    examples_path = path(str(examples_value)) if examples_value else None
    guidance_value = (
        memory.get("source_path")
        if memory_type
        in {"downstream_guidance", "reference_guidance", "amem_retrieval", "tk_boost_repair_rules"}
        else None
    )
    guidance_dir = path(str(guidance_value)) if guidance_value else None
    if memory_type == "raw_retrieval" and examples_path is None:
        raise ValueError("examples_path or memory.source_path is required for raw_retrieval")
    if (
        memory_type
        in {"downstream_guidance", "reference_guidance", "amem_retrieval", "tk_boost_repair_rules"}
        and guidance_dir is None
    ):
        raise ValueError(f"memory.source_path is required for {memory_type}")
    top_k = int(evaluation.get("top_k", 5))
    if memory_type == "raw_retrieval" and top_k < 1:
        raise ValueError("evaluation.top_k must be >= 1")
    schema_linking_type = str(schema_linking.get("type", "full_schema"))
    if schema_linking_type not in {"full_schema", "simple"}:
        raise ValueError("schema_linking.type must be full_schema or simple")
    return Settings(
        config_path=config_path,
        dataset_type=dataset_type,
        examples_path=examples_path,
        guidance_dir=guidance_dir,
        test_set_path=path(_required(expanded, "test_set_path")),
        workspace_root=path(str(expanded.get("workspace_root", "../workspace"))),
        database_root=database_root,
        include_tables=include_tables,
        llm_model=model,
        llm_base_url=_optional(llm.get("base_url")),
        llm_api_key=api_key,
        temperature=float(llm.get("temperature", 0)),
        timeout=float(llm.get("timeout", 300)),
        top_k=top_k,
        sql_timeout=float(evaluation.get("sql_timeout", 30)),
        reference_time=str(expanded.get("reference_time", "2026-08-21 00:00:00")),
        retriever_type=retriever_type,
        memory_type=memory_type,
        embedding_model=_optional(retrieval.get("model")),
        schema_linking_type=schema_linking_type,
        schema_table_top_k=int(schema_linking.get("table_top_k", 8)),
        schema_column_top_k=int(schema_linking.get("column_top_k", 30)),
        schema_value_top_k=int(schema_linking.get("value_top_k", 10)),
        schema_values_per_column=int(schema_linking.get("values_per_column", 100)),
    )


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _expand(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.getenv(name)
        if resolved is None:
            raise ValueError(f"required environment variable is not set: {name}")
        return resolved

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


def _mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ValueError(f"{key} must be an object")
    return result


def _required(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if result is None or not str(result).strip():
        raise ValueError(f"{key} must be configured")
    return str(result)


def _optional(value: Any) -> str | None:
    return str(value) if value is not None and str(value).strip() else None
