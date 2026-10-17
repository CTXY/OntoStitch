from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from cortex.application.sql_training import (
    QASqlTrainingRecord,
    SqlTrainingCaseResult,
    SqlTrainingEvaluationReport,
)
from cortex.capabilities.sql_training.artifacts import (
    Workspace,
    deserialize_asset_envelope,
    write_jsonl,
)
from cortex.capabilities.sql_training.core.enums import NodeType, SourceType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.graph.adapter import NodeGraphAdapter
from cortex.capabilities.sql_training.graph.case import project_case_edges
from cortex.capabilities.sql_training.graph.physical import select_run_assets
from cortex.capabilities.sql_training.llm import NodeLLMProvider
from cortex.capabilities.sql_training.models import (
    ExampleProperties,
    NodeAsset,
    Source,
)
from cortex.capabilities.sql_training.pipeline.init import ScanCatalogPipeline
from cortex.capabilities.sql_training.pipeline.train import TrainPipeline
from cortex.capabilities.sql_training.pipeline.values import DimensionValuePipeline
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
from cortex.config import CortexConfig
from cortex.infrastructure.database import DatabaseConnector
from cortex.schema.graph import GraphEdge, GraphSpace, GraphState
from cortex.schema.models import SchemaDefinition


class SqlTrainingConfigurationError(ValueError):
    """Required SQL-training infrastructure is not configured."""


class SqlTrainingCapability:
    """Historical Node training runtime exposed through the current Engine contract."""

    def __init__(
        self,
        *,
        llm: BaseChatModel,
        config: CortexConfig,
        runtime_config: SqlTrainingRuntimeConfig | None = None,
        validation_client: Any | None = None,
        validation_client_factory: Callable[[], Any] | None = None,
        case_llm_factory: Callable[
            [], AbstractContextManager[tuple[BaseChatModel, Any]]
        ] | None = None,
        database: DatabaseConnector | None = None,
        pipeline_factory: Callable[..., TrainPipeline] | None = None,
        prepared_workspace: Path | None = None,
    ) -> None:
        self.llm = llm
        self.config = config
        self.runtime_config = runtime_config or SqlTrainingRuntimeConfig.from_cortex(config)
        self.validation_client = validation_client
        self.validation_client_factory = validation_client_factory
        self.case_llm_factory = case_llm_factory
        self.database = database
        self.pipeline_factory = pipeline_factory or TrainPipeline
        self.prepared_workspace = prepared_workspace

    async def train(
        self,
        training_set: tuple[QASqlTrainingRecord, ...],
        schema: SchemaDefinition,
        existing_graph: GraphSpace,
    ) -> tuple[GraphState, SqlTrainingEvaluationReport]:
        # The migrated pipeline is still synchronous. Keep ownership of its worker
        # until it has completed even when the caller is cancelled; otherwise
        # asyncio.run() may begin interpreter shutdown while the worker continues
        # scheduling LLM/database futures.
        task = asyncio.create_task(
            asyncio.to_thread(
                self._train_sync,
                training_set,
                schema,
                existing_graph,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

    def _train_sync(
        self,
        training_set: tuple[QASqlTrainingRecord, ...],
        schema: SchemaDefinition,
        existing_graph: GraphSpace,
    ) -> tuple[GraphState, SqlTrainingEvaluationReport]:
        self._validate_configuration()
        workspace = Workspace(self.runtime_config.workspace)
        workspace.init()
        qa_path = workspace.inputs_dir / "sql_training.qa_sql.jsonl"
        write_jsonl(qa_path, [_training_record(record) for record in training_set])

        try:
            if self.prepared_workspace is None:
                ScanCatalogPipeline(self.runtime_config, database=self.database).run()
            else:
                from cortex.capabilities.sql_training.artifacts.workspace import ASSET_GROUP_FILES

                for kind in (NodeType.TABLE, NodeType.COLUMN, NodeType.DIMENSION_VALUE):
                    source = self.prepared_workspace / "assets" / "draft" / ASSET_GROUP_FILES[kind]
                    if not source.is_file():
                        raise ValueError(f"missing frozen physical assets: {source}")
                    shutil.copy2(source, workspace.draft_dir / source.name)
                source = self.prepared_workspace / "assets" / "draft" / "catalog_edges.json"
                if not source.is_file():
                    raise ValueError(f"missing frozen catalog edges: {source}")
                shutil.copy2(source, workspace.draft_dir / source.name)
            dimension_path = self.config.services.sql_training.dimension_values_path
            if dimension_path is not None and self.prepared_workspace is None:
                DimensionValuePipeline(
                    self.runtime_config,
                    database=self.database,
                ).run(dimension_path)

            pipeline = self.pipeline_factory(
                self.runtime_config,
                llm_provider=NodeLLMProvider(self.llm),
                validation_client=self.validation_client,
                validation_client_factory=self.validation_client_factory,
                case_resources_factory=(
                    self._case_resources if self.case_llm_factory is not None else None
                ),
                database=self.database,
                schema=schema,
                existing_join_edges=tuple(
                    edge for edge in existing_graph.edges if edge.edge_type == "joins_with"
                ),
            )
            raw_result = pipeline.run(
                qa_path=qa_path,
                dataset="sql_training",
                max_rounds=self.config.services.sql_training.max_rounds,
                max_iterations=self.config.services.sql_training.max_iterations,
                sql_timeout=self.config.services.sql_training.sql_timeout,
            )
        except Exception as exc:
            raw_result = _fatal_result(workspace, exc)

        try:
            _upsert_training_records(workspace, training_set)
            run_assets = select_run_assets(
                workspace.read_committed_assets(),
                existing_graph=existing_graph,
            )
            case_fragments = _merge_eligible_case_fragments(raw_result)
            run_assets = _with_case_referenced_dimension_values(
                run_assets,
                case_fragments,
                existing_graph=existing_graph,
            )
            state, adapter_warnings = NodeGraphAdapter(workspace).to_trace_incoming_state(
                schema,
                committed_assets=run_assets,
                case_fragments=case_fragments,
                existing_edge_ids=(edge.id for edge in existing_graph.edges),
                existing_nodes=existing_graph.nodes,
            )
        except Exception as exc:
            state = GraphState(graph=GraphSpace(), node_conflicts=())
            adapter_warnings = ()
            raw_result = _append_fatal_error(raw_result, exc)
        report = _report(raw_result, workspace, adapter_warnings, training_set)
        return state, report

    @contextmanager
    def _case_resources(self):
        with self.case_llm_factory() as (llm, validation_client):
            yield NodeLLMProvider(llm), validation_client

    def _validate_configuration(self) -> None:
        missing = []
        if not self.runtime_config.database.url:
            missing.append("database.url")
        if missing:
            raise SqlTrainingConfigurationError(
                f"Missing required SQL training configuration: {', '.join(missing)}"
            )


def _training_record(record: QASqlTrainingRecord) -> dict[str, Any]:
    return {
        "source_id": record.source_id,
        "question": record.question,
        "sql": record.sql,
        "evidence": record.evidence,
        "generated_sql": record.generated_sql,
        "assets": list(record.assets),
        "metrics": list(record.metrics),
        "semantic_mappings": list(record.semantic_mappings),
        "generation_rules": list(record.generation_rules),
    }


def _upsert_training_records(
    workspace: Workspace,
    training_set: tuple[QASqlTrainingRecord, ...],
) -> None:
    existing = {asset.id: asset for asset in workspace.read_draft_assets(NodeType.EXAMPLE)}
    for record in training_set:
        asset_id = make_node_id(NodeType.EXAMPLE, record.source_id)
        existing[asset_id] = NodeAsset(
            id=asset_id,
            node_type=NodeType.EXAMPLE,
            name=record.question[:80],
            description="Validated QA-SQL training record",
            properties=ExampleProperties(
                task=record.question,
                sql=record.sql,
                generated_sql=record.generated_sql,
            ),
            sources=[Source(source_type=SourceType.QA, source_id=record.source_id)],
        )
    workspace.write_draft_assets(NodeType.EXAMPLE, existing.values())


def _merge_eligible_case_fragments(
    result: dict[str, Any],
) -> tuple[tuple[str, tuple[NodeAsset, ...], tuple[GraphEdge, ...]], ...]:
    """Project the case outputs already marked ready by Train into Graph Merge fragments."""

    fragments = []
    for case in result.get("cases") or []:
        if case.get("asset_outcome") != "ready":
            continue
        source_id = str(case.get("source_id") or "")
        if not source_id:
            raise ValueError("merge-eligible Trace case is missing source_id")
        assets = tuple(
            deserialize_asset_envelope(row) for row in case.get("draft_nodes") or []
        )
        if not assets:
            raise ValueError(f"merge-eligible Trace case {source_id!r} has no draft assets")
        fragments.append((source_id, assets, project_case_edges(assets)))
    return tuple(fragments)


def _with_case_referenced_dimension_values(
    run_assets: tuple[NodeAsset, ...],
    case_fragments: tuple[tuple[str, tuple[NodeAsset, ...], tuple[GraphEdge, ...]], ...],
    *,
    existing_graph: GraphSpace,
) -> tuple[NodeAsset, ...]:
    existing_ids = {node.id for node in existing_graph.nodes}
    asset_ids = {asset.id for asset in run_assets}
    required_values = sorted({
        reference
        for _, assets, _ in case_fragments
        for asset in assets
        for reference in asset.references()
        if reference.startswith("dimension_value.")
        and reference not in existing_ids
        and reference not in asset_ids
    })
    if required_values:
        raise ValueError(
            "case assets reference Dimension Values absent from verified database assets: "
            f"{required_values}"
        )
    return run_assets


def _report(
    result: dict[str, Any],
    workspace: Workspace,
    adapter_warnings: tuple[str, ...],
    training_set: tuple[QASqlTrainingRecord, ...],
) -> SqlTrainingEvaluationReport:
    raw_cases = list(result.get("cases") or [])
    records_by_id = {record.source_id: record for record in training_set}
    cases = tuple(
        _case_result(case, records_by_id.get(str(case.get("source_id") or "")))
        for case in raw_cases
    )
    total = len(cases)
    passed = sum(case.passed for case in cases)
    errors = tuple(str(item) for item in result.get("errors") or [])
    warnings = tuple(str(item) for item in result.get("warnings") or []) + adapter_warnings
    fatal = bool(result.get("fatal")) or bool(errors and not cases)
    return SqlTrainingEvaluationReport(
        total=total,
        passed=passed,
        failed=total - passed,
        cases=cases,
        success=bool(result.get("success", not errors)),
        fatal=fatal,
        run_id=result.get("run_id"),
        sql_correct_cases=int(result.get("sql_correct_cases") or passed),
        ontology_updated_cases=int(result.get("ontology_updated_cases") or 0),
        generation_rule_updated_cases=int(result.get("generation_rule_updated_cases") or 0),
        metric_updated_cases=int(result.get("metric_updated_cases") or 0),
        optimization_trial_cases=int(result.get("optimization_trial_cases") or 0),
        failed_optimization_cases=int(result.get("failed_optimization_cases") or 0),
        accepted_generation_rule_count=int(result.get("accepted_generation_rule_count") or 0),
        accepted_metric_count=int(result.get("accepted_metric_count") or 0),
        errors=errors,
        warnings=warnings,
        workspace=str(workspace.root),
        trace_path=result.get("trace_path"),
    )


def _case_result(
    value: dict[str, Any],
    record: QASqlTrainingRecord | None,
) -> SqlTrainingCaseResult:
    passed = bool(value.get("correct"))
    failed_closed = bool(value.get("failed_closed"))
    fatal = bool(value.get("fatal"))
    training_status = (
        "failed"
        if fatal
        else "passed"
        if passed
        else "failed_closed"
        if failed_closed
        else "needs_review"
    )
    baseline = dict(value.get("baseline_evaluation_summary") or {})
    final = dict(value.get("current_evaluation_summary") or {})
    predicted_sql = value.get("predicted_sql") or value.get("query")
    if predicted_sql is None and record is not None:
        predicted_sql = record.generated_sql
    reason = value.get("reason") or value.get("failure_reason")
    return SqlTrainingCaseResult(
        source_id=str(value.get("source_id") or "unknown"),
        passed=passed,
        question=value.get("question") or (record.question if record is not None else None),
        gold_sql=record.sql if record is not None else value.get("gold_sql"),
        predicted_sql=predicted_sql,
        training_status=training_status,
        message=reason,
        fatal=fatal,
        ontology_updated=bool(value.get("ontology_updated")),
        failed_closed=failed_closed,
        accepted_metric_ids=tuple(value.get("accepted_metric_ids") or []),
        accepted_generation_rule_ids=tuple(value.get("accepted_rule_ids") or []),
        committed_semantic_mapping_ids=tuple(value.get("committed_semantic_mapping_ids") or []),
        replay_status=value.get("replay_status"),
        required_correct_replays=value.get("required_correct_replays"),
        completed_replays=value.get("completed_replays"),
        transaction_rolled_back=bool(value.get("transaction_rolled_back")),
        completion_reason=value.get("completion_reason"),
        asset_outcome=value.get("asset_outcome"),
        draft_node_ids=tuple(value.get("draft_node_ids") or []),
        draft_node_count=int(value.get("draft_node_count") or 0),
        bundle_path=value.get("bundle_path"),
        iterations=int(value.get("iterations") or 0),
        trace_id=value.get("trace_id"),
        execution_comparison={
            "correct": passed,
            "reason": reason,
            "baseline": baseline,
            "final": final,
        },
        baseline_evaluation=baseline,
        final_evaluation=final,
        active_targets_complete=bool(
            value.get(
                "active_targets_complete",
                value.get("targets_complete", passed),
            )
        ),
        residual_status=str(
            value.get("residual_status")
            or ("none" if passed else "pending_analysis")
        ),
        case_stable_correct=bool(value.get("case_stable_correct", passed)),
    )


def _fatal_result(workspace: Workspace, exc: Exception) -> dict[str, Any]:
    return {
        "success": False,
        "fatal": True,
        "run_id": None,
        "cases": [],
        "errors": [f"{type(exc).__name__}: {exc}"],
        "warnings": [],
        "workspace": str(workspace.root),
        "trace_path": None,
    }


def _append_fatal_error(result: dict[str, Any], exc: Exception) -> dict[str, Any]:
    updated = dict(result)
    errors = [str(item) for item in updated.get("errors") or []]
    errors.append(f"{type(exc).__name__}: {exc}")
    updated.update({"success": False, "fatal": True, "errors": errors})
    return updated


__all__ = ["SqlTrainingCapability", "SqlTrainingConfigurationError"]
