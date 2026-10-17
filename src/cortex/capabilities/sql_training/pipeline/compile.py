from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.contracts.references import (
    physical_reference_catalog,
    validate_asset_batch_references,
)
from cortex.capabilities.sql_training.core.enums import NodeType, SourceType
from cortex.capabilities.sql_training.extractors import (
    SEMANTIC_CANDIDATE_TYPES,
    CandidateSource,
    GenerationRuleExtractor,
    MetricExtractor,
    SemanticMappingExtractor,
    extract_typed_candidates,
)
from cortex.capabilities.sql_training.extractors.parallel import run_parallel_extractions
from cortex.capabilities.sql_training.extractors.sql_normalizer import (
    build_column_table_map,
    collect_physical_column_references,
    normalize_sql_aliases,
)
from cortex.capabilities.sql_training.llm import LLMProvider
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.prepare import load_standard_qa_sql_records
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
from cortex.capabilities.sql_training.training.initial.semantic_values import (
    materialize_semantic_mapping_dimension_values,
)
from cortex.infrastructure.database import DatabaseConnector

_ASSET_LIST_KEYS = {
    "metrics",
    "semantic_mappings",
    "generation_rules",
}


class CompilePipeline:
    def __init__(
        self,
        config: SqlTrainingRuntimeConfig,
        *,
        llm_provider: LLMProvider | None = None,
        database: DatabaseConnector | None = None,
    ):
        self.config = config
        self.llm_provider = llm_provider
        self.database = database
        self.workspace = Workspace(config.workspace)
        self._last_initial_metric_candidate_sets: list[dict[str, Any]] = []

    def run(
        self,
        *,
        qa_path: str | Path | None = None,
        replace: bool = False,
        checkpoint: Callable[[], None] | None = None,
    ) -> dict:
        warnings: list[dict] = []
        qa_records = [record.to_record() for record in load_standard_qa_sql_records(qa_path)]
        if replace:
            self.workspace.delete_draft_assets(NodeType.EXAMPLE)
        qa_result = {
            "qa_records": len(qa_records),
            "candidate_counts": {},
            "skipped_invalid_candidates": [],
            "warnings": [],
            "workspace": str(self.workspace.root),
        }
        if qa_records:
            qa_result = self.extract_qa_records(
                qa_records,
                replace=replace,
                checkpoint=checkpoint,
            )
            warnings.extend(qa_result["warnings"])

        return {
            "qa_records": len(qa_records),
            "candidate_counts": qa_result["candidate_counts"],
            "skipped_invalid_candidates": qa_result["skipped_invalid_candidates"],
            "warnings": warnings,
            "initial_metric_candidate_sets": list(self._last_initial_metric_candidate_sets),
            "workspace": str(self.workspace.root),
        }

    def extract_qa_records(
        self,
        records: list[dict[str, Any]],
        *,
        replace: bool = False,
        current_metrics: tuple[NodeAsset, ...] | None = None,
        checkpoint: Callable[[], None] | None = None,
        llm_context: dict[str, Any] | None = None,
    ) -> dict:
        provider = self.llm_provider
        metric_snapshot = current_metrics
        if metric_snapshot is None:
            metric_snapshot = tuple(self.workspace.read_draft_assets(NodeType.METRIC))

        grouped, warnings, skipped = self._extract_qa_candidates(
            records,
            provider,
            current_metrics=metric_snapshot,
            column_table_map=_build_column_table_map(self.database, self.config),
            catch_provider_errors=self.llm_provider is None,
            checkpoint=checkpoint,
            llm_context=llm_context,
        )

        write_types = set(grouped)
        if replace and records:
            write_types.update(SEMANTIC_CANDIDATE_TYPES)

        for asset_type in sorted(write_types):
            if checkpoint is not None:
                checkpoint()
            assets = grouped.get(asset_type, [])
            if replace:
                self.workspace.write_candidates(asset_type, assets)
            elif assets:
                self.workspace.append_candidates(asset_type, assets)

        return {
            "qa_records": len(records),
            "candidate_counts": {
                asset_type.value: len(assets) for asset_type, assets in sorted(grouped.items())
            },
            "skipped_invalid_candidates": skipped,
            "warnings": warnings,
            "initial_metric_candidate_sets": list(self._last_initial_metric_candidate_sets),
            "workspace": str(self.workspace.root),
        }

    def _extract_qa_candidates(
        self,
        records: list[dict[str, Any]],
        provider: LLMProvider | None,
        *,
        current_metrics: tuple[NodeAsset, ...] = (),
        column_table_map: dict[str, str] | None = None,
        catch_provider_errors: bool = False,
        checkpoint: Callable[[], None] | None = None,
        llm_context: dict[str, Any] | None = None,
    ) -> tuple[dict[NodeType, list[NodeAsset]], list[dict], list[dict]]:
        grouped: dict[NodeType, list[NodeAsset]] = defaultdict(list)
        warnings: list[dict] = []
        skipped: list[dict] = []
        provider_available = provider is not None
        database_context = {
            str(asset.properties.table_name): str(asset.properties.table_schema)
            for asset in self.workspace.read_draft_assets(NodeType.TABLE)
            if asset.properties.table_name and asset.properties.table_schema
        }
        column_metadata = {
            asset.id: {
                "column": asset.properties.column_name,
                "table": next(
                    (
                        reference.removeprefix("table.")
                        for reference in asset.properties.references
                        if reference.startswith("table.")
                    ),
                    None,
                ),
                "data_type": asset.properties.data_type,
                "description": asset.description,
                "sample_values": list(asset.properties.sample_values),
                "nullable": asset.properties.nullable,
                "semantic_role": (
                    asset.properties.semantic_role.value
                    if asset.properties.semantic_role is not None
                    else None
                ),
                "key_role": (
                    asset.properties.key_role.value
                    if asset.properties.key_role is not None
                    else None
                ),
            }
            for asset in self.workspace.read_draft_assets(NodeType.COLUMN)
        }
        working_semantic_assets = [
            asset
            for asset_type in SEMANTIC_CANDIDATE_TYPES
            for asset in self.workspace.read_draft_assets(asset_type)
        ]
        metric_extractor = MetricExtractor()
        semantic_mapping_extractor = SemanticMappingExtractor(table_schema=database_context)
        llm_qa_extractors = [
            metric_extractor,
            semantic_mapping_extractor,
            GenerationRuleExtractor(),
        ]
        explicit_asset_extractors = [
            metric_extractor,
            semantic_mapping_extractor,
            GenerationRuleExtractor(),
        ]
        total = len(records)
        initial_metric_candidate_sets: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            if checkpoint is not None:
                checkpoint()
            explicit_assets = _explicit_assets(record)
            extracted_assets: list[Any] = []
            source_id = _record_source_id(record)
            question = _question(record)
            sql = _sql(record)
            source = CandidateSource(
                source_type=SourceType.QA,
                source_id=source_id,
                error_prefix="QA",
            )
            print(
                f"\n[compile {index + 1}/{total}] {source_id}  q={question[:60]!r}",
                flush=True,
            )
            if provider_available:
                if sql:
                    try:
                        normalized_sql = normalize_sql_aliases(
                            sql,
                            column_table_map=column_table_map,
                            dialect=self.config.database.dialect,
                        )
                        available_column_refs = {
                            reference
                            for reference in physical_reference_catalog(database_context)
                            if reference.startswith("column.")
                        }
                        gold_physical_refs = (
                            collect_physical_column_references(
                                normalized_sql,
                                available_column_refs=available_column_refs,
                                dialect=self.config.database.dialect,
                            )
                            if available_column_refs
                            else None
                        )
                        print(
                            f"  [compile {index + 1}/{total}] extracting Metric + SM + Rule ...",
                            flush=True,
                        )
                        extracted = run_parallel_extractions(
                            provider=provider,
                            record={
                                "question": question,
                                "sql": normalized_sql,
                                **(
                                    {"evidence": record["evidence"]}
                                    if record.get("evidence")
                                    else {}
                                ),
                                "database_context": database_context,
                                "database_dialect": self.config.database.dialect,
                                "routing_join_paths": record.get("routing_join_paths") or [],
                                "direct_join_alternatives": record.get("direct_join_alternatives")
                                or [],
                                **(
                                    {"gold_physical_refs": sorted(gold_physical_refs)}
                                    if gold_physical_refs is not None
                                    else {}
                                ),
                                **(
                                    {
                                        "gold_column_metadata": {
                                            reference: column_metadata[reference]
                                            for reference in sorted(gold_physical_refs)
                                            if reference in column_metadata
                                        }
                                    }
                                    if gold_physical_refs is not None
                                    else {}
                                ),
                                "current_metrics": [
                                    asset.model_dump(mode="json") for asset in current_metrics
                                ],
                            },
                            context={
                                "source_id": source_id,
                                **(llm_context or {}),
                            },
                        )
                        if checkpoint is not None:
                            checkpoint()
                        extracted_assets.extend(extracted)
                        extracted_assets, metric_sets = _separate_initial_metric_set_metadata(
                            extracted_assets,
                            source_id=source_id,
                        )
                        initial_metric_candidate_sets.extend(metric_sets)
                        by_type: dict[str, int] = {}
                        for a in extracted:
                            t = a.get("type") if isinstance(a, dict) else "?"
                            by_type[t] = by_type.get(t, 0) + 1
                        print(
                            f"  [compile {index + 1}/{total}] raw candidates: {len(extracted)} "
                            f"({by_type})",
                            flush=True,
                        )
                    except Exception as exc:
                        if not catch_provider_errors:
                            raise
                        provider_available = False
                        warnings.append(
                            {
                                "code": "LLM_EXTRACTION_FAILED",
                                "record": source_id,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            }
                        )
                        skipped.append(
                            {
                                "code": "SKIPPED_LLM_EXTRACTION",
                                "record": source_id,
                                "reason": "llm_extraction_failed",
                            }
                        )
                else:
                    warnings.append(
                        {
                            "code": "QA_SQL_MISSING",
                            "record": source_id,
                        }
                    )
                    skipped.append(
                        {
                            "code": "SKIPPED_LLM_EXTRACTION",
                            "record": source_id,
                            "reason": "sql_missing",
                        }
                    )
            elif provider is not None:
                skipped.append(
                    {
                        "code": "SKIPPED_LLM_EXTRACTION",
                        "record": source_id,
                        "reason": "llm_provider_unavailable",
                    }
                )
            extracted_by_type = extract_typed_candidates(
                explicit_assets,
                source=source,
                extractors=explicit_asset_extractors,
                skipped=skipped,
            )
            for asset_type, assets in extract_typed_candidates(
                extracted_assets,
                source=source,
                extractors=llm_qa_extractors,
                skipped=skipped,
            ).items():
                extracted_by_type.setdefault(asset_type, []).extend(assets)
            record_assets = [asset for assets in extracted_by_type.values() for asset in assets]
            try:
                record_assets, generated_values = materialize_semantic_mapping_dimension_values(
                    record_assets,
                    table_schema=database_context,
                    database=self.database,
                    source_id=source_id,
                )
            except ValueError as exc:
                skipped.append(
                    {
                        "code": "INVALID_SEMANTIC_MAPPING_DIMENSION_VALUE",
                        "record": source_id,
                        "reason": str(exc),
                    }
                )
                print(
                    f"  [compile {index + 1}/{total}] rejected semantic value "
                    f"materialization: {exc}",
                    flush=True,
                )
                continue
            if generated_values:
                _merge_workspace_dimension_values(self.workspace, generated_values)
                extracted_by_type = defaultdict(list)
                for asset in record_assets:
                    extracted_by_type[asset.node_type].append(asset)
            try:
                validate_asset_batch_references(
                    record_assets,
                    working_assets=working_semantic_assets,
                    table_schema=database_context,
                )
            except ValueError as exc:
                skipped.append(
                    {
                        "code": "INVALID_REFERENCE_CLOSURE",
                        "record": source_id,
                        "reason": str(exc),
                    }
                )
                print(
                    f"  [compile {index + 1}/{total}] rejected reference closure: {exc}",
                    flush=True,
                )
                continue
            for asset_type, assets in extracted_by_type.items():
                grouped[asset_type].extend(assets)
                for asset in assets:
                    print(
                        json.dumps(asset.model_dump(mode="json"), ensure_ascii=False),
                        flush=True,
                    )
            running = {t.value: len(v) for t, v in grouped.items() if v}
            print(
                f"  [compile {index + 1}/{total}] cumulative valid: {running}",
                flush=True,
            )
        valid_metric_ids = {
            asset.id for asset in grouped.get(NodeType.METRIC, ())
        }
        self._last_initial_metric_candidate_sets = _filter_initial_metric_candidate_sets(
            initial_metric_candidate_sets,
            valid_metric_ids=valid_metric_ids,
        )
        return dict(grouped), warnings, skipped


def _merge_workspace_dimension_values(
    workspace: Workspace,
    generated_values: list[NodeAsset],
) -> None:
    by_id = {
        asset.id: asset for asset in workspace.read_draft_assets(NodeType.DIMENSION_VALUE)
    }
    for asset in generated_values:
        existing = by_id.get(asset.id)
        if existing is None:
            by_id[asset.id] = asset
            continue
        sources = {
            (source.source_type.value, source.source_id): source
            for source in [*existing.sources, *asset.sources]
        }
        by_id[asset.id] = asset.model_copy(
            update={"sources": [sources[key] for key in sorted(sources)]}
        )
    workspace.write_draft_assets(NodeType.DIMENSION_VALUE, by_id.values())


def _separate_initial_metric_set_metadata(
    assets: list[Any],
    *,
    source_id: str,
) -> tuple[list[Any], list[dict[str, Any]]]:
    clean_assets: list[Any] = []
    sets: dict[str, dict[str, Any]] = {}
    for raw_asset in assets:
        if not isinstance(raw_asset, dict) or "_candidate_set_id" not in raw_asset:
            clean_assets.append(raw_asset)
            continue
        asset = dict(raw_asset)
        candidate_set_id = str(asset.pop("_candidate_set_id"))
        asset_id = str(asset.get("id") or "")
        entry = sets.setdefault(
            candidate_set_id,
            {
                "source_id": source_id,
                "candidate_id": candidate_set_id,
                "asset_ids": [],
            },
        )
        entry["asset_ids"].append(asset_id)
        clean_assets.append(asset)
    return clean_assets, list(sets.values())


def _filter_initial_metric_candidate_sets(
    candidate_sets: list[dict[str, Any]],
    *,
    valid_metric_ids: set[str],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for candidate_set in candidate_sets:
        asset_ids = [
            str(asset_id)
            for asset_id in candidate_set.get("asset_ids", ())
            if str(asset_id) in valid_metric_ids
        ]
        if asset_ids:
            filtered.append({**candidate_set, "asset_ids": asset_ids})
    return filtered


def _explicit_assets(record: dict[str, Any]) -> list[Any]:
    assets = list(record.get("assets") or [])
    for key in _ASSET_LIST_KEYS:
        assets.extend(record.get(key) or [])
    return [
        asset
        for asset in assets
        if not (isinstance(asset, dict) and str(asset.get("type") or "") == NodeType.EXAMPLE.value)
    ]


def _question(record: dict[str, Any]) -> str:
    return str(record.get("question") or "")


def _sql(record: dict[str, Any]) -> str | None:
    value = record.get("sql")
    return str(value) if value is not None else None


def _record_source_id(record: dict[str, Any]) -> str:
    return str(record["source_id"])


def _build_column_table_map(
    database: DatabaseConnector | None,
    config: SqlTrainingRuntimeConfig,
) -> dict[str, str] | None:
    """Build a column-name → table-name mapping from the configured database.

    Returns None when no database URL is configured or introspection fails.
    """
    if database is None:
        return None
    try:
        catalog = database.introspect_catalog(
            include_tables=config.database.include_tables or None,
        )
        column_dicts = [
            {"column_name": col.column_name, "table_name": col.table_name}
            for col in catalog.columns
        ]
        return build_column_table_map(column_dicts)
    except Exception:
        return None
