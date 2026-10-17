from __future__ import annotations

import hashlib
import inspect
import json
import re
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager, ExitStack
from copy import deepcopy
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from cortex.capabilities.sql_training.artifacts import Workspace, write_jsonl
from cortex.capabilities.sql_training.artifacts.jsonl import write_json, write_json_atomic
from cortex.capabilities.sql_training.artifacts.workspace import (
    ASSET_GROUP_FILES,
    CANDIDATE_GROUP_FILES,
    serialize_asset_envelope,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.error_analysis.workflow import ErrorAnalysisWorkflow
from cortex.capabilities.sql_training.extractors import (
    GenerationRuleExtractor,
)
from cortex.capabilities.sql_training.extractors.parallel import is_retryable_llm_error
from cortex.capabilities.sql_training.extractors.sql_normalizer import (
    build_column_table_map,
    collect_physical_column_references,
    normalize_sql_aliases,
)
from cortex.capabilities.sql_training.graph.case import project_case_edges
from cortex.capabilities.sql_training.graph.join_edges import (
    build_gold_join_edges_from_llm_proposals,
    join_edge_key,
    merge_join_edges,
    pure_routing_paths,
)
from cortex.capabilities.sql_training.graph.relationships import compile_all_relationships
from cortex.capabilities.sql_training.graph.retrieval import (
    CaseRetrievalGraphBuilder,
    CaseRetrievalGraphError,
)
from cortex.capabilities.sql_training.llm import LLMProvider
from cortex.capabilities.sql_training.llm.prompts import load_prompt
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.pipeline.compile import CompilePipeline
from cortex.capabilities.sql_training.pipeline.execution import (
    GoldQueryCache,
    evaluate_record,
    generate_sql,
)
from cortex.capabilities.sql_training.pipeline.prepare import PrepareQASQLPipeline
from cortex.capabilities.sql_training.prepare import load_standard_qa_sql_records
from cortex.capabilities.sql_training.runtime_config import CortexConfig
from cortex.capabilities.sql_training.training.initial.bootstrap import (
    TrainingDimensionValueBootstrap,
)
from cortex.capabilities.sql_training.training.initial.flow import run_initial_flow
from cortex.capabilities.sql_training.training.optimizers.dispatch import OptimizerDispatcher
from cortex.capabilities.sql_training.training.optimizers.generation_rule import (
    matched_ontology_ids,
)
from cortex.capabilities.sql_training.training.repair.assessment import (
    assess_residual_joint_trial,
)
from cortex.capabilities.sql_training.training.repair.contracts import (
    OptimizationOwner,
    ontology_fingerprint,
)
from cortex.capabilities.sql_training.training.repair.cycle import RepairCycle
from cortex.capabilities.sql_training.training.repair.plan import (
    build_optimization_plan,
)
from cortex.capabilities.sql_training.training.repair.trial import (
    CaseWorkingOntology,
)
from cortex.capabilities.sql_training.training.replay import (
    ReplayAttempt,
    StableReplayResult,
    run_stable_replay,
)
from cortex.capabilities.sql_training.training.trace import (
    TracingLLMProvider,
    TrainTraceLogger,
    sanitize_trace_payload,
)
from cortex.capabilities.sql_training.validation.models import GenerationResponse
from cortex.infrastructure.database import DatabaseConnector
from cortex.schema import GraphEdge, GraphSpace, SchemaDefinition, load_root_schema

_TRAIN_GR_EXTRACTORS = [GenerationRuleExtractor()]
_SEMANTIC_TYPES = (
    NodeType.METRIC,
    NodeType.SEMANTIC_MAPPING,
    NodeType.GENERATION_RULE,
)


class TrainPipeline:
    def __init__(
        self,
        config: CortexConfig,
        *,
        llm_provider: LLMProvider | None = None,
        validation_client: Any | None = None,
        validation_client_factory: Callable[[], Any] | None = None,
        case_resources_factory: Callable[[], AbstractContextManager[tuple[LLMProvider, Any]]]
        | None = None,
        database: DatabaseConnector | None = None,
        schema: SchemaDefinition | None = None,
        existing_join_edges: tuple[GraphEdge, ...] = (),
    ):
        self.config = config
        self.llm_provider = llm_provider
        self.validation_client = validation_client
        self.validation_client_factory = validation_client_factory
        self.case_resources_factory = case_resources_factory
        self._schema = schema or load_root_schema()
        self._progress_lock = Lock()
        self.workspace = Workspace(config.workspace)
        self.database = database
        self._existing_join_edges = existing_join_edges
        self._last_intent_output: dict[str, Any] | None = None
        self._last_selected_ontology_ids: set[str] | None = None
        self._case_staged_semantic_assets: list[NodeAsset] | None = None
        self._case_initial_metric_candidate_sets: list[dict[str, Any]] | None = None
        self._gold_query_cache: GoldQueryCache | None = GoldQueryCache()
        self._retrieval_graph_builder = CaseRetrievalGraphBuilder(
            self.workspace,
            self._schema,
        )

    def run(
        self,
        *,
        qa_path: str | Path,
        dataset: str,
        max_rounds: int | None = None,
        max_iterations: int | None = None,
        sql_timeout: float | None = None,
        trace_prefix: str | None = None,
    ) -> dict[str, Any]:
        run_id = f"train_{uuid4().hex}"
        self._gold_query_cache = GoldQueryCache()
        max_iter = (
            max_iterations if max_iterations is not None else self.config.train.max_iterations
        )
        max_round = max_rounds if max_rounds is not None else self.config.train.max_rounds
        timeout = sql_timeout if sql_timeout is not None else self.config.train.sql_timeout
        trace_prefix = trace_prefix or run_id
        errors: list[str] = []
        dialect_error = _dialect_configuration_error(self.config)
        warnings: list[str] = []
        trace_logger = TrainTraceLogger(
            self.workspace.root,
            run_id,
            level=self.config.train.trace_level,
        )
        trace_logger.log(
            source_id=None,
            step="train_start",
            input={
                "qa_path": str(qa_path),
                "dataset": dataset,
                "max_rounds": max_round,
                "max_iterations": max_iter,
                "sql_timeout": timeout,
                "required_consecutive_correct_replays": (
                    self.config.train.required_consecutive_correct_replays
                ),
                "trace_prefix": trace_prefix,
                "database_dialect": self.config.database.dialect,
                "validation_dialect": self.config.validation.dialect,
                "retrieval_backend": "graph_index",
            },
            output={"run_id": run_id, "workspace": str(self.workspace.root)},
            summary={"run_id": run_id, "warnings": warnings},
        )
        if dialect_error:
            trace_logger.log(
                source_id=None,
                step="train_configuration_error",
                input={
                    "database_dialect": self.config.database.dialect,
                    "validation_dialect": self.config.validation.dialect,
                },
                output={"error": dialect_error},
                summary={"error": dialect_error},
            )
            return _failure(run_id, dialect_error, trace_path=trace_logger.path)
        if not self.config.database.url:
            message = "database.url is required for node train execution evaluation"
            trace_logger.log(
                source_id=None,
                step="train_configuration_error",
                input={"database_url_configured": False},
                output={"error": message},
                summary={"error": message},
            )
            return _failure(run_id, message, trace_path=trace_logger.path)

        provider = self.llm_provider
        if provider is None:
            message = "llm provider is required for node train"
            trace_logger.log(
                source_id=None,
                step="train_configuration_error",
                input={"llm_provider": self.config.llm.provider},
                output={"error": message},
                summary={"error": message},
            )
            return _failure(run_id, message, trace_path=trace_logger.path)
        provider = TracingLLMProvider(provider, trace_logger)

        try:
            prepared = PrepareQASQLPipeline(self.config).run(qa_path=qa_path, dataset=dataset)
        except Exception as exc:
            message = f"train QA preparation failed: {exc}"
            trace_logger.log(
                source_id=None,
                step="prepare_qa_error",
                input={"qa_path": str(qa_path), "dataset": dataset},
                output={"error_type": type(exc).__name__, "error": str(exc)},
                summary={"error": message},
            )
            return _failure(run_id, message, trace_path=trace_logger.path)
        prepared_path = prepared["output_path"]
        records = [record.to_record() for record in load_standard_qa_sql_records(prepared_path)]
        records = [self._add_gold_column_metadata(record) for record in records]
        trace_logger.log(
            source_id=None,
            step="prepare_qa",
            input={"qa_path": str(qa_path), "dataset": dataset},
            output=prepared,
            summary={
                "qa_records": prepared.get("qa_records"),
                "output_path": prepared.get("output_path"),
            },
        )

        try:
            self._prepare_gold_join_edges(records, provider, trace_logger)
        except Exception as exc:
            message = f"train Gold join edge preparation failed: {exc}"
            trace_logger.log(
                source_id=None,
                step="gold_join_edge_preparation_error",
                phase="train_start",
                output={"error_type": type(exc).__name__, "error": str(exc)},
                summary={"error": message},
            )
            return _failure(run_id, message, trace_path=trace_logger.path)

        try:
            dimension_value_bootstrap = self._bootstrap_dimension_values(records, run_id)
            trace_logger.log(
                source_id=None,
                step="dimension_value_bootstrap",
                phase="train_start",
                input={"record_count": len(records)},
                output={
                    **dimension_value_bootstrap,
                },
                summary={
                    "selected_column_count": len(
                        dimension_value_bootstrap.get("selected_column_refs", [])
                    ),
                    "dimension_value_count": dimension_value_bootstrap.get("dimension_value_count"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            message = f"train Dimension Value bootstrap failed: {exc}"
            trace_logger.log(
                source_id=None,
                step="dimension_value_bootstrap_error",
                phase="train_start",
                output={"error_type": type(exc).__name__, "error": str(exc)},
                summary={"error": message},
            )
            return _failure(run_id, message, trace_path=trace_logger.path)

        if self.config.train.case_max_workers is not None:
            return self._run_parallel_cases(
                records=records,
                prepared=prepared,
                dimension_value_bootstrap=dimension_value_bootstrap,
                run_id=run_id,
                max_rounds=max_round,
                max_iterations=max_iter,
                sql_timeout=timeout,
                trace_prefix=trace_prefix,
                trace_logger=trace_logger,
            )

        sql_correct_cases = 0
        ontology_updated_cases = 0
        generation_rule_updated_cases = 0
        metric_updated_cases = 0
        optimization_trial_cases = 0
        failed_optimization_cases = 0
        accepted_generation_rule_count = 0
        accepted_metric_count = 0
        case_results: list[dict[str, Any]] = []
        compile_results: list[dict[str, Any]] = []

        def build_result() -> dict[str, Any]:
            case_count = len(case_results)
            return sanitize_trace_payload(
                {
                    "success": (
                        not errors
                        and case_count == len(records)
                        and sql_correct_cases == case_count
                        and failed_optimization_cases == 0
                    ),
                    "run_id": run_id,
                    "prepared": prepared,
                    "dimension_value_bootstrap": dimension_value_bootstrap,
                    "compile": {"cases": compile_results},
                    "cases": case_results,
                    "case_draft_nodes": [
                        {
                            "source_id": case.get("source_id"),
                            "draft_nodes": case.get("draft_nodes", []),
                        }
                        for case in case_results
                    ],
                    "sql_correct_cases": sql_correct_cases,
                    "ontology_updated_cases": ontology_updated_cases,
                    "generation_rule_updated_cases": generation_rule_updated_cases,
                    "metric_updated_cases": metric_updated_cases,
                    "optimization_trial_cases": optimization_trial_cases,
                    "failed_optimization_cases": failed_optimization_cases,
                    "accepted_generation_rule_count": accepted_generation_rule_count,
                    "accepted_metric_count": accepted_metric_count,
                    "accepted_cases": sql_correct_cases,
                    "errors": errors,
                    "warnings": warnings,
                    "workspace": str(self.workspace.root),
                    "trace_path": str(trace_logger.path),
                }
            )

        current_metrics = tuple(self.workspace.read_draft_assets(NodeType.METRIC))
        source_evidence = self.workspace.read_source_evidence()
        extraction_workers = min(self.config.train.extraction_max_workers, len(records))
        trace_logger.log(
            source_id=None,
            step="parallel_case_extraction_start",
            phase="case_setup",
            input={
                "case_source_ids": [str(record["source_id"]) for record in records],
                "max_workers": extraction_workers,
            },
            summary={"case_count": len(records), "max_workers": extraction_workers},
        )
        with ThreadPoolExecutor(
            max_workers=extraction_workers,
            thread_name_prefix="cortex-train-extract",
        ) as executor:
            extraction_futures = {
                executor.submit(
                    self._extract_case_candidates,
                    record,
                    provider=provider,
                    case_index=index,
                    current_metrics=current_metrics,
                    source_evidence=source_evidence,
                    trace_logger=trace_logger,
                ): index - 1
                for index, record in enumerate(records, start=1)
            }
            extraction_results: list[tuple[dict[str, Any], list[NodeAsset]] | Exception | None] = [
                None
            ] * len(records)
            for future in as_completed(extraction_futures):
                extraction_results[extraction_futures[future]] = future.result()
        if any(result is None for result in extraction_results):
            raise RuntimeError("case extraction completed without producing every result")
        trace_logger.log(
            source_id=None,
            step="parallel_case_extraction_complete",
            phase="case_setup",
            output={
                "case_source_ids": [str(record["source_id"]) for record in records],
                "max_workers": extraction_workers,
            },
            summary={"case_count": len(records), "max_workers": extraction_workers},
        )
        # Seal an inspectable extraction artifact for *every* input before case
        # training begins.  In particular, keep the failure record too: an LLM
        # extraction error must not make a QA case disappear from a review run.
        extraction_graph_paths: dict[int, Path] = {}
        for index, (record, extraction) in enumerate(
            zip(records, extraction_results, strict=True), start=1
        ):
            source_id = str(record["source_id"])
            if isinstance(extraction, Exception):
                compile_result: dict[str, Any] | None = None
                staged_assets: list[NodeAsset] = []
                extraction_error = {
                    "error_type": type(extraction).__name__,
                    "error": str(extraction),
                }
            else:
                compile_result, staged_assets = extraction
                extraction_error = None
            extraction_graph_paths[index] = _write_extraction_graph_artifact(
                self.workspace,
                run_id=run_id,
                case_index=index,
                source_id=source_id,
                candidates=staged_assets,
                compile_result=compile_result,
                extraction_error=extraction_error,
            )
        trace_logger.log(
            source_id=None,
            step="parallel_case_extraction_artifacts_sealed",
            phase="case_setup",
            output={
                "artifacts": {
                    str(records[index - 1]["source_id"]): str(path)
                    for index, path in extraction_graph_paths.items()
                }
            },
            summary={"artifact_count": len(extraction_graph_paths)},
        )

        for index, (record, extraction) in enumerate(
            zip(records, extraction_results, strict=True), start=1
        ):
            source_id = str(record["source_id"])
            self._case_staged_semantic_assets = None
            self._case_initial_metric_candidate_sets = None
            self.workspace.init()
            provider.set_context(source_id=source_id, case_index=index, phase="case_setup")
            trace_logger.log(
                source_id=source_id,
                case_index=index,
                step="case_transaction_start",
                phase="case_setup",
                input={"source_id": source_id},
                output={"accepted_working_asset_ids": []},
                summary={"base_ontology_fingerprint": ontology_fingerprint([])},
            )
            if isinstance(extraction, Exception):
                exc = extraction
                message = f"train case extraction failed for {source_id}: {exc}"
                retryable = is_retryable_llm_error(exc)
                case_local_contract_failure = _is_case_local_extraction_contract_failure(exc)
                trace_logger.log(
                    source_id=source_id,
                    case_index=index,
                    step="case_setup_error",
                    input={"record": record, "stage": "compile_extract"},
                    output={
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "retryable": retryable,
                        "case_local_contract_failure": case_local_contract_failure,
                    },
                    summary={"error": message, "stage": "compile_extract"},
                )
                if retryable or case_local_contract_failure:
                    self._case_staged_semantic_assets = None
                    self._case_initial_metric_candidate_sets = None
                    failure_kind = (
                        "retryable_llm_error" if retryable else "extract_contract_failure"
                    )
                    case_result = _case_setup_failure(
                        record,
                        message,
                        stage="compile_extract",
                        failure_kind=failure_kind,
                    )
                    case_results.append(case_result)
                    _write_case_progress(
                        self.workspace,
                        run_id=run_id,
                        case_index=index,
                        result=case_result,
                        extraction_graph_path=extraction_graph_paths[index],
                    )
                    errors.append(message)
                    trace_logger.log(
                        source_id=source_id,
                        case_index=index,
                        step="case_complete",
                        phase="case_setup",
                        input={"record": record},
                        output=case_result,
                        summary={
                            "fatal": True,
                            "stage": "compile_extract",
                            "failure_kind": failure_kind,
                        },
                    )
                    continue
                self._case_staged_semantic_assets = None
                self._case_initial_metric_candidate_sets = None
                errors.append(message)
                result = build_result()
                trace_logger.log(
                    source_id=None,
                    step="train_aborted",
                    output=result,
                    summary={"error": message, "case_count": len(case_results)},
                )
                return result

            compile_result, staged_assets = extraction
            compile_results.append({"source_id": source_id, **compile_result})
            self._case_staged_semantic_assets = staged_assets
            self._case_initial_metric_candidate_sets = list(
                compile_result.get("initial_metric_candidate_sets") or []
            )
            extraction_graph_path = extraction_graph_paths[index]
            trace_logger.log(
                source_id=source_id,
                case_index=index,
                step="case_staging_sealed",
                phase="case_setup",
                input={
                    "staged_asset_ids": [asset.id for asset in self._case_staged_semantic_assets],
                },
                output={
                    "accepted_working_asset_ids": [],
                    "extraction_graph_artifact": str(extraction_graph_path),
                },
                summary={
                    "staged_ontology_fingerprint": ontology_fingerprint(
                        self._case_staged_semantic_assets
                    ),
                    "working_ontology_initialized": False,
                },
            )
            case_result = self._run_case(
                record,
                provider=provider,
                max_rounds=max_round,
                max_iterations=max_iter,
                sql_timeout=timeout,
                trace_prefix=f"{trace_prefix}_{index}",
                prepared_path=prepared_path,
                run_id=run_id,
                case_index=index,
                trace_logger=trace_logger,
            )
            if case_result.get("fatal"):
                _clear_rolled_back_case_commit_result(case_result)
                trace_logger.log(
                    source_id=source_id,
                    case_index=index,
                    step="case_transaction_rollback",
                    phase="case_complete",
                    input={"reason": case_result.get("reason")},
                    output={"rollback_error": None},
                    summary={"restored": True},
                )
            elif "transaction_rolled_back" not in case_result:
                case_result["transaction_rolled_back"] = False
            self._case_staged_semantic_assets = None
            self._case_initial_metric_candidate_sets = None
            case_results.append(case_result)
            _write_case_progress(
                self.workspace,
                run_id=run_id,
                case_index=index,
                result=case_result,
                extraction_graph_path=extraction_graph_path,
            )
            if case_result["correct"]:
                sql_correct_cases += 1
            if case_result.get("ontology_updated"):
                ontology_updated_cases += 1
            accepted_rule_ids = case_result.get("accepted_rule_ids") or []
            if accepted_rule_ids:
                generation_rule_updated_cases += 1
                accepted_generation_rule_count += len(accepted_rule_ids)
            accepted_metric_ids = case_result.get("accepted_metric_ids") or []
            optimized_metric_ids = case_result.get("optimized_metric_ids") or []
            if optimized_metric_ids:
                metric_updated_cases += 1
            if accepted_metric_ids:
                accepted_metric_count += len(accepted_metric_ids)
            if any(event.get("round") for event in case_result.get("events") or []):
                optimization_trial_cases += 1
            case_failed = bool(case_result.get("failed_closed")) or not bool(
                case_result.get("correct")
            )
            if case_failed:
                failed_optimization_cases += 1
                reason = str(case_result.get("reason") or "case SQL not correct")
                errors.append(f"{source_id}: {reason}")

        # Semantic integration and Graph publication intentionally remain outside
        # def train. Until that workflow is designed, validated case bundles are
        # the only formal semantic output of this stage.
        result = build_result()
        trace_logger.log(
            source_id=None,
            step="train_complete",
            input={"case_source_ids": [case.get("source_id") for case in case_results]},
            output=result,
            summary={
                "success": result["success"],
                "sql_correct_cases": sql_correct_cases,
                "ontology_updated_cases": ontology_updated_cases,
                "accepted_generation_rule_count": accepted_generation_rule_count,
                "accepted_metric_count": accepted_metric_count,
                "case_count": len(case_results),
                "errors": errors,
            },
        )
        return result

    def _run_parallel_cases(
        self,
        *,
        records: list[dict[str, Any]],
        prepared: dict[str, Any],
        dimension_value_bootstrap: dict[str, Any],
        run_id: str,
        max_rounds: int,
        max_iterations: int,
        sql_timeout: float,
        trace_prefix: str,
        trace_logger: TrainTraceLogger,
    ) -> dict[str, Any]:
        """Schedule complete case transactions; only this coordinator aggregates results."""
        started = perf_counter()
        if (
            self.validation_client is not None
            and self.validation_client_factory is None
            and self.case_resources_factory is None
        ):
            raise ValueError(
                "Complete Case concurrency requires validation_client_factory "
                "when injecting a validation client"
            )
        workers = min(self.config.train.case_max_workers, len(records))
        metrics = tuple(self.workspace.read_draft_assets(NodeType.METRIC))
        evidence = self.workspace.read_source_evidence()
        trace_logger.log(
            source_id=None,
            step="parallel_cases_start",
            summary={"case_count": len(records), "max_workers": workers},
        )

        def run_one(item):
            index, record = item
            return self._run_isolated_case(
                record,
                case_index=index,
                current_metrics=metrics,
                source_evidence=evidence,
                run_id=run_id,
                max_rounds=max_rounds,
                max_iterations=max_iterations,
                sql_timeout=sql_timeout,
                trace_prefix=trace_prefix,
                prepared_path=prepared["output_path"],
                trace_logger=trace_logger,
            )

        if records:
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="cortex-train-case",
            ) as executor:
                # map yields in input order; each worker seals progress before returning.
                outcomes = list(executor.map(run_one, enumerate(records, start=1)))
        else:
            outcomes = []
        cases = [case for _, case in outcomes]
        errors = [
            f"{case.get('source_id')}: {case.get('reason') or 'case SQL not correct'}"
            for case in cases
            if case.get("failed_closed") or not case.get("correct")
        ]
        correct = sum(bool(case.get("correct")) for case in cases)
        result = sanitize_trace_payload(
            {
                "success": not errors and correct == len(records),
                "run_id": run_id,
                "prepared": prepared,
                "dimension_value_bootstrap": dimension_value_bootstrap,
                "compile": {
                    "cases": [
                        {"source_id": record["source_id"], **compiled}
                        for record, (compiled, _) in zip(records, outcomes, strict=True)
                        if compiled is not None
                    ]
                },
                "cases": cases,
                "case_draft_nodes": [
                    {"source_id": case.get("source_id"), "draft_nodes": case.get("draft_nodes", [])}
                    for case in cases
                ],
                "sql_correct_cases": correct,
                "ontology_updated_cases": sum(bool(c.get("ontology_updated")) for c in cases),
                "generation_rule_updated_cases": sum(
                    bool(c.get("accepted_rule_ids")) for c in cases
                ),
                "metric_updated_cases": sum(bool(c.get("optimized_metric_ids")) for c in cases),
                "optimization_trial_cases": sum(
                    any(event.get("round") for event in c.get("events", [])) for c in cases
                ),
                "failed_optimization_cases": len(errors),
                "accepted_generation_rule_count": sum(
                    len(c.get("accepted_rule_ids") or []) for c in cases
                ),
                "accepted_metric_count": sum(
                    len(c.get("accepted_metric_ids") or []) for c in cases
                ),
                "accepted_cases": correct,
                "errors": errors,
                "warnings": [],
                "workspace": str(self.workspace.root),
                "trace_path": str(trace_logger.path),
            }
        )
        trace_logger.log(
            source_id=None,
            step="train_complete",
            output=result,
            summary={
                "case_count": len(cases),
                "sql_correct_cases": correct,
                "errors": errors,
                "elapsed_seconds": round(perf_counter() - started, 3),
            },
        )
        return result

    def _run_isolated_case(
        self,
        record: dict[str, Any],
        *,
        case_index: int,
        current_metrics: tuple[NodeAsset, ...],
        source_evidence: list[dict[str, Any]],
        run_id: str,
        max_rounds: int,
        max_iterations: int,
        sql_timeout: float,
        trace_prefix: str,
        prepared_path: str | Path,
        trace_logger: TrainTraceLogger,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Own all mutable learning and tracing state for one Case, including extraction."""
        started = perf_counter()
        source_id = str(record["source_id"])
        compiled = None
        artifact = None
        resources = ExitStack()
        try:
            if self.case_resources_factory is not None:
                case_provider, validation_client = resources.enter_context(
                    self.case_resources_factory()
                )
            else:
                case_provider = self.llm_provider
                validation_client = (
                    self.validation_client_factory() if self.validation_client_factory else None
                )
            provider = TracingLLMProvider(case_provider, trace_logger)
            provider.set_context(source_id=source_id, case_index=case_index, phase="case_setup")
            runner = type(self)(
                self.config,
                llm_provider=case_provider,
                validation_client=validation_client,
                database=self.database,
                schema=self._schema,
            )
            trace_logger.log(
                source_id=source_id,
                case_index=case_index,
                step="case_transaction_start",
                phase="case_setup",
                output={"accepted_working_asset_ids": []},
                summary={"base_ontology_fingerprint": ontology_fingerprint([])},
            )
            extraction = runner._extract_case_candidates(
                deepcopy(record),
                provider=provider,
                case_index=case_index,
                current_metrics=deepcopy(current_metrics),
                source_evidence=deepcopy(source_evidence),
                trace_logger=trace_logger,
            )
            if isinstance(extraction, Exception):
                raise extraction
            compiled, staged = extraction
            artifact = _write_extraction_graph_artifact(
                self.workspace,
                run_id=run_id,
                case_index=case_index,
                source_id=source_id,
                candidates=staged,
                compile_result=compiled,
                extraction_error=None,
            )
            runner._case_staged_semantic_assets = staged
            runner._case_initial_metric_candidate_sets = list(
                compiled.get("initial_metric_candidate_sets") or []
            )
            trace_logger.log(
                source_id=source_id,
                case_index=case_index,
                step="case_staging_sealed",
                phase="case_setup",
                output={"extraction_graph_artifact": str(artifact)},
            )
            result = runner._run_case(
                deepcopy(record),
                provider=provider,
                max_rounds=max_rounds,
                max_iterations=max_iterations,
                sql_timeout=sql_timeout,
                trace_prefix=f"{trace_prefix}_{case_index}",
                prepared_path=prepared_path,
                run_id=run_id,
                case_index=case_index,
                trace_logger=trace_logger,
            )
        except Exception as exc:
            stage = "compile_extract" if compiled is None else "case_training"
            result = _case_setup_failure(
                record,
                f"train case {source_id} failed: {exc}",
                stage=stage,
                failure_kind="case_execution_error",
            )
            if artifact is None:
                artifact = _write_extraction_graph_artifact(
                    self.workspace,
                    run_id=run_id,
                    case_index=case_index,
                    source_id=source_id,
                    candidates=[],
                    compile_result=compiled,
                    extraction_error={"error_type": type(exc).__name__, "error": str(exc)},
                )
        finally:
            resources.close()
        if result.get("fatal"):
            _clear_rolled_back_case_commit_result(result)
        else:
            result.setdefault("transaction_rolled_back", False)
        with self._progress_lock:
            _write_case_progress(
                self.workspace,
                run_id=run_id,
                case_index=case_index,
                result=result,
                extraction_graph_path=artifact,
            )
        trace_logger.log(
            source_id=source_id,
            case_index=case_index,
            step="case_complete",
            phase="case_complete",
            output=result,
            summary={
                "correct": bool(result.get("correct")),
                "fatal": bool(result.get("fatal")),
                "elapsed_seconds": round(perf_counter() - started, 3),
            },
        )
        return compiled, result

    def _add_gold_column_metadata(self, record: dict[str, Any]) -> dict[str, Any]:
        """Attach compact metadata for Columns actually used by the Gold SQL."""

        columns = self.workspace.read_draft_assets(NodeType.COLUMN)
        metadata_by_ref = {
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
            for asset in columns
        }
        column_rows = [
            {
                "column_name": asset.properties.column_name,
                "table_name": metadata_by_ref[asset.id]["table"],
            }
            for asset in columns
        ]
        normalized_sql = normalize_sql_aliases(
            str(record["sql"]),
            column_table_map=build_column_table_map(column_rows),
            dialect=self.config.database.dialect,
        )
        gold_refs = collect_physical_column_references(
            normalized_sql,
            available_column_refs=set(metadata_by_ref),
            dialect=self.config.database.dialect,
        )
        return {
            **record,
            "gold_column_metadata": {
                reference: metadata_by_ref[reference]
                for reference in sorted(gold_refs)
                if reference in metadata_by_ref
            },
        }

    def _bootstrap_dimension_values(self, records: list[dict[str, Any]], run_id: str) -> dict:
        return TrainingDimensionValueBootstrap(
            self.config, self.workspace, self.database,
        ).run(records, run_id=run_id).to_dict()

    def _extract_case_candidates(
        self,
        record: dict[str, Any],
        *,
        provider: TracingLLMProvider,
        case_index: int,
        current_metrics: tuple[NodeAsset, ...],
        source_evidence: list[dict[str, Any]],
        trace_logger: TrainTraceLogger,
    ) -> tuple[dict[str, Any], list[NodeAsset]] | Exception:
        source_id = str(record["source_id"])
        try:
            with tempfile.TemporaryDirectory(
                prefix=f"train_case_{case_index}_",
                dir=str(self.workspace.root),
            ) as staging_root:
                case_workspace = Workspace(staging_root)
                case_workspace.init()
                _copy_non_semantic_draft_assets(self.workspace, case_workspace)
                if source_evidence:
                    case_workspace.write_source_evidence(source_evidence)
                case_config = self.config.model_copy(update={"workspace": staging_root})
                compiler = CompilePipeline(
                    case_config,
                    llm_provider=provider,
                    database=self.database,
                )
                compile_result = compiler.extract_qa_records(
                    [record],
                    replace=True,
                    current_metrics=current_metrics,
                    llm_context={
                        "case_index": case_index,
                        "phase": "case_setup",
                    },
                )
                staged_assets = _read_semantic_candidates(case_workspace)
                self._merge_case_dimension_values(case_workspace, self.workspace)
                trace_logger.log(
                    source_id=source_id,
                    case_index=case_index,
                    step="compile_extract",
                    phase="case_setup",
                    input={"record": record, "replace": True},
                    output=compile_result,
                    summary={
                        "candidate_counts": compile_result.get("candidate_counts", {}),
                        "warnings": len(compile_result.get("warnings") or []),
                    },
                )
                return compile_result, staged_assets
        except Exception as exc:  # fail in deterministic input order in run()
            return exc

    def _prepare_gold_join_edges(
        self,
        records: list[dict[str, Any]],
        provider: TracingLLMProvider,
        trace_logger: TrainTraceLogger,
    ) -> None:
        if not getattr(provider.provider, "supports_join_edge_extraction", False):
            return
        table_columns: dict[str, set[str]] = {}
        for asset in self.workspace.read_draft_assets(NodeType.COLUMN):
            for reference in asset.properties.references:
                if reference.startswith("table."):
                    table_columns.setdefault(reference.removeprefix("table."), set()).add(
                        asset.properties.column_name
                    )
        existing = merge_join_edges(
            self._existing_join_edges,
            self.workspace.read_catalog_edges(),
        )
        proposed_edges: list[GraphEdge] = []
        case_edges: list[list[GraphEdge]] = []
        for index, record in enumerate(records, start=1):
            source_id = str(record["source_id"])
            provider.set_context(
                source_id=source_id, case_index=index, phase="gold_join_edge_preparation"
            )
            proposals = provider.extract_assets(
                {
                    "source_id": source_id,
                    "sql": record["sql"],
                    "database_dialect": self.config.database.dialect,
                    "table_columns": {
                        table: sorted(columns) for table, columns in table_columns.items()
                    },
                },
                context={
                    "prompt_name": "gold_join_edges",
                    "prompt": load_prompt("gold_join_edges"),
                },
            )
            proposals, dropped_proposals = _drop_self_gold_join_proposals(
                proposals,
                table_columns=table_columns,
            )
            if dropped_proposals:
                trace_logger.log(
                    source_id=source_id,
                    case_index=index,
                    step="gold_join_edges_dropped",
                    phase="gold_join_edge_preparation",
                    output={"dropped": dropped_proposals},
                    summary={
                        "dropped_count": len(dropped_proposals),
                        "reasons": sorted(
                            {str(item["reason"]) for item in dropped_proposals}
                        ),
                    },
                )
            edges = build_gold_join_edges_from_llm_proposals(
                proposals=proposals,
                source_id=source_id,
                table_columns=table_columns,
            )
            proposed_edges.extend(edges)
            case_edges.append(edges)
            trace_logger.log(
                source_id=source_id,
                case_index=index,
                step="gold_join_edges_validated",
                phase="gold_join_edge_preparation",
                output={"edge_ids": [edge.id for edge in edges]},
                summary={"edge_count": len(edges)},
            )
        merged = merge_join_edges(existing, proposed_edges)
        merged_by_key = {join_edge_key(edge): edge for edge in merged}
        for record, extracted in zip(records, case_edges, strict=True):
            canonical_edges = [merged_by_key[join_edge_key(edge)] for edge in extracted]
            record["routing_join_paths"] = pure_routing_paths(
                sql=str(record["sql"]),
                dialect=self.config.database.dialect,
                case_edges=canonical_edges,
            )
            endpoint_pairs = {
                frozenset(path["semantic_tables"]) for path in record["routing_join_paths"]
            }
            record["direct_join_alternatives"] = [
                {
                    "edge_id": edge.id,
                    "tables": [edge.from_node_id, edge.to_node_id],
                    "join_type": edge.properties.get("default_join_type"),
                    "conditions": edge.properties.get("conditions"),
                }
                for edge in merged
                if frozenset({edge.from_node_id, edge.to_node_id}) in endpoint_pairs
            ]
        self.workspace.write_catalog_edges(merged)
        trace_logger.log(
            source_id=None,
            step="gold_join_edges_published_to_training_graph",
            phase="train_start",
            output={"edge_ids": [edge.id for edge in merged]},
            summary={
                "catalog_edge_count": len(existing),
                "gold_proposal_count": len(proposed_edges),
                "merged_edge_count": len(merged),
            },
        )

    def _run_case(
        self,
        record: dict[str, Any],
        *,
        provider: LLMProvider,
        max_iterations: int,
        sql_timeout: float,
        trace_prefix: str,
        prepared_path: str | Path,
        run_id: str,
        case_index: int,
        trace_logger: TrainTraceLogger,
        max_rounds: int = 1,
    ) -> dict[str, Any]:
        try:
            return self._run_case_joint(
                record,
                provider=provider,
                max_iterations=max_iterations,
                sql_timeout=sql_timeout,
                trace_prefix=trace_prefix,
                prepared_path=prepared_path,
                run_id=run_id,
                case_index=case_index,
                trace_logger=trace_logger,
                max_rounds=max_rounds,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed at the case boundary
            trace_logger.log(
                source_id=str(record["source_id"]),
                case_index=case_index,
                step="joint_optimization_error",
                phase="joint_failure",
                input={"record": record},
                output={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                summary={"error": str(exc)},
            )
            result = _joint_case_result(
                record,
                {"correct": False, "reason": str(exc)},
                [],
                failed_closed=True,
                failure_reason=f"joint_optimization_error: {exc}",
            )
            result["fatal"] = True
            return result

    def _run_case_joint(
        self,
        record: dict[str, Any],
        *,
        provider: LLMProvider,
        max_iterations: int,
        sql_timeout: float,
        trace_prefix: str,
        prepared_path: str | Path,
        run_id: str,
        case_index: int,
        trace_logger: TrainTraceLogger,
        max_rounds: int = 1,
    ) -> dict[str, Any]:
        """Run the complete Joint case in a private Working Ontology."""

        permanent_workspace = self.workspace
        with tempfile.TemporaryDirectory(
            prefix=f"joint_case_{case_index}_",
            dir=str(permanent_workspace.root),
        ) as tmp_dir:
            working_workspace = Workspace(tmp_dir)
            working_workspace.init()
            _copy_non_semantic_draft_assets(permanent_workspace, working_workspace)
            self.workspace = working_workspace
            try:
                try:
                    result = self._run_case_joint_working(
                        record,
                        provider=provider,
                        max_iterations=max_iterations,
                        sql_timeout=sql_timeout,
                        trace_prefix=trace_prefix,
                        prepared_path=prepared_path,
                        run_id=run_id,
                        case_index=case_index,
                        trace_logger=trace_logger,
                        max_rounds=max_rounds,
                    )
                except Exception as exc:
                    trace_logger.log(
                        source_id=str(record["source_id"]),
                        case_index=case_index,
                        step="joint_optimization_error",
                        phase="joint_failure",
                        input={"record": record},
                        output={"error_type": type(exc).__name__, "error": str(exc)},
                        summary={"error": str(exc)},
                    )
                    result = _joint_case_result(
                        record,
                        {"correct": False, "reason": str(exc)},
                        [],
                        failed_closed=True,
                        failure_reason=f"internal_error: {exc}",
                    )
                    result["fatal"] = True
                    result["operational_error"] = True
                final_assets = _read_semantic_draft_assets(working_workspace)
            finally:
                self.workspace = permanent_workspace
            self._merge_case_dimension_values(working_workspace, permanent_workspace)
            accepted_ids = {
                asset_id
                for key in (
                    "accepted_semantic_mapping_ids",
                    "accepted_metric_ids",
                    "accepted_rule_ids",
                )
                for asset_id in result.get(key, [])
            }
            final_assets = [asset for asset in final_assets if asset.id in accepted_ids]
            final_fingerprint = ontology_fingerprint(final_assets)
            result["final_materialized_ontology_fingerprint"] = final_fingerprint
            result["final_ontology_fingerprint"] = final_fingerprint
            result["draft_nodes"] = [serialize_asset_envelope(asset) for asset in final_assets]
            result["draft_node_ids"] = [asset.id for asset in final_assets]
            result["draft_node_count"] = len(final_assets)
            draft_edges = project_case_edges(final_assets)
            result["draft_edge_ids"] = [edge.id for edge in draft_edges]
            result["draft_edge_count"] = len(draft_edges)
            if final_assets and result.get("correct") and not result.get("failed_closed"):
                result["asset_outcome"] = "ready"
            elif final_assets:
                result["asset_outcome"] = "partial"
            else:
                result["asset_outcome"] = "empty"
            result["ontology_updated"] = False
            result["draft_nodes_generated"] = bool(final_assets)
            result["completion_reason"] = _completion_reason(result)
            result["bundle_path"] = str(
                _write_case_bundle(
                    permanent_workspace,
                    run_id=run_id,
                    case_index=case_index,
                    record=record,
                    result=result,
                    assets=final_assets,
                    edges=draft_edges,
                )
            )
            trace_logger.log(
                source_id=str(record["source_id"]),
                case_index=case_index,
                step="case_draft_nodes_sealed",
                phase="case_complete",
                input={"final_ontology_fingerprint": final_fingerprint},
                output={"draft_node_ids": result["draft_node_ids"]},
                summary={"draft_node_count": len(final_assets)},
            )
            return result

    def _merge_case_dimension_values(
        self,
        source: Workspace,
        target: Workspace,
    ) -> None:
        """Promote DB-verified case-local Dimension Values to the physical workspace."""

        values = source.read_draft_assets(NodeType.DIMENSION_VALUE)
        if not values:
            return
        with self._progress_lock:
            by_id = {
                asset.id: asset for asset in target.read_draft_assets(NodeType.DIMENSION_VALUE)
            }
            changed = False
            for asset in values:
                existing = by_id.get(asset.id)
                if existing is None:
                    by_id[asset.id] = asset
                    changed = True
                    continue
                sources = {
                    (source.source_type.value, source.source_id): source
                    for source in [*existing.sources, *asset.sources]
                }
                merged = asset.model_copy(
                    update={"sources": [sources[key] for key in sorted(sources)]}
                )
                if merged != existing:
                    by_id[asset.id] = merged
                    changed = True
            if changed:
                target.write_draft_assets(NodeType.DIMENSION_VALUE, by_id.values())

    def _run_case_joint_working(
        self,
        record: dict[str, Any],
        *,
        provider: LLMProvider,
        max_iterations: int,
        sql_timeout: float,
        trace_prefix: str,
        prepared_path: str | Path,
        run_id: str,
        case_index: int,
        trace_logger: TrainTraceLogger,
        max_rounds: int = 1,
    ) -> dict[str, Any]:
        """Validate Initial candidates, then repair the accepted Working Ontology."""

        del prepared_path
        source_id = str(record["source_id"])
        staged_assets = [
            asset.model_copy(deep=True)
            for asset in (
                self._case_staged_semantic_assets
                if self._case_staged_semantic_assets is not None
                else _read_semantic_draft_assets(self.workspace)
            )
        ]
        trace_suffix = {
            "initial_current_working_replay": "initial_current_working",
            "initial_induction_replay": "initial_induction",
            "initial_retention_replay": "initial_retention",
        }

        def replay_initial(stage: str, assets: list[NodeAsset]) -> StableReplayResult:
            suffix = trace_suffix[stage]
            return self._run_chatbi_stability_replay(
                record,
                trace_id_prefix=f"{trace_prefix}_{source_id}_{suffix}",
                sql_timeout=sql_timeout,
                source_id=source_id,
                case_index=case_index,
                phase=stage,
                trace_logger=trace_logger,
                retrieval_graph=self._build_retrieval_graph(
                    assets,
                    source_id=source_id,
                    case_index=case_index,
                    phase=stage,
                    trace_logger=trace_logger,
                ),
            )

        initial = run_initial_flow(
            record=record,
            staged_assets=staged_assets,
            provider=provider,
            replay=replay_initial,
            publish_working=lambda assets: _write_semantic_draft_assets(
                self.workspace,
                assets,
            ),
            trace=lambda **payload: trace_logger.log(
                source_id=source_id,
                case_index=case_index,
                **payload,
            ),
            metric_candidate_sets=self._case_initial_metric_candidate_sets,
        )
        accepted_by_id = {asset.id: asset for asset in initial.working_assets}
        accepted_metric_ids = sorted(
            asset_id
            for asset_id in initial.accepted_asset_ids
            if accepted_by_id[asset_id].node_type == NodeType.METRIC
        )
        accepted_mapping_ids = sorted(
            asset_id
            for asset_id in initial.accepted_asset_ids
            if accepted_by_id[asset_id].node_type == NodeType.SEMANTIC_MAPPING
        )
        accepted_rule_ids = sorted(
            asset_id
            for asset_id in initial.accepted_asset_ids
            if accepted_by_id[asset_id].node_type == NodeType.GENERATION_RULE
        )
        if initial.failure_reason is not None:
            return _joint_case_result(
                record,
                initial.evaluation,
                initial.events,
                accepted_metric_ids=accepted_metric_ids,
                accepted_semantic_mapping_ids=accepted_mapping_ids,
                accepted_rule_ids=accepted_rule_ids,
                rejected_asset_ids=initial.rejected_asset_ids,
                deduplicated_asset_ids=initial.deduplicated_asset_ids,
                failed_closed=True,
                failure_reason=initial.failure_reason,
            )
        if not initial.requires_repair:
            return _joint_case_result(
                record,
                initial.evaluation,
                initial.events,
                accepted_metric_ids=accepted_metric_ids,
                accepted_semantic_mapping_ids=accepted_mapping_ids,
                accepted_rule_ids=accepted_rule_ids,
                rejected_asset_ids=initial.rejected_asset_ids,
                deduplicated_asset_ids=initial.deduplicated_asset_ids,
                ontology_updated=bool(initial.accepted_asset_ids),
            )
        return self._run_residual_joint_pipeline(
            record,
            provider=provider,
            initial_evaluation=initial.evaluation,
            max_rounds=max_rounds,
            max_iterations=max_iterations,
            sql_timeout=sql_timeout,
            trace_prefix=trace_prefix,
            source_id=source_id,
            case_index=case_index,
            trace_logger=trace_logger,
            events=initial.events,
            initial_accepted_asset_ids=set(initial.accepted_asset_ids),
            rejected_asset_ids=set(initial.rejected_asset_ids),
            deduplicated_asset_ids=set(initial.deduplicated_asset_ids),
        )

    def _run_residual_joint_pipeline(
        self,
        record: dict[str, Any],
        *,
        provider: LLMProvider,
        initial_evaluation: dict[str, Any],
        max_rounds: int,
        max_iterations: int,
        sql_timeout: float,
        trace_prefix: str,
        source_id: str,
        case_index: int,
        trace_logger: TrainTraceLogger,
        events: list[dict[str, Any]],
        initial_accepted_asset_ids: set[str],
        rejected_asset_ids: set[str],
        deduplicated_asset_ids: set[str],
    ) -> dict[str, Any]:
        working = CaseWorkingOntology(
            assets=[
                asset.model_copy(deep=True) for asset in _read_semantic_draft_assets(self.workspace)
            ],
        )
        dispatcher = OptimizerDispatcher(provider)
        latest_selected_ids: set[str] = set()
        latest_raw_retrieved_ids: set[str] = set()
        reference_date = str(record.get("reference_date") or "").strip() or None
        reference_date_evidence = {
            "status": "supplied" if reference_date else "not_supplied",
            "value": reference_date,
        }

        def analyze_errors(diagnostic_eval, current_working, round_number):
            analysis = self._analyze_error_with_trace(
                record,
                diagnostic_eval,
                provider,
                sql_timeout=sql_timeout,
                trace_logger=trace_logger,
                source_id=source_id,
                case_index=case_index,
                phase=f"joint_round_{round_number}_analysis",
                allow_execution_failure=True,
                working_assets=[_compact_repair_asset(asset) for asset in current_working.assets],
            )
            repair_groups = _joint_optimization_targets_from_analysis(analysis)
            trace_logger.log(
                source_id=source_id,
                case_index=case_index,
                step="residual_heavy_round_review",
                phase=f"joint_round_{round_number}_analysis",
                input={
                    "diagnostic_evaluation": _evaluation_summary(diagnostic_eval),
                    "accepted_working_asset_ids": [asset.id for asset in current_working.assets],
                },
                output={"repair_groups": repair_groups},
                summary={"repair_group_count": len(repair_groups)},
            )
            return repair_groups

        def build_plan(repair_groups, diagnostic_eval, current_working, round_number):
            plan = build_optimization_plan(
                repair_groups=repair_groups,
                working_assets=current_working.assets,
                round_id=f"{source_id}:round:{round_number}",
                diagnostic_sql=str(diagnostic_eval.get("query") or ""),
            )
            trace_logger.log(
                source_id=source_id,
                case_index=case_index,
                step="residual_owner_plan",
                phase=f"joint_round_{round_number}_planning",
                input={"repair_groups": repair_groups},
                output=plan,
                summary={
                    "target_count": len(plan.targets),
                    "routing": "repair_group_owner",
                },
            )
            return plan

        def initialize(group, plan, current_working, accepted_working):
            return dispatcher.for_owner(group.owner).initialize(
                group,
                plan,
                current_working,
                accepted_working=accepted_working,
                evidence={
                    "source_id": source_id,
                    "question": record["question"],
                    "gold_sql": record["sql"],
                    "evidence": record.get("evidence"),
                    "gold_column_metadata": record.get("gold_column_metadata", {}),
                    "reference_date": reference_date,
                    "reference_date_evidence": reference_date_evidence,
                    "diagnostic_sql": plan.diagnostic_sql,
                    "table_schema": self._table_schema_payload(),
                    "database_dialect": self.config.database.dialect,
                },
                max_attempts=self.config.train.max_trial_attempts,
            )

        def execute_trial(snapshot, round_number, iteration):
            phase = f"joint_round_{round_number}_iteration_{iteration}"
            try:
                retrieval_graph = self._build_retrieval_graph(
                    snapshot.assets,
                    source_id=source_id,
                    case_index=case_index,
                    phase=phase,
                    trace_logger=trace_logger,
                )
            except CaseRetrievalGraphError as exc:
                return _ontology_contract_evaluation(exc)
            replay = self._run_chatbi_stability_replay(
                record,
                trace_id_prefix=f"{trace_prefix}_{source_id}_{phase}",
                sql_timeout=sql_timeout,
                source_id=source_id,
                case_index=case_index,
                phase=phase,
                trace_logger=trace_logger,
                retrieval_graph=retrieval_graph,
            )
            latest_selected_ids.clear()
            latest_selected_ids.update(replay.effective_selected_ontology_ids())
            latest_raw_retrieved_ids.clear()
            latest_raw_retrieved_ids.update(replay.effective_raw_retrieved_ontology_ids())
            return replay.terminal_evaluation

        def execute_final(snapshot, round_number):
            phase = f"joint_round_{round_number}_final_candidate"
            try:
                retrieval_graph = self._build_retrieval_graph(
                    snapshot.assets,
                    source_id=source_id,
                    case_index=case_index,
                    phase=phase,
                    trace_logger=trace_logger,
                )
            except CaseRetrievalGraphError as exc:
                return _ontology_contract_evaluation(exc)
            replay = self._run_chatbi_stability_replay(
                record,
                trace_id_prefix=f"{trace_prefix}_{source_id}_{phase}",
                sql_timeout=sql_timeout,
                source_id=source_id,
                case_index=case_index,
                phase=phase,
                trace_logger=trace_logger,
                retrieval_graph=retrieval_graph,
            )
            return replay.terminal_evaluation

        def assess_trial(evaluation, plan, bindings, candidates, accepted_ids):
            assessment_trace: dict[str, Any] = {}
            assessment = assess_residual_joint_trial(
                provider,
                payload={
                    "question": record["question"],
                    "gold_sql": record["sql"],
                    "evidence": record.get("evidence"),
                    "gold_column_metadata": record.get("gold_column_metadata", {}),
                    "reference_date": reference_date,
                    "reference_date_evidence": reference_date_evidence,
                    "current_sql": evaluation.get("query"),
                    "evaluation_summary": _evaluation_summary(evaluation),
                    "retrieved_logical_asset_ids": sorted(latest_selected_ids),
                    "raw_retrieved_asset_ids": sorted(latest_raw_retrieved_ids),
                    "candidate_assets": [
                        candidate.model_dump(mode="json") for candidate in candidates.values()
                    ],
                    "candidate_stage_evidence": [
                        {
                            "candidate_id": candidate_id,
                            "logical_asset_id": binding.logical_asset_id,
                            "raw_retrieved": binding.logical_asset_id in latest_raw_retrieved_ids,
                            "selected": binding.logical_asset_id in latest_selected_ids,
                        }
                        for candidate_id, binding in sorted(bindings.candidates_by_id.items())
                    ],
                },
                plan=plan,
                bindings=bindings,
                reviewed_candidate_ids=set(candidates),
                accepted_candidate_ids=accepted_ids,
                accepted_working_assets=[
                    _compact_repair_asset(asset)
                    for asset in _read_semantic_draft_assets(self.workspace)
                ],
                trace_capture=assessment_trace,
                max_attempts=self.config.train.max_trial_attempts,
            )
            trace_logger.log(
                source_id=source_id,
                case_index=case_index,
                step="residual_joint_assessment",
                phase=plan.round_id,
                input={"evaluation": _evaluation_summary(evaluation)},
                output=assessment,
                prompt=assessment_trace.get("prompt"),
                response=assessment_trace.get("response"),
                summary={
                    "accepted_candidate_ids_before_trial": sorted(accepted_ids),
                },
            )
            return assessment

        def refine_candidate(candidate, assessment, target_assessments, plan):
            owner = {
                NodeType.CONCEPT: OptimizationOwner.SEMANTIC_MAPPING,
                NodeType.METRIC: OptimizationOwner.METRIC,
                NodeType.GENERATION_RULE: OptimizationOwner.GENERATION_RULE,
            }[candidate.asset.node_type]
            return dispatcher.for_owner(owner).refine(
                candidate,
                assessment,
                target_assessments,
                plan,
                evidence={
                    "source_id": source_id,
                    "question": record["question"],
                    "gold_sql": record["sql"],
                    "evidence": record.get("evidence"),
                    "gold_column_metadata": record.get("gold_column_metadata", {}),
                    "table_schema": self._table_schema_payload(),
                    "database_dialect": self.config.database.dialect,
                },
                working=CaseWorkingOntology(
                    assets=[
                        asset.model_copy(deep=True)
                        for asset in _read_semantic_draft_assets(self.workspace)
                    ]
                ),
                max_attempts=self.config.train.max_trial_attempts,
            )

        def persist_accepted_working(accepted_working):
            _write_semantic_draft_assets(
                self.workspace,
                [asset.model_copy(deep=True) for asset in accepted_working.assets],
            )

        runner = RepairCycle(
            analyze_errors=analyze_errors,
            build_plan=build_plan,
            initialize=initialize,
            execute_trial=execute_trial,
            execute_final=execute_final,
            assess_trial=assess_trial,
            refine_candidate=refine_candidate,
            on_working_updated=persist_accepted_working,
        )

        run_result = runner.run(
            initial_evaluation=initial_evaluation,
            working=working,
            max_rounds=max_rounds,
            max_iterations=max_iterations,
        )
        _write_semantic_draft_assets(self.workspace, run_result.working.assets)
        events.append(
            {
                "stage": "repair_cycle",
                "evaluation": run_result.evaluation,
                "rounds_run": run_result.rounds_run,
                "trials_run": run_result.trials_run,
                "candidate_dispositions": [
                    item.model_dump(mode="json") for item in run_result.candidate_dispositions
                ],
                "acceptance_evidence": [
                    item.model_dump(mode="json") for item in run_result.acceptance_evidence
                ],
            }
        )
        assets_by_id = {asset.id: asset for asset in run_result.working.assets}
        accepted_ids = set(initial_accepted_asset_ids) | {
            asset_id
            for item in run_result.acceptance_evidence
            for asset_id in item.composition_asset_ids
        }
        accepted_metric_ids = sorted(
            asset_id
            for asset_id in accepted_ids
            if asset_id in assets_by_id and assets_by_id[asset_id].node_type == NodeType.METRIC
        )
        accepted_mapping_ids = sorted(
            asset_id
            for asset_id in accepted_ids
            if asset_id in assets_by_id and assets_by_id[asset_id].node_type == NodeType.CONCEPT
        )
        accepted_rule_ids = sorted(
            asset_id
            for asset_id in accepted_ids
            if asset_id in assets_by_id
            and assets_by_id[asset_id].node_type == NodeType.GENERATION_RULE
        )
        result = _joint_case_result(
            record,
            run_result.evaluation,
            events,
            accepted_metric_ids=accepted_metric_ids,
            accepted_semantic_mapping_ids=accepted_mapping_ids,
            optimized_metric_ids=accepted_metric_ids,
            accepted_rule_ids=accepted_rule_ids,
            rejected_asset_ids=sorted(rejected_asset_ids - accepted_ids),
            deduplicated_asset_ids=sorted(deduplicated_asset_ids),
            ontology_updated=bool(accepted_ids - initial_accepted_asset_ids),
            failed_closed=False,
            failure_reason=run_result.failure_reason,
            remaining_target_ids=list(run_result.remaining_target_ids),
        )
        result["candidate_dispositions"] = [
            item.model_dump(mode="json") for item in run_result.candidate_dispositions
        ]
        result["acceptance_evidence"] = [
            item.model_dump(mode="json") for item in run_result.acceptance_evidence
        ]
        return result

    def _build_retrieval_graph(
        self,
        semantic_assets: list[NodeAsset],
        *,
        source_id: str,
        case_index: int,
        phase: str,
        trace_logger: TrainTraceLogger,
    ) -> GraphSpace:
        graph = self._retrieval_graph_builder.build(semantic_assets)
        semantic_ids = sorted(asset.id for asset in semantic_assets)
        trace_logger.log(
            source_id=source_id,
            case_index=case_index,
            step="case_retrieval_graph_build",
            phase=phase,
            input={"semantic_asset_ids": semantic_ids},
            output={
                "graph_fingerprint": _retrieval_graph_fingerprint(graph),
                "node_count": len(graph.nodes),
                "edge_count": len(graph.edges),
            },
            summary={
                "semantic_asset_count": len(semantic_ids),
                "node_count": len(graph.nodes),
                "edge_count": len(graph.edges),
            },
        )
        return graph

    def _run_chatbi_stability_replay(
        self,
        record: dict[str, Any],
        *,
        trace_id_prefix: str,
        sql_timeout: float,
        source_id: str | None,
        case_index: int | None,
        phase: str | None,
        trace_logger: TrainTraceLogger | None,
        retrieval_graph: GraphSpace,
    ) -> StableReplayResult:
        required_attempts = self.config.train.required_consecutive_correct_replays

        def run_attempt(attempt_number: int) -> ReplayAttempt:
            trace_id = f"{trace_id_prefix}_stability_attempt_{attempt_number}"
            evaluation = self._call_evaluate_record(
                record,
                trace_id=trace_id,
                sql_timeout=sql_timeout,
                source_id=source_id,
                case_index=case_index,
                phase=phase,
                trace_logger=trace_logger,
                retrieval_graph=retrieval_graph,
                force_generate=True,
            )
            failure = evaluation.get("candidate_failure") or {}
            if failure.get("kind") == "generation" and failure.get("retryable"):
                trace_id = f"{trace_id}_generation_retry"
                evaluation = self._call_evaluate_record(
                    record,
                    trace_id=trace_id,
                    sql_timeout=sql_timeout,
                    source_id=source_id,
                    case_index=case_index,
                    phase=phase,
                    trace_logger=trace_logger,
                    retrieval_graph=retrieval_graph,
                    force_generate=True,
                )
            return ReplayAttempt(
                attempt=attempt_number,
                trace_id=trace_id,
                evaluation=deepcopy(evaluation),
                intent_output=deepcopy(self._last_intent_output),
                selected_ontology_ids=sorted(self._selected_ontology_ids()),
                raw_retrieved_ontology_ids=sorted(
                    _raw_retrieved_ontology_ids(self._last_intent_output)
                ),
            )

        result = run_stable_replay(
            run_attempt,
            required_correct_attempts=required_attempts,
        )
        terminal = result.terminal_attempt
        self._last_intent_output = deepcopy(terminal.intent_output)
        self._last_selected_ontology_ids = set(terminal.selected_ontology_ids)
        if trace_logger is not None:
            trace_logger.log(
                source_id=source_id,
                case_index=case_index,
                step="chatbi_stability_replay",
                phase=phase,
                input={
                    "required_correct_attempts": required_attempts,
                    "graph_fingerprint": _retrieval_graph_fingerprint(retrieval_graph),
                },
                output={
                    "status": result.status.value,
                    "first_failure_attempt": result.first_failure_attempt,
                    "attempts": _stable_replay_attempt_summaries(result),
                },
                summary={
                    "status": result.status.value,
                    "attempts_completed": len(result.attempts),
                    "first_failure_attempt": result.first_failure_attempt,
                },
            )
        return result

    def _call_evaluate_record(
        self,
        record: dict[str, Any],
        *,
        trace_id: str,
        sql_timeout: float,
        source_id: str | None,
        case_index: int | None,
        phase: str | None,
        trace_logger: TrainTraceLogger | None,
        retrieval_graph: GraphSpace,
        force_generate: bool = False,
    ) -> dict[str, Any]:
        self._last_intent_output = None
        self._last_selected_ontology_ids = None
        kwargs: dict[str, Any] = {
            "trace_id": trace_id,
            "sql_timeout": sql_timeout,
        }
        signature = inspect.signature(self._evaluate_record)
        if "retrieval_graph" in signature.parameters:
            kwargs["retrieval_graph"] = retrieval_graph
        if "force_generate" in signature.parameters:
            kwargs["force_generate"] = force_generate
        if "trace_logger" in signature.parameters:
            kwargs.update(
                {
                    "source_id": source_id,
                    "case_index": case_index,
                    "phase": phase,
                    "trace_logger": trace_logger,
                }
            )
        return self._evaluate_record(record, **kwargs)

    def _evaluate_record(
        self,
        record: dict[str, Any],
        *,
        trace_id: str,
        sql_timeout: float,
        source_id: str | None = None,
        case_index: int | None = None,
        phase: str | None = None,
        trace_logger: TrainTraceLogger | None = None,
        retrieval_graph: GraphSpace,
        force_generate: bool = False,
    ) -> dict[str, Any]:
        return evaluate_record(
            self.config,
            record,
            generate_sql=self._generate_sql,
            trace_id=trace_id,
            sql_timeout=sql_timeout,
            source_id=source_id,
            case_index=case_index,
            phase=phase,
            trace_logger=trace_logger,
            retrieval_graph=retrieval_graph,
            force_generate=force_generate,
            gold_cache=self._gold_query_cache,
        )

    def _generate_sql(
        self,
        question: str,
        *,
        trace_id: str,
        source_id: str | None = None,
        case_index: int | None = None,
        phase: str | None = None,
        trace_logger: TrainTraceLogger | None = None,
        retrieval_graph: GraphSpace,
    ) -> tuple[GenerationResponse | None, list[str]]:
        self._last_selected_ontology_ids = None
        return generate_sql(
            self.config,
            self.validation_client,
            question,
            observe_retrieval=self._observe_retrieval,
            trace_id=trace_id,
            source_id=source_id,
            case_index=case_index,
            phase=phase,
            trace_logger=trace_logger,
            retrieval_graph=retrieval_graph,
        )

    def _observe_retrieval(
        self,
        intent_output: dict[str, Any],
        selected_ontology_ids: set[str],
    ) -> None:
        self._last_intent_output = intent_output
        self._last_selected_ontology_ids = selected_ontology_ids

    def _selected_ontology_ids(self) -> set[str]:
        if self._last_selected_ontology_ids is not None:
            return set(self._last_selected_ontology_ids)
        return _selected_ontology_ids_from_intent(self._last_intent_output)

    def _table_schema_payload(self) -> dict[str, str]:
        table_schema: dict[str, str] = {}
        for asset in self.workspace.read_draft_assets(NodeType.TABLE):
            table_name = asset.properties.table_name
            schema = asset.properties.table_schema
            if table_name and schema:
                table_schema[table_name] = schema
        return table_schema

    def _analyze_error(
        self,
        record: dict[str, Any],
        eval_result: dict[str, Any],
        provider: LLMProvider,
        *,
        sql_timeout: float,
        allow_execution_failure: bool = False,
        working_assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        workflow = ErrorAnalysisWorkflow(self.config, llm_provider=provider)
        if allow_execution_failure and _trial_execution_status(eval_result) == "failed":
            sql_ic = str(eval_result.get("query") or "").strip()
            if not sql_ic:
                raise ValueError("candidate generation failed before SQL error analysis")
            return workflow.run_execution_failure(
                question=record["question"],
                sql_gt=record["sql"],
                sql_ic=sql_ic,
                execution_error=str(
                    eval_result.get("predicted_error")
                    or eval_result.get("reason")
                    or "predicted SQL execution failed"
                ),
                table_schema=self._table_schema_payload(),
                matched_ontology=self._last_intent_output or {},
                draft_metrics=[
                    asset.model_dump(mode="json")
                    for asset in _read_semantic_draft_assets(self.workspace)
                    if asset.node_type == NodeType.METRIC
                ],
                evaluation_contract=(
                    record.get("evaluation") if isinstance(record.get("evaluation"), dict) else None
                ),
                working_assets=working_assets,
            )
        workflow_kwargs = {
            "question": record["question"],
            "sql_gt": record["sql"],
            "sql_ic": str(eval_result.get("query") or ""),
            "sql_timeout": sql_timeout,
            "evaluation_contract": (
                record.get("evaluation") if isinstance(record.get("evaluation"), dict) else None
            ),
            "working_assets": working_assets
            or [
                _compact_repair_asset(asset)
                for asset in _read_semantic_draft_assets(self.workspace)
            ],
            "schema_context": self._table_schema_payload(),
            "evidence": record.get("evidence"),
            "gold_column_metadata": record.get("gold_column_metadata", {}),
            "gold_cache": self._gold_query_cache,
        }
        result = workflow.run(**workflow_kwargs)
        return result

    def _analyze_error_with_trace(
        self,
        record: dict[str, Any],
        eval_result: dict[str, Any],
        provider: LLMProvider,
        *,
        sql_timeout: float,
        trace_logger: TrainTraceLogger,
        source_id: str,
        case_index: int,
        phase: str,
        allow_execution_failure: bool = False,
        working_assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if allow_execution_failure:
            analysis = self._analyze_error(
                record,
                eval_result,
                provider,
                sql_timeout=sql_timeout,
                allow_execution_failure=True,
                working_assets=working_assets,
            )
        else:
            # Preserve the legacy override call shape when execution analysis is unavailable.
            analysis = self._analyze_error(
                record,
                eval_result,
                provider,
                sql_timeout=sql_timeout,
                working_assets=working_assets,
            )
        llm_trace = _pop_llm_trace(analysis)
        errors = _error_candidates_from_analysis(analysis)
        trace_logger.log(
            source_id=source_id,
            case_index=case_index,
            step="error_analysis",
            phase=phase,
            input={
                "question": record["question"],
                "gold_sql": record["sql"],
                "predicted_sql": eval_result.get("query"),
                "evaluation_summary": _evaluation_summary(eval_result),
                "accepted_working_assets": working_assets or [],
            },
            output=analysis,
            prompt=llm_trace.get("prompt"),
            response=llm_trace.get("response"),
            summary={
                "error_candidate_count": len(errors),
                "error_ids": [error.get("id") for error in errors],
            },
        )
        return analysis


class _WorkspaceSnapshot:
    def __init__(self, payloads: dict[Path, bytes | None]):
        self.payloads = payloads

    @classmethod
    def capture(cls, workspace: Workspace) -> _WorkspaceSnapshot:
        paths = _snapshot_paths(workspace)
        payloads = {path: path.read_bytes() if path.exists() else None for path in paths}
        return cls(payloads)

    def restore(self) -> None:
        for path, payload in self.payloads.items():
            if payload is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)


def _snapshot_paths(workspace: Workspace) -> list[Path]:
    paths = [
        workspace.source_evidence_path(),
    ]
    for asset_type in _SEMANTIC_TYPES:
        paths.append(workspace.draft_dir / ASSET_GROUP_FILES[asset_type])
        paths.append(workspace.candidates_dir / CANDIDATE_GROUP_FILES[asset_type])
    return paths


def _evaluation_summary(eval_result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(eval_result, dict):
        return {
            "execution_status": "unavailable",
            "matches_gold": False,
            "reason": "missing evaluation result",
        }
    evaluation = eval_result.get("evaluation")
    evaluation_dict = evaluation if isinstance(evaluation, dict) else {}
    gold_result = eval_result.get("gold_result")
    predicted_result = eval_result.get("predicted_result")
    return {
        "execution_status": _trial_execution_status(eval_result),
        "matches_gold": bool(eval_result.get("correct") or evaluation_dict.get("exec") == 1),
        "exec": evaluation_dict.get("exec"),
        "reason": eval_result.get("reason") or evaluation_dict.get("reason") or "",
        "gold_columns": _result_columns(gold_result),
        "predicted_columns": _result_columns(predicted_result),
        "gold_row_count": _result_row_count(gold_result),
        "predicted_row_count": _result_row_count(predicted_result),
        "gold_sample_rows": _result_sample_rows(gold_result),
        "predicted_sample_rows": _result_sample_rows(predicted_result),
        "gold_error": eval_result.get("gold_error"),
        "predicted_error": eval_result.get("predicted_error"),
        "candidate_failure": eval_result.get("candidate_failure"),
        "evaluation_contract": eval_result.get("evaluation_contract"),
    }


def _stable_replay_attempt_summaries(
    result: StableReplayResult,
) -> list[dict[str, Any]]:
    return [
        {
            "attempt": attempt.attempt,
            "trace_id": attempt.trace_id,
            "evaluation": _evaluation_summary(attempt.evaluation),
            "query": attempt.evaluation.get("query"),
            "selected_ontology_ids": attempt.selected_ontology_ids,
            "raw_retrieved_ontology_ids": attempt.raw_retrieved_ontology_ids,
        }
        for attempt in result.attempts
    ]


def _raw_retrieved_ontology_ids(intent_output: Any) -> set[str]:
    """Read ChatBI's pre-selection recall evidence from an intent response."""

    if isinstance(intent_output, BaseModel):
        intent_output = intent_output.model_dump(mode="json")
    if not isinstance(intent_output, dict):
        return set()
    data = intent_output.get("data")
    if not isinstance(data, dict):
        return set()
    raw_ids = data.get("raw_retrieved_asset_ids")
    if not isinstance(raw_ids, list):
        return set()
    return {
        asset_id.strip() for asset_id in raw_ids if isinstance(asset_id, str) and asset_id.strip()
    }


def _selected_ontology_ids_from_intent(intent_output: Any) -> set[str]:
    """Prefer ChatBI's explicit post-selection IDs and retain legacy packet support."""

    if isinstance(intent_output, BaseModel):
        intent_output = intent_output.model_dump(mode="json")
    if not isinstance(intent_output, dict):
        return set()
    selected: set[str] = set()
    data = intent_output.get("data")
    if isinstance(data, dict):
        raw_ids = data.get("selected_asset_ids")
        if isinstance(raw_ids, list):
            selected.update(
                asset_id.strip()
                for asset_id in raw_ids
                if isinstance(asset_id, str) and asset_id.strip()
            )
    try:
        selected.update(matched_ontology_ids(intent_output))
    except ValueError:
        pass
    return selected


def _trial_execution_status(eval_result: dict[str, Any]) -> str:
    if eval_result.get("evaluation_available") is False:
        return "unavailable"
    if eval_result.get("candidate_failure"):
        return "failed"
    predicted_error = str(eval_result.get("predicted_error") or "").strip()
    reason = str(eval_result.get("reason") or "")
    if predicted_error or reason.startswith("predicted SQL execution failed"):
        return "failed"
    return "succeeded"


def _result_columns(result: Any) -> list[str]:
    if isinstance(result, dict) and isinstance(result.get("columns"), list):
        return [str(column) for column in result["columns"]]
    return []


def _result_row_count(result: Any) -> int | None:
    if isinstance(result, dict) and isinstance(result.get("rows"), list):
        return len(result["rows"])
    return None


def _result_sample_rows(result: Any, *, limit: int = 5) -> list[Any]:
    if isinstance(result, dict) and isinstance(result.get("rows"), list):
        return list(result["rows"][:limit])
    return []


def _pop_llm_trace(payload: dict[str, Any]) -> dict[str, Any]:
    trace = payload.pop("__llm_trace", None)
    return trace if isinstance(trace, dict) else {}


def _error_candidates_from_analysis(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("error_candidates", "errors"):
        candidates = _error_candidate_dicts(analysis.get(key))
        if candidates:
            return candidates
    return []


def _joint_optimization_targets_from_analysis(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    confirmed_errors = _error_candidate_dicts(analysis.get("confirmed_errors"))
    if confirmed_errors:
        raw_groups = analysis.get("repair_groups")
        if isinstance(raw_groups, list) and raw_groups:
            return _repair_group_target_dicts(raw_groups, confirmed_errors)
        valid_owners = {"semantic_mapping", "metric", "generation_rule"}
        if all(error.get("owner") in valid_owners for error in confirmed_errors):
            return [
                {
                    **error,
                    "error_ids": [error["id"]],
                    "repair_instruction": error["gold_logic"],
                    "grouping_reason": "",
                    "member_errors": [error],
                }
                for error in confirmed_errors
            ]
        # Typed diagnostics do not carry ontology ownership. Wait for a validated review instead
        # of fabricating ownerless optimization targets from them.
        return []
    if "confirmed_errors" in analysis:
        return []
    if "error_candidates" in analysis:
        return _error_candidate_dicts(analysis.get("error_candidates"))
    return _error_candidates_from_analysis(analysis)


def _repair_group_target_dicts(
    value: list[Any],
    confirmed_errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    errors_by_id = {error["id"]: error for error in confirmed_errors}
    groups: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("id") or "").strip()
        error_ids = [
            str(error_id) for error_id in item.get("error_ids", []) if str(error_id).strip()
        ]
        repair_instruction = str(item.get("repair_instruction") or "").strip()
        member_errors = [
            errors_by_id[error_id] for error_id in error_ids if error_id in errors_by_id
        ]
        if not group_id or not repair_instruction or len(member_errors) != len(error_ids):
            continue
        groups.append(
            {
                "id": group_id,
                "owner": str(item.get("owner") or "").strip(),
                "error_key": (
                    member_errors[0]["error_key"] if len(member_errors) == 1 else "coupled_repair"
                ),
                "difference_point": " ".join(error["difference_point"] for error in member_errors),
                "gold_logic": repair_instruction,
                "predicted_logic": " ".join(error["predicted_logic"] for error in member_errors),
                "error_ids": error_ids,
                "repair_instruction": repair_instruction,
                "base_asset_id": str(item.get("base_asset_id") or "").strip() or None,
                "grouping_reason": str(item.get("grouping_reason") or ""),
                "member_errors": member_errors,
            }
        )
    return groups


def _error_candidate_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    candidates: list[dict[str, Any]] = []
    fields = (
        "id",
        "error_key",
        "difference_point",
        "gold_logic",
        "predicted_logic",
    )
    optional_fields = (
        "error_ids",
        "repair_instruction",
        "grouping_reason",
        "member_errors",
        "owner",
        "question_evidence",
        "evidence_role",
    )
    for item in value:
        if not isinstance(item, dict):
            continue
        candidate = {key: item[key] for key in fields if key in item}
        if all(str(candidate.get(key) or "").strip() for key in fields):
            candidate.update({key: item[key] for key in optional_fields if key in item})
            candidates.append(candidate)
    return candidates


def _read_semantic_draft_assets(workspace: Workspace) -> list[NodeAsset]:
    assets: list[NodeAsset] = []
    for asset_type in _SEMANTIC_TYPES:
        assets.extend(workspace.read_draft_assets(asset_type))
    return assets


def _read_semantic_candidates(workspace: Workspace) -> list[NodeAsset]:
    assets: list[NodeAsset] = []
    for asset_type in _SEMANTIC_TYPES:
        assets.extend(workspace.read_candidates(asset_type))
    return sorted(assets, key=lambda asset: (asset.node_type.value, asset.id))


def _write_semantic_draft_assets(
    workspace: Workspace,
    assets: list[NodeAsset],
) -> None:
    by_type = {asset_type: [] for asset_type in _SEMANTIC_TYPES}
    for asset in assets:
        if asset.node_type in by_type:
            by_type[asset.node_type].append(asset)
    for asset_type, rows in by_type.items():
        workspace.write_draft_assets(asset_type, rows)


def _copy_non_semantic_draft_assets(source: Workspace, target: Workspace) -> None:
    for asset_type in (
        NodeType.TABLE,
        NodeType.COLUMN,
        NodeType.JOIN_PATH,
        NodeType.DIMENSION_VALUE,
    ):
        assets = source.read_draft_assets(asset_type)
        if assets:
            target.write_draft_assets(asset_type, assets)
    target.write_catalog_edges(source.read_catalog_edges())


def _write_extraction_graph_artifact(
    workspace: Workspace,
    *,
    run_id: str,
    case_index: int,
    source_id: str,
    candidates: list[NodeAsset],
    compile_result: dict[str, Any] | None,
    extraction_error: dict[str, str] | None,
) -> Path:
    """Persist one extraction outcome before transient case staging is removed."""

    base_assets = [
        asset
        for asset_type in ASSET_GROUP_FILES
        for asset in workspace.read_draft_assets(asset_type)
    ]
    overlay = {asset.id: asset for asset in base_assets}
    overlay.update({asset.id: asset for asset in candidates})
    graph_assets = [asset for asset in overlay.values() if asset.node_type != NodeType.JOIN_PATH]
    compilation = compile_all_relationships(
        graph_assets,
        endpoint_ids={asset.id: asset.id for asset in graph_assets},
    )
    candidate_ids = {asset.id for asset in candidates}
    edges = [edge for edge in compilation.outgoing_edges if edge.from_node_id in candidate_ids]
    included_ids = candidate_ids | {edge.to_node_id for edge in edges}
    nodes = [asset for asset in graph_assets if asset.id in included_ids]
    safe_source_id = re.sub(r"[^A-Za-z0-9._-]+", "_", source_id).strip("._") or "case"
    artifact_dir = workspace.root / "train_runs" / run_id / "extraction_graphs"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{case_index:04d}_{safe_source_id}.json"
    write_json_atomic(
        path,
        {
            "schema_version": 1,
            "source_id": source_id,
            "stage": "post_extraction_pre_merge",
            "status": "failed" if extraction_error else "completed",
            "compile": compile_result,
            "error": extraction_error,
            "candidate_node_ids": sorted(candidate_ids),
            "nodes": [serialize_asset_envelope(asset) for asset in nodes],
            "edges": [edge.model_dump(mode="json") for edge in edges],
            "diagnostics": list(compilation.diagnostics),
        },
    )
    return path


def _write_case_progress(
    workspace: Workspace,
    *,
    run_id: str,
    case_index: int,
    result: dict[str, Any],
    extraction_graph_path: Path,
) -> Path:
    """Append a compact durable status row as soon as one case finishes.

    ``result.json`` is intentionally written by the public CLI only after the
    engine returns.  This index therefore remains useful when a long run is
    interrupted, and points reviewers at both the pre-merge extraction and the
    complete case bundle when one exists.
    """

    path = workspace.root / "train_runs" / run_id / "case_progress.jsonl"
    row = {
        "run_id": run_id,
        "case_index": case_index,
        "source_id": result.get("source_id"),
        "question": result.get("question"),
        "correct": bool(result.get("correct")),
        "stable_correct": result.get("completion_reason") == "stable_correct",
        "completion_reason": result.get("completion_reason"),
        "replay_status": result.get("replay_status"),
        "required_correct_replays": result.get("required_correct_replays"),
        "completed_replays": result.get("completed_replays"),
        "failed_closed": bool(result.get("failed_closed")),
        "fatal": bool(result.get("fatal")),
        "reason": result.get("reason") or result.get("failure_reason"),
        "asset_outcome": result.get("asset_outcome"),
        "draft_node_ids": list(result.get("draft_node_ids") or []),
        "draft_node_count": int(result.get("draft_node_count") or 0),
        "extraction_graph_path": str(extraction_graph_path),
        "bundle_path": result.get("bundle_path"),
    }
    return write_jsonl(path, [row], append=True)


def _write_case_bundle(
    workspace: Workspace,
    *,
    run_id: str,
    case_index: int,
    record: dict[str, Any],
    result: dict[str, Any],
    assets: list[NodeAsset],
    edges: tuple[GraphEdge, ...],
) -> Path:
    """Persist a case-local semantic result for the post-Train Graph Merge boundary."""

    source_id = str(record["source_id"])
    safe_source_id = re.sub(r"[^A-Za-z0-9._-]+", "_", source_id).strip("._") or "case"
    cases_dir = workspace.root / "train_runs" / run_id / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    final_dir = cases_dir / f"{case_index:04d}_{safe_source_id}"
    with tempfile.TemporaryDirectory(prefix=f".{final_dir.name}-", dir=cases_dir) as tmp:
        staging = Path(tmp)
        assets_dir = staging / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        for asset_type in _SEMANTIC_TYPES:
            grouped = [
                serialize_asset_envelope(asset) for asset in assets if asset.node_type == asset_type
            ]
            write_json(assets_dir / ASSET_GROUP_FILES[asset_type], grouped)
        write_json(
            staging / "source.json",
            {
                "source_id": source_id,
                "question": record.get("question"),
                "sql": record.get("sql"),
            },
        )
        write_json(
            staging / "draft_nodes.json",
            [serialize_asset_envelope(asset) for asset in assets],
        )
        write_jsonl(
            staging / "draft_edges.jsonl",
            [edge.model_dump(mode="json") for edge in edges],
        )
        write_json(staging / "case_result.json", result)
        write_json_atomic(
            staging / "manifest.json",
            {
                "schema_version": 1,
                "source_id": source_id,
                "completion_reason": result.get("completion_reason"),
                "asset_outcome": result.get("asset_outcome"),
                "asset_count": len(assets),
                "edge_count": len(edges),
                "asset_fingerprint": ontology_fingerprint(assets),
                "merge_eligible": bool(assets) and result.get("asset_outcome") == "ready",
                "assets_path": "assets",
                "draft_nodes_path": "draft_nodes.json",
                "draft_edges_path": "draft_edges.jsonl",
                "source_path": "source.json",
                "case_result_path": "case_result.json",
            },
        )
        staging.replace(final_dir)
    return final_dir


def _completion_reason(result: dict[str, Any]) -> str:
    if result.get("correct"):
        return "stable_correct"
    if result.get("oracle_unavailable") or result.get("evaluation_available") is False:
        return "oracle_unavailable"
    if result.get("failed_closed"):
        return "failed_closed"
    failure_reason = str(result.get("failure_reason") or "")
    if failure_reason.startswith("planning_or_initialization_invalid:"):
        return "planning_or_initialization_invalid"
    return "budget_exhausted"


def _joint_case_result(
    record: dict[str, Any],
    final_eval: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    accepted_semantic_mapping_ids: list[str] | None = None,
    accepted_metric_ids: list[str] | None = None,
    optimized_metric_ids: list[str] | None = None,
    accepted_rule_ids: list[str] | None = None,
    rejected_asset_ids: list[str] | None = None,
    deduplicated_asset_ids: list[str] | None = None,
    remaining_target_ids: list[str] | None = None,
    ontology_updated: bool = False,
    failed_closed: bool = False,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    mappings = list(accepted_semantic_mapping_ids or [])
    metrics = list(accepted_metric_ids or [])
    optimized_metrics = list(optimized_metric_ids or [])
    rules = list(accepted_rule_ids or [])
    rejected = list(rejected_asset_ids or [])
    deduplicated = list(deduplicated_asset_ids or [])
    remaining = list(remaining_target_ids or [])
    trial_sql_correct = bool(final_eval.get("correct"))
    active_targets_complete = not remaining
    correct = trial_sql_correct and active_targets_complete and not failed_closed
    residual_status = "none" if correct else "analyzed" if remaining else "pending_analysis"
    updated_types = []
    if mappings:
        updated_types.append("semantic_mapping")
    if metrics:
        updated_types.append("metric")
    if rules:
        updated_types.append("generation_rule")
    return {
        "source_id": str(record["source_id"]),
        "question": record["question"],
        "correct": correct,
        "sql_correct": trial_sql_correct,
        "replay_status": final_eval.get("replay_status"),
        "required_correct_replays": final_eval.get("required_correct_replays"),
        "completed_replays": final_eval.get("completed_replays"),
        "ontology_updated": ontology_updated,
        "accepted_rule_ids": rules,
        "accepted_semantic_mapping_ids": mappings,
        "accepted_metric_ids": metrics,
        "optimized_metric_ids": optimized_metrics,
        "accepted_asset_ids_by_type": {
            "semantic_mapping": mappings,
            "metric": metrics,
            "generation_rule": rules,
        },
        "ontology_updated_types": updated_types,
        "rejected_rule_ids": [
            asset_id for asset_id in rejected if asset_id.startswith("generation_rule.")
        ],
        "rejected_asset_ids": rejected,
        "deduplicated_asset_ids": deduplicated,
        "remaining_error_ids": remaining,
        "remaining_target_error_ids": remaining,
        "active_targets_complete": active_targets_complete,
        "residual_status": residual_status,
        "case_stable_correct": correct,
        "iterations": len([event for event in events if "iteration" in event]),
        "accepted_assets": len(mappings) + len(metrics) + len(rules),
        "rejected_assets": len(rejected),
        "failed_closed": failed_closed,
        "failure_reason": failure_reason,
        "target_cycle_status": (
            failure_reason
            if failed_closed
            else "sql_correct"
            if correct
            else "target_complete_sql_incorrect"
            if not remaining
            else "target_unresolved"
        ),
        "targets_complete": active_targets_complete,
        "reason": failure_reason or (None if correct else final_eval.get("reason")),
        "events": events,
        "fatal": False,
    }


def _compact_repair_asset(asset: NodeAsset) -> dict[str, Any]:
    properties = asset.properties.model_dump(mode="json")
    keys = {
        NodeType.CONCEPT: ("mapping_type", "targets"),
        NodeType.METRIC: (
            "expression",
            "references",
            "sql_generation_plans",
            "aggregation_policy",
            "calculation_policy",
        ),
        NodeType.GENERATION_RULE: (
            "triggers",
            "condition",
            "action",
            "references",
        ),
    }[asset.node_type]
    return {
        "id": asset.id,
        "node_type": asset.node_type.value,
        "name": asset.name,
        "aliases": list(asset.aliases),
        "properties": {
            key: properties[key] for key in keys if properties.get(key) not in (None, "", [], {})
        },
    }


def _is_case_local_extraction_contract_failure(exc: Exception) -> bool:
    """Return whether extraction failed closed on one case's generated contract output."""

    return isinstance(exc, ValueError) and not is_retryable_llm_error(exc)


def _case_setup_failure(
    record: dict[str, Any],
    reason: str,
    *,
    stage: str,
    failure_kind: str,
) -> dict[str, Any]:
    return {
        "source_id": str(record["source_id"]),
        "question": record["question"],
        "correct": False,
        "sql_correct": False,
        "ontology_updated": False,
        "accepted_rule_ids": [],
        "remaining_error_ids": [],
        "iterations": 0,
        "accepted_assets": 0,
        "rejected_assets": 0,
        "reason": reason,
        "completion_reason": "case_setup_failed",
        "replay_status": "not_run",
        "failed_closed": True,
        "asset_outcome": "none",
        "draft_node_ids": [],
        "draft_node_count": 0,
        "events": [],
        "fatal": True,
        "operational_error": failure_kind == "retryable_llm_error",
        "failure_kind": failure_kind,
        "stage": stage,
    }


def _drop_self_gold_join_proposals(
    proposals: list[Any],
    *,
    table_columns: dict[str, set[str]],
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Drop LLM self-relationship proposals while keeping other validation strict."""

    canonical_tables = {str(table).lower(): str(table) for table in table_columns}
    filtered: list[Any] = []
    dropped: list[dict[str, Any]] = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            filtered.append(proposal)
            continue
        from_table = canonical_tables.get(str(proposal.get("from_table") or "").lower())
        to_table = canonical_tables.get(str(proposal.get("to_table") or "").lower())
        if from_table is not None and from_table == to_table:
            dropped.append(
                {
                    "index": index,
                    "reason": "self_relationship",
                    "from_table": from_table,
                    "to_table": to_table,
                    "evidence_fragment": proposal.get("evidence_fragment"),
                    "sql_construct_type": proposal.get("sql_construct_type"),
                }
            )
            continue
        filtered.append(proposal)
    return filtered, dropped


def _clear_rolled_back_case_commit_result(case_result: dict[str, Any]) -> None:
    case_result["transaction_rolled_back"] = True
    case_result["ontology_updated"] = False
    case_result["accepted_rule_ids"] = []
    case_result["accepted_semantic_mapping_ids"] = []
    case_result["accepted_metric_ids"] = []
    case_result["optimized_metric_ids"] = []
    case_result["committed_semantic_mapping_ids"] = []
    case_result["committed_metric_ids"] = []
    case_result["committed_generation_rule_ids"] = []
    case_result.pop("committed_ontology_fingerprint", None)
    case_result["accepted_assets"] = 0
    case_result["ontology_updated_types"] = []
    accepted_by_type = case_result.setdefault("accepted_asset_ids_by_type", {})
    accepted_by_type["semantic_mapping"] = []
    accepted_by_type["metric"] = []
    accepted_by_type["generation_rule"] = []


def _ontology_contract_evaluation(exc: CaseRetrievalGraphError) -> dict[str, Any]:
    message = str(exc)
    source_match = re.search(r"relationship target does not exist: ([^\s]+)\s+->", message)
    asset_id = source_match.group(1) if source_match else None
    return {
        "correct": False,
        "evaluation_available": True,
        "evaluation_status": "candidate_generation_failed",
        "reason": message,
        "candidate_failure": {
            "kind": "ontology_contract",
            "asset_id": asset_id,
            "field_path": "relationships",
            "message": message,
            "retryable": True,
        },
    }


def _dialect_configuration_error(config: CortexConfig) -> str | None:
    validation_dialect = config.validation.dialect.strip().lower()
    database_dialect = config.database.dialect.strip().lower()
    if validation_dialect == database_dialect:
        return None
    return (
        f"validation dialect {config.validation.dialect!r} differs from database dialect "
        f"{config.database.dialect!r}; node train requires matching dialects"
    )


def _retrieval_graph_fingerprint(graph: GraphSpace) -> str:
    payload = graph.model_dump(mode="json")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _failure(run_id: str, message: str, *, trace_path: str | Path | None = None) -> dict[str, Any]:
    return {
        "success": False,
        "run_id": run_id,
        "prepared": None,
        "compile": None,
        "merge": None,
        "cases": [],
        "sql_correct_cases": 0,
        "ontology_updated_cases": 0,
        "generation_rule_updated_cases": 0,
        "metric_updated_cases": 0,
        "optimization_trial_cases": 0,
        "failed_optimization_cases": 0,
        "accepted_generation_rule_count": 0,
        "accepted_metric_count": 0,
        "accepted_cases": 0,
        "errors": [message],
        "warnings": [],
        "trace_path": str(trace_path) if trace_path is not None else None,
    }
