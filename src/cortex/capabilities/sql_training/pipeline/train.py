"""Run preparation, isolated case scheduling and aggregation; no case learning logic."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.artifacts.cases import case_result
from cortex.capabilities.sql_training.artifacts.jsonl import (
    read_json,
    write_json_atomic,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.snapshots import PHYSICAL_TYPES, GraphStore
from cortex.capabilities.sql_training.graph.values import merge_verified_values
from cortex.capabilities.sql_training.learning.runtime import LearningAgent
from cortex.capabilities.sql_training.llm import NodeLLMProvider
from cortex.capabilities.sql_training.pipeline.physical import PhysicalPreparation
from cortex.capabilities.sql_training.pipeline.prepare import PrepareQASQLPipeline
from cortex.capabilities.sql_training.prepare import load_standard_qa_sql_records
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
from cortex.capabilities.sql_training.training.evidence import EvidenceStore
from cortex.capabilities.sql_training.training.observability import SummaryTracer
from cortex.capabilities.sql_training.training.services import CaseServices
from cortex.capabilities.sql_training.training.trace import (
    TracingLLMProvider,
    TrainTraceLogger,
)
from cortex.capabilities.sql_training.training.workflow import (
    CaseResumeError,
    CaseWorkflow,
)
from cortex.schema import load_root_schema


class TrainPipeline:
    skip_direct = False
    frozen_physical = False

    def __init__(
        self,
        config: SqlTrainingRuntimeConfig,
        *,
        model: BaseChatModel | None = None,
        llm_provider=None,
        validation_client=None,
        validation_client_factory=None,
        case_resources_factory=None,
        database=None,
        schema=None,
        existing_join_edges=(),
    ):
        self.config = config
        self.model = model
        self.llm_provider = llm_provider
        self.validation_client = validation_client
        self.validation_client_factory = validation_client_factory
        self.case_resources_factory = case_resources_factory
        self.database = database
        self.schema = schema or load_root_schema()
        self.existing_join_edges = existing_join_edges
        self.workspace = Workspace(config.workspace)
        self._aggregation_lock = Lock()
        self.tracer = SummaryTracer()

    def run(
        self,
        *,
        qa_path,
        dataset,
        max_rounds=None,
        max_iterations=None,
        sql_timeout=None,
        trace_prefix=None,
    ) -> dict[str, Any]:
        del trace_prefix
        settings = self.config.train.model_copy(
            update={
                "max_rounds": self.config.train.max_rounds
                if max_rounds is None
                else max_rounds,
                "max_iterations": self.config.train.max_iterations
                if max_iterations is None
                else max_iterations,
                "sql_timeout": self.config.train.sql_timeout
                if sql_timeout is None
                else sql_timeout,
            }
        )
        # Validate overrides as well as configuration loaded from YAML.
        settings = type(settings).model_validate(settings.model_dump())
        self.config = self.config.model_copy(update={"train": settings})
        run_id = f"train_{uuid4().hex}"
        logger = TrainTraceLogger(
            self.workspace.root, run_id, level=settings.trace_level
        )
        self.workspace.init()
        try:
            with self.tracer.span("extract.run"):
                return self._run(qa_path, dataset, run_id, logger)
        except Exception as exc:  # noqa: BLE001 - isolate run/case failures and preserve accepted assets
            logger.log(
                source_id=None,
                step="train_failure",
                output={"error": str(exc), "error_type": type(exc).__name__},
            )
            return {
                "success": False,
                "fatal": True,
                "run_id": run_id,
                "cases": [],
                "errors": [f"{type(exc).__name__}: {exc}"],
                "warnings": [],
                "workspace": str(self.workspace.root),
                "trace_path": str(logger.path),
            }

    def _run(self, qa_path, dataset, run_id, logger):
        if self.model is None or self.database is None or not self.config.database.url:
            raise ValueError(
                "training requires a tool-capable model and database connector/URL"
            )
        if self.config.database.dialect != self.config.validation.dialect:
            raise ValueError("database and validation dialects must agree")
        prepared = PrepareQASQLPipeline(self.config).run(
            qa_path=qa_path, dataset=dataset
        )
        records = [
            r.to_record() for r in load_standard_qa_sql_records(prepared["output_path"])
        ]
        ids = [r["source_id"] for r in records]
        if len(ids) != len(set(ids)):
            raise ValueError("training source_id must be unique within a run")
        provider = TracingLLMProvider(
            self.llm_provider or NodeLLMProvider(self.model), logger
        )
        prep = PhysicalPreparation(
            self.config, self.workspace, self.database, self.existing_join_edges
        )
        with self.tracer.span("extract.prepare"):
            records = [prep.add_gold_column_metadata(r) for r in records]
            if self.frozen_physical:
                bootstrap = {"reused_frozen_snapshot": True}
            else:
                prep.prepare_gold_join_edges(records, provider, logger)
                bootstrap = prep.bootstrap_dimension_values(records, run_id)
        # Capture once: case-local Value additions do not silently alter sibling or
        # historical snapshots, even when cases execute concurrently.
        physical = [
            a for kind in PHYSICAL_TYPES for a in self.workspace.read_draft_assets(kind)
        ]
        edges = self.workspace.read_catalog_edges()
        workers = min(self.config.train.case_max_workers or 1, max(1, len(records)))
        if (
            workers > 1
            and self.validation_client is not None
            and not (self.case_resources_factory or self.validation_client_factory)
        ):
            raise ValueError("parallel cases require isolated validation clients")

        def run_one(item):
            index, record = item
            return self._case(record, index, run_id, physical, edges, logger)

        if workers == 1:
            cases = [run_one(item) for item in enumerate(records, 1)]
        else:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="ontology-case"
            ) as executor:
                cases = list(executor.map(run_one, enumerate(records, 1)))
        errors = [
            f"{c['source_id']}: {c.get('reason') or 'case incomplete'}"
            for c in cases
            if not c["correct"]
        ]
        result = {
            "success": not errors,
            "fatal": False,
            "run_id": run_id,
            "prepared": prepared,
            "dimension_value_bootstrap": bootstrap,
            "cases": cases,
            "compile": {"mode": "agent_loop"},
            "case_draft_nodes": [
                {"source_id": c["source_id"], "draft_nodes": c.get("draft_nodes", [])}
                for c in cases
            ],
            "sql_correct_cases": sum(c["correct"] for c in cases),
            "ontology_updated_cases": 0,
            "generation_rule_updated_cases": sum(
                bool(c.get("accepted_rule_ids")) for c in cases
            ),
            "metric_updated_cases": sum(
                bool(c.get("optimized_metric_ids")) for c in cases
            ),
            "optimization_trial_cases": sum(
                any("iteration" in e for e in c.get("events", [])) for c in cases
            ),
            "failed_optimization_cases": len(errors),
            "accepted_generation_rule_count": sum(
                len(c.get("accepted_rule_ids", [])) for c in cases
            ),
            "accepted_metric_count": sum(
                len(c.get("accepted_metric_ids", [])) for c in cases
            ),
            "accepted_cases": sum(c["correct"] for c in cases),
            "errors": errors,
            "warnings": [],
            "workspace": str(self.workspace.root),
            "trace_path": str(logger.path),
        }
        write_json_atomic(
            self.workspace.root / "train_runs" / run_id / "result.json", result
        )
        logger.log(
            source_id=None,
            step="train_complete",
            summary={"cases": len(cases), "passed": result["accepted_cases"]},
        )
        return result

    def resume_case(self, case_root: str | Path) -> dict[str, Any]:
        """Continue the same case from its persistent LangGraph thread."""
        root = Path(case_root)
        source = read_json(root / "source.json")
        info = read_json(root / "run_context.json")
        if not source or not info or not (root / "checkpoints.sqlite").is_file():
            raise CaseResumeError(
                "case resume requires source, run context and a LangGraph checkpoint"
            )
        logger = TrainTraceLogger(
            self.workspace.root, info["run_id"], level=self.config.train.trace_level
        )
        return self._case(
            source,
            info["case_index"],
            info["run_id"],
            None,
            None,
            logger,
            resume=True,
            root_override=root,
        )

    def _case(
        self,
        record,
        index,
        run_id,
        physical,
        edges,
        logger,
        *,
        resume=False,
        root_override=None,
    ):
        safe = (
            re.sub(r"[^A-Za-z0-9._-]+", "_", record["source_id"]).strip("._") or "case"
        )
        root = (
            Path(root_override)
            if root_override
            else (
                self.workspace.root
                / "train_runs"
                / run_id
                / "cases"
                / f"{index:04d}_{safe}"
            )
        )
        graph = GraphStore(
            root / "snapshots", workspace=self.workspace, schema=self.schema
        )
        evidence = EvidenceStore(root / "evidence")
        workflow = services = None
        try:
            with (
                self.tracer.span(
                    "extract.case", case_id=record["source_id"]
                ) as summary,
                ExitStack() as resources,
            ):
                if self.case_resources_factory:
                    model, validator = resources.enter_context(
                        self.case_resources_factory()
                    )
                else:
                    model = self.model
                    validator = (
                        self.validation_client_factory()
                        if self.validation_client_factory
                        else self.validation_client
                    )
                    if self.validation_client_factory and callable(
                        getattr(validator, "close", None)
                    ):
                        resources.callback(validator.close)
                if validator is None:
                    raise ValueError("training requires a validation client")

                def initialize():
                    previous = read_json(root / "source.json")
                    if previous is not None and previous != record:
                        raise CaseResumeError("case source cannot be replaced")
                    graph.initialize(physical, edges)
                    write_json_atomic(root / "source.json", record)
                    write_json_atomic(
                        root / "run_context.json",
                        {"run_id": run_id, "case_index": index},
                    )

                services = CaseServices(
                    config=self.config,
                    model=model,
                    validation_client=validator,
                    graph=graph,
                    evidence=evidence,
                    record=record,
                    trace=logger,
                )
                workflow = CaseWorkflow(
                    root=root,
                    graph=graph,
                    evidence=evidence,
                    services=services,
                    agent=LearningAgent(model),
                    database=self.database,
                    record=record,
                    max_rounds=self.config.train.max_rounds,
                    max_iterations=self.config.train.max_iterations,
                    skip_direct=self.skip_direct,
                    initialize=initialize if not resume else None,
                    close_resources=resources.close,
                    merge_values=self._merge_case_values,
                )
                state = workflow.run(resume=resume)
                if not state.bundle_ref:
                    raise RuntimeError("case graph stopped without a sealed result")
                result = read_json(Path(state.bundle_ref))
                summary.update(
                    status=result["case_status"],
                    revision=state.accepted_revision,
                    model_calls=state.model_calls,
                    tool_calls=state.tool_calls,
                )
        except CaseResumeError:
            raise
        except Exception as exc:  # noqa: BLE001 - report an isolated, resumable case failure
            # A failed graph remains resumable. Report from the last checkpoint,
            # never seal a failure bundle over the graph's completed output.
            evidence.put(
                "case_failure", {"error": str(exc), "error_type": type(exc).__name__}
            )
            if workflow is not None and workflow.state is not None:
                state = workflow.state.model_copy(deep=True)
                state.operational_failure = True
                state.completion_reason = "operational_failure"
                result = case_result(record, state, graph, services, root)
            else:
                result = {
                    "source_id": record["source_id"],
                    "question": record["question"],
                    "correct": False,
                    "fatal": True,
                    "failed_closed": True,
                    "case_status": "failed",
                    "asset_outcome": "empty",
                    "completion_reason": "operational_failure",
                    "draft_nodes": [],
                    "events": [],
                }
            result.update(
                reason=f"{type(exc).__name__}: {exc}",
                resumable=bool(workflow and workflow.has_checkpoint),
            )
        write_json_atomic(
            root / "progress.json",
            {
                "source_id": record["source_id"],
                "case_status": result["case_status"],
                "checkpoint_path": str(root / "checkpoints.sqlite"),
                "thread_id": workflow.thread_id if workflow else None,
                "bundle_path": result.get("bundle_path"),
                "completion_reason": result.get("completion_reason"),
            },
        )
        return result

    def _merge_case_values(self, graph: GraphStore) -> None:
        """Aggregate verified facts before publishing a merge-eligible bundle."""
        values = [
            a
            for a in graph.working.physical_assets
            if a.node_type == NodeType.DIMENSION_VALUE
        ]
        with self._aggregation_lock:
            merged = merge_verified_values(
                self.workspace.read_draft_assets(NodeType.DIMENSION_VALUE), values
            )
            self.workspace.write_draft_assets(NodeType.DIMENSION_VALUE, merged)
