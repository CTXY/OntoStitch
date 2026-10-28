from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from cortex.application.common import ApplicationError
from cortex.application.retrieval import (
    ColumnSemanticContext,
    RelevanceDecision,
    RetrievedRepresentation,
)
from cortex.schema import (
    GraphSpace,
    GraphState,
    NodeMergeConflict,
    SchemaDefinition,
)

MentionRole = Literal[
    "metric", "dimension", "dimension_value", "time", "operator", "other"
]
LinkStatus = Literal["selected", "ambiguous", "rejected"]
RetrieveDbStatus = Literal["completed", "needs_clarification", "no_match", "unresolved"]


class RetrieveDbError(ApplicationError):
    """Raised when database retrieval violates its public contract."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EntityMention(_Model):
    mention_id: str
    text: str
    normalized_text: str
    role: MentionRole
    expected_node_types: tuple[str, ...] = ()

    @field_validator("mention_id", "text", "normalized_text")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not (text := value.strip()):
            raise ValueError("mention values must not be empty")
        return text


class SqlColumnReference(_Model):
    table_name: str | None = None
    column_name: str = Field(min_length=1)


class SqlLiteralSignal(_Model):
    value: str
    table_name: str | None = None
    column_name: str | None = None
    operator: str | None = None


class SqlSignals(_Model):
    initial_sql: str
    operations: tuple[str, ...] = ()
    table_names: tuple[str, ...] = ()
    column_refs: tuple[SqlColumnReference, ...] = ()
    literals: tuple[SqlLiteralSignal, ...] = ()


class CandidateEvidence(_Model):
    channel: str
    detail: str


class NodeCandidate(_Model):
    candidate_id: str
    node_id: str
    node_type: str
    name: str
    mention_ids: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    score: float = Field(ge=0, le=1)
    evidence: tuple[CandidateEvidence, ...] = ()
    neighbor_summary: tuple[str, ...] = ()


class NodeSelection(_Model):
    candidate_id: str
    status: LinkStatus
    role: Literal["direct", "rule", "schema", "value"]
    reason: str = ""


class ClarificationSlot(_Model):
    slot_id: str
    mention_id: str
    question: str
    candidates: tuple[NodeCandidate, ...] = Field(min_length=1)


class ClarificationAnswer(_Model):
    slot_id: str
    selected_node_id: str


class TypedRetrievalPath(_Model):
    kind: Literal["concept", "metric", "rule", "schema", "join"]
    root_node_id: str
    node_ids: tuple[str, ...] = Field(min_length=1)
    edge_ids: tuple[str, ...] = ()
    semantic_tables: tuple[str, ...] = ()
    joins: tuple[dict[str, Any], ...] = ()
    joinability_rule_id: str | None = None
    required: bool = True
    reason: str


class RetrieveDbTraceStep(_Model):
    action: str
    observation: str


class RetrieveDbPending(_Model):
    question: str
    initial_sql: str | None = None
    graph_fingerprint: str
    mentions: tuple[EntityMention, ...]
    sql_signals: SqlSignals | None = None
    candidates: tuple[NodeCandidate, ...]
    selections: tuple[NodeSelection, ...]
    clarification_slots: tuple[ClarificationSlot, ...] = Field(min_length=1)


class RetrieveDbResult(_Model):
    status: RetrieveDbStatus
    question: str
    initial_sql: str | None = None
    graph_fingerprint: str
    mentions: tuple[EntityMention, ...] = ()
    sql_signals: SqlSignals | None = None
    candidates: tuple[NodeCandidate, ...] = ()
    selections: tuple[NodeSelection, ...] = ()
    raw_retrieved_node_ids: tuple[str, ...] = ()
    selected_node_ids: tuple[str, ...] = ()
    relevant_ontology_asset_node_ids: tuple[str, ...] = ()
    dependency_node_ids: tuple[str, ...] = ()
    generation_rule_node_ids: tuple[str, ...] = ()
    seed_node_ids: tuple[str, ...] = ()
    paths: tuple[TypedRetrievalPath, ...] = ()
    clarification_slots: tuple[ClarificationSlot, ...] = ()
    pending: RetrieveDbPending | None = None
    trace: tuple[RetrieveDbTraceStep, ...] = ()
    run_id: str | None = None
    retrieved_graph_state: GraphState | None = None
    representations: tuple[RetrievedRepresentation, ...] = ()
    relevance: tuple[RelevanceDecision, ...] = ()
    relevant_semantic_node_ids: tuple[str, ...] = ()
    relevant_grounding_node_ids: tuple[str, ...] = ()

    @property
    def subgraph(self) -> GraphSpace | None:
        """Read-only compatibility view; the inline graph is the only stored value."""
        return (
            self.retrieved_graph_state.graph
            if self.retrieved_graph_state is not None
            else None
        )

    @property
    def node_conflicts(self) -> tuple[NodeMergeConflict, ...]:
        """Legacy read view, never serialized or written by the workflow."""
        return (
            self.retrieved_graph_state.node_conflicts
            if self.retrieved_graph_state is not None
            else ()
        )

    @property
    def relevant_graph(self) -> dict[str, Any] | None:
        if self.retrieved_graph_state is None:
            return None
        from cortex.schema import export_inline_graph

        return export_inline_graph(self.retrieved_graph_state)

    def public_payload(self) -> dict[str, Any]:
        """Expose one inline graph, retaining legacy views only on the Python API."""
        payload = self.model_dump(mode="json", exclude={"retrieved_graph_state"})
        payload["relevant_graph"] = self.relevant_graph
        return payload

    @model_validator(mode="before")
    @classmethod
    def import_legacy_result(cls, value):
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        payload.pop("relevant_graph", None)
        graph = payload.pop("subgraph", None)
        conflicts = payload.pop("node_conflicts", None)
        if graph is not None:
            imported = {"graph": graph}
            if conflicts is not None:
                imported["node_conflicts"] = conflicts
            legacy = GraphState.model_validate(imported)
            if (
                payload.get("retrieved_graph_state") is not None
                and GraphState.model_validate(payload["retrieved_graph_state"])
                != legacy
            ):
                raise ValueError("legacy subgraph differs from retrieved_graph_state")
            payload["retrieved_graph_state"] = legacy
        elif conflicts:
            raise ValueError("legacy conflicts require their graph")
        return payload

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status == "needs_clarification":
            if self.pending is None or not self.clarification_slots:
                raise ValueError(
                    "clarification result requires pending state and slots"
                )
        elif self.pending is not None or self.clarification_slots:
            raise ValueError("terminal result cannot contain clarification state")
        if self.status == "completed" and self.subgraph is None:
            raise ValueError("completed result requires a subgraph")
        return self


class RetrieveDbEngine(Protocol):
    async def retrieve(
        self,
        *,
        question: str,
        graph_state: GraphState,
        graph_fingerprint: str,
        dialect: str,
        initial_sql: str | None,
        enable_clarification: bool,
    ) -> RetrieveDbResult: ...

    async def resume(
        self,
        *,
        graph: GraphSpace,
        pending: RetrieveDbPending,
        answers: tuple[ClarificationAnswer, ...],
    ) -> RetrieveDbResult: ...


class RetrieveDbService:
    def __init__(
        self,
        engine: RetrieveDbEngine,
        *,
        schema: SchemaDefinition,
        default_enable_clarification: bool = False,
    ) -> None:
        self._engine = engine
        self._schema = schema
        self._default_enable_clarification = default_enable_clarification

    @property
    def context_fingerprint(self) -> str:
        """Stable retrieval implementation identity for dependent workflow recovery."""
        value = getattr(self._engine, "context_fingerprint", None)
        if value is not None:
            return str(value)
        from cortex.schema import calculate_schema_hash

        return calculate_schema_hash(self._schema)

    async def run(
        self,
        *,
        question: str,
        graph: GraphSpace | None = None,
        graph_state: GraphState | None = None,
        column_context: tuple[ColumnSemanticContext, ...] = (),
        run_id: str | None = None,
        dialect: str = "mysql",
        initial_sql: str | None = None,
        enable_clarification: bool | None = None,
        node_conflicts: tuple[NodeMergeConflict, ...] | None = None,
    ) -> RetrieveDbResult:
        normalized = question.strip()
        if not normalized:
            raise RetrieveDbError("retrieve_db question must not be empty")
        if graph_state is not None and graph is not None:
            raise RetrieveDbError("supply graph_state or graph, not both")
        if graph_state is None:
            if graph is None:
                raise RetrieveDbError("retrieve_db requires graph_state or graph")
            imported = {"graph": graph}
            if node_conflicts is not None:
                imported["node_conflicts"] = node_conflicts
            graph_state = GraphState.model_validate(imported)
        elif node_conflicts:
            raise RetrieveDbError(
                "supply legacy conflicts only with a legacy graph input"
            )
        graph_state = GraphState.model_validate(graph_state.model_dump(mode="python"))
        copied = graph_state.graph
        from cortex.application.ontology_graph import GraphManager

        GraphManager(self._schema).validate_state(graph_state)
        fingerprint = calculate_graph_fingerprint(copied)
        call_kwargs = {}
        if column_context:
            call_kwargs["column_context"] = column_context
        if run_id is not None:
            call_kwargs["run_id"] = run_id
        raw = await self._engine.retrieve(
            question=normalized,
            graph_state=graph_state,
            graph_fingerprint=fingerprint,
            dialect=dialect.strip() or "mysql",
            initial_sql=initial_sql.strip()
            if initial_sql and initial_sql.strip()
            else None,
            enable_clarification=(
                self._default_enable_clarification
                if enable_clarification is None
                else enable_clarification
            ),
            **call_kwargs,
        )
        expected = tuple(
            node
            for node in graph_state.conflict_nodes
            if raw.subgraph and node.id in {n.id for n in raw.subgraph.nodes}
        )
        if (
            raw.retrieved_graph_state.conflict_nodes
            if raw.retrieved_graph_state
            else ()
        ) != expected:
            raise RetrieveDbError("result conflict nodes differ from input")
        return _validate_result(raw, copied, normalized, fingerprint, self._schema)

    async def resume(
        self,
        *,
        graph: GraphSpace,
        pending: RetrieveDbPending,
        answers: tuple[ClarificationAnswer, ...],
    ) -> RetrieveDbResult:
        copied = GraphSpace.model_validate(graph.model_dump(mode="python"))
        from cortex.application.ontology_graph import GraphManager

        GraphManager(self._schema).validate_state(GraphState(graph=copied))
        fingerprint = calculate_graph_fingerprint(copied)
        if fingerprint != pending.graph_fingerprint:
            raise RetrieveDbError(
                "pending graph fingerprint does not match input graph"
            )
        _validate_answers(pending, answers)
        raw = await self._engine.resume(graph=copied, pending=pending, answers=answers)
        return _validate_result(
            raw, copied, pending.question, fingerprint, self._schema
        )


def calculate_graph_fingerprint(graph: GraphSpace) -> str:
    payload = graph.model_dump(mode="json")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _validate_answers(
    pending: RetrieveDbPending, answers: tuple[ClarificationAnswer, ...]
) -> None:
    slots = {slot.slot_id: slot for slot in pending.clarification_slots}
    supplied = {answer.slot_id: answer for answer in answers}
    if len(supplied) != len(answers) or set(supplied) != set(slots):
        raise RetrieveDbError(
            "answers must cover every clarification slot exactly once"
        )
    for slot_id, answer in supplied.items():
        allowed = {candidate.node_id for candidate in slots[slot_id].candidates}
        if answer.selected_node_id not in allowed:
            raise RetrieveDbError(
                f"answer is outside clarification candidates: {slot_id}"
            )


def _validate_result(
    raw: RetrieveDbResult,
    graph: GraphSpace,
    question: str,
    fingerprint: str,
    schema: SchemaDefinition,
) -> RetrieveDbResult:
    result = RetrieveDbResult.model_validate(
        raw.model_dump(mode="python", exclude_computed_fields=True)
    )
    if result.question != question or result.graph_fingerprint != fingerprint:
        raise RetrieveDbError("retrieve_db result identity does not match request")
    nodes = {node.id: node for node in graph.nodes}
    edges = {edge.id: edge for edge in graph.edges}
    candidate_ids = {candidate.candidate_id for candidate in result.candidates}
    candidate_nodes = {candidate.node_id for candidate in result.candidates}
    if (
        len(candidate_ids) != len(result.candidates)
        or not candidate_nodes <= nodes.keys()
    ):
        raise RetrieveDbError("retrieve_db candidates are invalid or duplicated")
    if any(
        selection.candidate_id not in candidate_ids for selection in result.selections
    ):
        raise RetrieveDbError("retrieve_db selection references an unknown candidate")
    if set(result.raw_retrieved_node_ids) != candidate_nodes:
        raise RetrieveDbError("raw retrieved node ids do not match candidates")
    selected_from_decisions = {
        candidate.node_id
        for candidate in result.candidates
        for selection in result.selections
        if selection.candidate_id == candidate.candidate_id
        and selection.status == "selected"
    }
    if set(result.selected_node_ids) != selected_from_decisions:
        raise RetrieveDbError("selected node ids do not match selections")
    for values in (
        result.raw_retrieved_node_ids,
        result.selected_node_ids,
        result.relevant_ontology_asset_node_ids,
        result.dependency_node_ids,
        result.generation_rule_node_ids,
        result.seed_node_ids,
    ):
        if not set(values) <= nodes.keys():
            raise RetrieveDbError(
                "retrieve_db result references a node outside input graph"
            )
    expected_relevant_assets = {
        node_id
        for node_id in result.selected_node_ids
        if nodes[node_id].node_type in {"concept", "metric", "generation_rule"}
    }
    if set(result.relevant_ontology_asset_node_ids) != expected_relevant_assets:
        raise RetrieveDbError(
            "relevant ontology assets must equal selected semantic asset nodes"
        )
    if result.subgraph is not None:
        for node in result.subgraph.nodes:
            if node.id not in nodes or node != nodes[node.id]:
                raise RetrieveDbError(
                    f"subgraph contains a modified or unknown node: {node.id}"
                )
        subgraph_nodes = {node.id for node in result.subgraph.nodes}
        for edge in result.subgraph.edges:
            if edge.id not in edges or edge != edges[edge.id]:
                raise RetrieveDbError(
                    f"subgraph contains a modified or unknown edge: {edge.id}"
                )
            if {edge.from_node_id, edge.to_node_id} - subgraph_nodes:
                raise RetrieveDbError(
                    f"subgraph edge has a missing endpoint: {edge.id}"
                )
        from cortex.application.ontology_graph import GraphManager

        GraphManager(schema).validate_state(GraphState(graph=result.subgraph))
    return result


__all__ = [
    "CandidateEvidence",
    "ClarificationAnswer",
    "ClarificationSlot",
    "EntityMention",
    "MentionRole",
    "NodeCandidate",
    "NodeSelection",
    "RetrieveDbEngine",
    "RetrieveDbError",
    "RetrieveDbPending",
    "RetrieveDbResult",
    "RetrieveDbService",
    "RetrieveDbStatus",
    "RetrieveDbTraceStep",
    "SqlColumnReference",
    "SqlLiteralSignal",
    "SqlSignals",
    "TypedRetrievalPath",
    "calculate_graph_fingerprint",
]
