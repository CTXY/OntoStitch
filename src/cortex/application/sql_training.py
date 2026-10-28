from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cortex.application.common import (
    ReviewItem,
    RunDiagnostics,
    RunStatus,
)
from cortex.application.merge_graph import (
    DiagnosticCode,
    GraphMergeResult,
    GraphMergeUseCase,
)
from cortex.infrastructure.progress import emit_progress
from cortex.schema import calculate_schema_hash
from cortex.schema.graph import GraphSpace, GraphState
from cortex.schema.models import SchemaDefinition


class _SqlTrainingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QASqlTrainingRecord(_SqlTrainingModel):
    source_id: str
    question: str
    sql: str
    evidence: str | None = None
    generated_sql: str | None = None
    assets: tuple[dict[str, Any], ...] = ()
    metrics: tuple[dict[str, Any], ...] = ()
    semantic_mappings: tuple[dict[str, Any], ...] = ()
    generation_rules: tuple[dict[str, Any], ...] = ()

    @field_validator("source_id", "question", "sql")
    @classmethod
    def require_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("QA-SQL values must not be empty")
        return value

    @field_validator("generated_sql", "evidence")
    @classmethod
    def normalize_optional_sql(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class SqlTrainingCaseResult(_SqlTrainingModel):
    source_id: str
    passed: bool
    question: str | None = None
    gold_sql: str | None = None
    predicted_sql: str | None = None
    training_status: str = "completed"
    message: str | None = None
    fatal: bool = False
    ontology_updated: bool = False
    failed_closed: bool = False
    accepted_metric_ids: tuple[str, ...] = ()
    accepted_generation_rule_ids: tuple[str, ...] = ()
    committed_semantic_mapping_ids: tuple[str, ...] = ()
    replay_status: str | None = None
    required_correct_replays: int | None = Field(default=None, ge=0)
    completed_replays: int | None = Field(default=None, ge=0)
    transaction_rolled_back: bool = False
    completion_reason: str | None = None
    asset_outcome: str | None = None
    draft_node_ids: tuple[str, ...] = ()
    draft_node_count: int = Field(default=0, ge=0)
    result_path: str | None = None
    ontology_path: str | None = None
    iterations: int = Field(default=0, ge=0)
    trace_id: str | None = None
    execution_comparison: dict[str, Any] = Field(default_factory=dict)
    baseline_evaluation: dict[str, Any] = Field(default_factory=dict)
    final_evaluation: dict[str, Any] = Field(default_factory=dict)
    active_targets_complete: bool = False
    residual_status: Literal["none", "analyzed", "pending_analysis"] = "none"
    case_stable_correct: bool = False


class SqlTrainingEvaluationReport(_SqlTrainingModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    cases: tuple[SqlTrainingCaseResult, ...] = ()
    success: bool = True
    fatal: bool = False
    run_id: str | None = None
    sql_correct_cases: int = Field(default=0, ge=0)
    ontology_updated_cases: int = Field(default=0, ge=0)
    generation_rule_updated_cases: int = Field(default=0, ge=0)
    metric_updated_cases: int = Field(default=0, ge=0)
    optimization_trial_cases: int = Field(default=0, ge=0)
    failed_optimization_cases: int = Field(default=0, ge=0)
    accepted_generation_rule_count: int = Field(default=0, ge=0)
    accepted_metric_count: int = Field(default=0, ge=0)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    workspace: str | None = None
    trace_path: str | None = None

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if self.passed + self.failed != self.total:
            raise ValueError("SQL training evaluation totals are inconsistent")
        if self.cases and len(self.cases) != self.total:
            raise ValueError("SQL training evaluation cases must match total")
        if self.cases and sum(case.passed for case in self.cases) != self.passed:
            raise ValueError(
                "SQL training evaluation case outcomes must match passed count"
            )
        source_ids = [case.source_id for case in self.cases]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError(
                "SQL training evaluation case source_id values must be unique"
            )
        return self


class TrainingExtractionResult(_SqlTrainingModel):
    """Portable handoff, bound to the graph and schema used during extraction."""

    format_version: Literal["training-extraction-v1"] = "training-extraction-v1"
    state: GraphState
    evaluation_report: SqlTrainingEvaluationReport
    existing_state: GraphState
    schema_hash: str

    @property
    def status(self) -> RunStatus:
        return extraction_status(self.evaluation_report)


class SqlTrainingResult(_SqlTrainingModel):
    status: RunStatus
    state: GraphState
    evaluation_report: SqlTrainingEvaluationReport
    merge_report: GraphMergeResult | None = None
    reviews: tuple[ReviewItem, ...] = ()
    diagnostics: RunDiagnostics

    @model_validator(mode="after")
    def validate_merged_state(self) -> Self:
        if (
            self.status != RunStatus.FAILED
            and self.merge_report is not None
            and self.state != self.merge_report.state
        ):
            raise ValueError("state must match merge_report.state")
        return self


class SqlTrainingEngine(Protocol):
    def train(
        self,
        training_set: tuple[QASqlTrainingRecord, ...],
        schema: SchemaDefinition,
        existing_graph: GraphSpace,
    ) -> tuple[GraphState, SqlTrainingEvaluationReport]: ...


class GraphStateRepository(Protocol):
    def load(self) -> GraphState: ...

    def save(self, state: GraphState) -> None: ...


class SqlTrainingService:
    def __init__(
        self,
        engine: SqlTrainingEngine,
        *,
        graph_merge: GraphMergeUseCase | None = None,
        graph_merge_factory: Callable[[], GraphMergeUseCase] | None = None,
        state_repository: GraphStateRepository,
        config_fingerprint: str | None = None,
    ) -> None:
        self._engine = engine
        self._graph_merge = graph_merge
        self._graph_merge_factory = graph_merge_factory
        self._state_repository = state_repository
        self._config_fingerprint = config_fingerprint

    def run(
        self,
        training_set: tuple[QASqlTrainingRecord, ...],
        schema: SchemaDefinition,
        *,
        run_id: str | None = None,
    ) -> SqlTrainingResult:
        existing, revision = _load_snapshot(self._state_repository)
        extraction = extract_training(self._engine, training_set, schema, existing)
        return merge_training_extraction(
            extraction,
            self._graph_merge,
            schema,
            state_repository=self._state_repository,
            graph_merge_factory=self._graph_merge_factory,
            expected_revision=revision,
            config_fingerprint=self._config_fingerprint,
            run_id=run_id,
        )


def extraction_status(report: SqlTrainingEvaluationReport) -> RunStatus:
    """One gate for extraction-only, full training and delayed integration."""
    if report.fatal or any(case.fatal for case in report.cases):
        return RunStatus.FAILED
    if report.failed or report.errors or not report.success:
        return RunStatus.NEEDS_REVIEW
    return RunStatus.COMPLETED


# These are deterministic cleanup of obsolete derived relationships. All other
# diagnostic codes represent uncertainty or preserved variants requiring review.
_INFORMATIONAL_MERGE_CODES = frozenset(
    {
        DiagnosticCode.STALE_REFERENCE_EDGE_DROPPED,
        DiagnosticCode.INVALIDATED_RELATIONSHIP_DROPPED,
    }
)


def training_status(
    report: SqlTrainingEvaluationReport, merged: GraphMergeResult
) -> RunStatus:
    status = extraction_status(report)
    if status != RunStatus.COMPLETED:
        return status
    if merged.state.conflict_nodes or any(
        diagnostic.code not in _INFORMATIONAL_MERGE_CODES
        for diagnostic in merged.diagnostics
    ):
        return RunStatus.NEEDS_REVIEW
    return RunStatus.COMPLETED


def extract_training(
    engine: SqlTrainingEngine,
    training_set: tuple[QASqlTrainingRecord, ...],
    schema: SchemaDefinition,
    existing_state: GraphState | None = None,
) -> TrainingExtractionResult:
    from cortex.application.ontology_graph import GraphManager

    if not training_set:
        raise ValueError("SQL training requires a non-empty QA-SQL training set")
    source_ids = [record.source_id for record in training_set]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError(
            "QA-SQL training record source_id values must be unique within one run"
        )
    existing = GraphManager(schema).validate_state(existing_state or _empty_state())
    emit_progress("phase", phase="training", total=len(training_set))
    try:
        trained, report = engine.train(
            training_set,
            SchemaDefinition.model_validate(schema.model_dump(mode="python")),
            _copy_graph(existing.graph),
        )
        incoming = GraphState.model_validate(trained.model_dump(mode="python"))
        report = SqlTrainingEvaluationReport.model_validate(
            report.model_dump(mode="python")
        )
        # Fatal infrastructure failures may occur before any case is evaluated.
        # Preserve that complete original report rather than replace its cause
        # with a secondary record-count error.
        if extraction_status(report) != RunStatus.FAILED:
            if report.total != len(training_set):
                raise ValueError(
                    "extraction report total does not match training records"
                )
            if report.cases and {case.source_id for case in report.cases} != set(
                source_ids
            ):
                raise ValueError(
                    "extraction report cases do not match training records"
                )
    except Exception as exc:  # noqa: BLE001 -- preserve a fatal extraction artifact
        incoming = _empty_state()
        report = SqlTrainingEvaluationReport(
            total=len(training_set),
            passed=0,
            failed=len(training_set),
            success=False,
            fatal=True,
            errors=(str(exc),),
        )
    return TrainingExtractionResult(
        state=incoming,
        evaluation_report=report,
        existing_state=existing,
        schema_hash=calculate_schema_hash(schema),
    )


def merge_training_extraction(
    extraction: TrainingExtractionResult,
    graph_merge: GraphMergeUseCase | None,
    schema: SchemaDefinition,
    *,
    state_repository: GraphStateRepository | None = None,
    graph_merge_factory: Callable[[], GraphMergeUseCase] | None = None,
    expected_revision: str | None = None,
    config_fingerprint: str | None = None,
    run_id: str | None = None,
) -> SqlTrainingResult:
    """Shared failure gate, integration, validation and optional atomic publication.

    Incoming fragments may refer to existing endpoints; they must be validated
    against their base graph rather than as standalone complete graphs.
    """
    from cortex.application.ontology_graph import GraphManager

    existing = extraction.existing_state
    merged = None
    stage = "extraction"
    failure = None
    report = extraction.evaluation_report
    status = extraction_status(report)
    try:
        if status != RunStatus.FAILED:
            stage = "input_validation"
            if extraction.schema_hash != calculate_schema_hash(schema):
                raise ValueError("extraction schema does not match active schema")
            manager = GraphManager(schema)
            manager.validate_state(existing)
            if state_repository is not None:
                stored, revision = _load_snapshot(state_repository)
                manager.validate_state(stored)
                if stored != existing:
                    raise ValueError(
                        "stored graph does not match extraction existing_state"
                    )
                if expected_revision is None:
                    expected_revision = revision
            incoming = manager.prepare_fragment(extraction.state, existing=existing)
            stage = "merge"
            if graph_merge is None and graph_merge_factory is not None:
                graph_merge = graph_merge_factory()
            if graph_merge is None:
                raise ValueError("graph merge operator is required")
            emit_progress("phase", phase="merging")
            kwargs = {"run_id": run_id} if run_id is not None else {}
            merged = graph_merge.merge(existing, incoming, schema, **kwargs)
            stage = "final_validation"
            emit_progress("phase", phase="validating_graph")
            manager.validate_state(merged.state)
            status = training_status(report, merged)
            if state_repository is not None:
                stage = "persistence"
                if expected_revision is not None:
                    state_repository.commit(
                        merged.state, expected_revision=expected_revision
                    )
                else:
                    state_repository.save(merged.state)
    except Exception as exc:  # noqa: BLE001 -- preserve failure report without publishing
        status = RunStatus.FAILED
        failure = str(exc)
    state = (
        merged.state if merged is not None and status != RunStatus.FAILED else existing
    )
    context = {
        key: value
        for key, value in {
            "workspace": report.workspace,
            "trace_path": report.trace_path,
            "failure_stage": stage if status == RunStatus.FAILED else None,
            "failure_message": failure,
        }.items()
        if value is not None
    }
    return SqlTrainingResult(
        status=status,
        state=state,
        evaluation_report=report,
        merge_report=merged,
        diagnostics=RunDiagnostics(
            config_fingerprint=config_fingerprint,
            metrics={
                "training_records": report.total,
                "incoming_nodes": len(extraction.state.graph.nodes),
                "incoming_edges": len(extraction.state.graph.edges),
                "merged_node_mappings": len(merged.node_mappings) if merged else 0,
                "merged_edge_mappings": len(merged.edge_mappings) if merged else 0,
                "merge_diagnostics": len(merged.diagnostics) if merged else 0,
                "nodes": len(state.graph.nodes),
                "edges": len(state.graph.edges),
                "passed": report.passed,
                "failed": report.failed,
            },
            warnings=list(report.warnings),
            context=context,
        ),
    )


def _empty_state() -> GraphState:
    return GraphState(graph=GraphSpace())


def _load_snapshot(repository: GraphStateRepository) -> tuple[GraphState, str | None]:
    if hasattr(repository, "load_snapshot"):
        return repository.load_snapshot()
    return repository.load(), None


def _copy_graph(value: GraphSpace) -> GraphSpace:
    return GraphSpace.model_validate(value.model_dump(mode="python"))


__all__ = [
    "GraphStateRepository",
    "QASqlTrainingRecord",
    "SqlTrainingCaseResult",
    "SqlTrainingEngine",
    "SqlTrainingEvaluationReport",
    "SqlTrainingResult",
    "SqlTrainingService",
    "TrainingExtractionResult",
    "extract_training",
    "extraction_status",
    "merge_training_extraction",
    "training_status",
]
