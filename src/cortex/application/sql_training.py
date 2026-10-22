from __future__ import annotations

from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cortex.application.common import (
    ReviewItem,
    RunDiagnostics,
    RunStatus,
)
from cortex.application.merge_graph import GraphMergeUseCase
from cortex.schema.graph import GraphSpace, GraphState
from cortex.schema.models import SchemaDefinition
from cortex.schema.validation import validate_graph_space


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
    bundle_path: str | None = None
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
        return self


class SqlTrainingResult(_SqlTrainingModel):
    status: RunStatus
    state: GraphState
    evaluation_report: SqlTrainingEvaluationReport
    reviews: tuple[ReviewItem, ...] = ()
    diagnostics: RunDiagnostics


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
        graph_merge: GraphMergeUseCase,
        state_repository: GraphStateRepository,
        config_fingerprint: str | None = None,
    ) -> None:
        self._engine = engine
        self._graph_merge = graph_merge
        self._state_repository = state_repository
        self._config_fingerprint = config_fingerprint

    def run(
        self,
        training_set: tuple[QASqlTrainingRecord, ...],
        schema: SchemaDefinition,
    ) -> SqlTrainingResult:
        if not training_set:
            raise ValueError("SQL training requires a non-empty QA-SQL training set")
        source_ids = [record.source_id for record in training_set]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError(
                "QA-SQL training record source_id values must be unique within one run"
            )

        existing_state = self._state_repository.load()
        validate_graph_space(existing_state.graph, schema)
        engine_graph = _copy_graph(existing_state.graph)
        engine_schema = SchemaDefinition.model_validate(schema.model_dump(mode="python"))
        trained, report = self._engine.train(training_set, engine_schema, engine_graph)
        incoming_state = GraphState.model_validate(trained.model_dump(mode="python"))
        evaluation_report = SqlTrainingEvaluationReport.model_validate(
            report.model_dump(mode="python")
        )
        if evaluation_report.fatal:
            return SqlTrainingResult(
                status=RunStatus.FAILED,
                state=existing_state,
                evaluation_report=evaluation_report,
                diagnostics=RunDiagnostics(
                    config_fingerprint=self._config_fingerprint,
                    metrics={
                        "training_records": len(training_set),
                        "incoming_nodes": len(incoming_state.graph.nodes),
                        "incoming_edges": len(incoming_state.graph.edges),
                        "merged_node_mappings": 0,
                        "merged_edge_mappings": 0,
                        "merge_diagnostics": 0,
                    },
                ),
            )
        merged = self._graph_merge.merge(existing_state, incoming_state, schema)
        self._state_repository.save(merged.state)
        graph = merged.state.graph
        status = (
            RunStatus.NEEDS_REVIEW
            if evaluation_report.failed or evaluation_report.errors
            else RunStatus.COMPLETED
        )
        return SqlTrainingResult(
            status=status,
            state=merged.state,
            evaluation_report=evaluation_report,
            diagnostics=RunDiagnostics(
                config_fingerprint=self._config_fingerprint,
                metrics={
                    "training_records": len(training_set),
                    "incoming_nodes": len(incoming_state.graph.nodes),
                    "incoming_edges": len(incoming_state.graph.edges),
                    "merged_node_mappings": len(merged.node_mappings),
                    "merged_edge_mappings": len(merged.edge_mappings),
                    "merge_diagnostics": len(merged.diagnostics),
                    "nodes": len(graph.nodes),
                    "edges": len(graph.edges),
                    "passed": evaluation_report.passed,
                    "failed": evaluation_report.failed,
                },
                warnings=list(evaluation_report.warnings),
                context={
                    key: value
                    for key, value in {
                        "workspace": evaluation_report.workspace,
                        "trace_path": evaluation_report.trace_path,
                    }.items()
                    if value is not None
                },
            ),
        )


def _copy_graph(value: GraphSpace) -> GraphSpace:
    return GraphSpace.model_validate(value.model_dump(mode="python"))


__all__ = [
    "QASqlTrainingRecord",
    "SqlTrainingCaseResult",
    "SqlTrainingEngine",
    "SqlTrainingEvaluationReport",
    "GraphStateRepository",
    "SqlTrainingResult",
    "SqlTrainingService",
]
