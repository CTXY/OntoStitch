from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cortex.schema import GraphState, SchemaDefinition


class _MergeApplicationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiagnosticCode(StrEnum):
    CONSOLIDATION_PRESERVED_UNRESOLVED = "CONSOLIDATION_PRESERVED_UNRESOLVED"
    GENERALIZER_UNAVAILABLE = "GENERALIZER_UNAVAILABLE"
    FUSER_UNAVAILABLE = "FUSER_UNAVAILABLE"
    MODEL_FAILURE = "MODEL_FAILURE"
    INVALID_SELECTION = "INVALID_SELECTION"
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    EVIDENCE_LIMIT_REACHED = "EVIDENCE_LIMIT_REACHED"
    BRANCH_SELECTION_AMBIGUOUS = "BRANCH_SELECTION_AMBIGUOUS"
    CROSS_TYPE_AMBIGUOUS = "CROSS_TYPE_AMBIGUOUS"
    INVALID_GENERALIZATION_PROPOSAL = "INVALID_GENERALIZATION_PROPOSAL"
    INVALID_FUSION_PROPOSAL = "INVALID_FUSION_PROPOSAL"
    EDGE_CONTENT_VARIANT = "EDGE_CONTENT_VARIANT"
    STALE_REFERENCE_EDGE_DROPPED = "STALE_REFERENCE_EDGE_DROPPED"
    INVALIDATED_RELATIONSHIP_DROPPED = "INVALIDATED_RELATIONSHIP_DROPPED"
    MINING_COMPONENT_LIMIT_REACHED = "MINING_COMPONENT_LIMIT_REACHED"
    MINING_PROPOSAL_LIMIT_REACHED = "MINING_PROPOSAL_LIMIT_REACHED"
    MINING_EVIDENCE_LIMIT_REACHED = "MINING_EVIDENCE_LIMIT_REACHED"
    MINER_MODEL_FAILURE = "MINER_MODEL_FAILURE"
    INVALID_MINER_PROPOSAL = "INVALID_MINER_PROPOSAL"
    INVALID_GRAPH_REWRITE_PROPOSAL = "INVALID_GRAPH_REWRITE_PROPOSAL"
    GRAPH_REWRITE_CONFLICT = "GRAPH_REWRITE_CONFLICT"
    GRAPH_REWRITE_CYCLE = "GRAPH_REWRITE_CYCLE"
    MINING_STAGE_ROLLED_BACK = "MINING_STAGE_ROLLED_BACK"


class CanonicalIdMapping(_MergeApplicationModel):
    input_id: str
    canonical_id: str


class MergeDiagnostic(_MergeApplicationModel):
    code: DiagnosticCode
    element_kind: Literal["node", "edge", "merge"]
    element_id: str | None = None
    message: str


class GraphMergeResult(_MergeApplicationModel):
    state: GraphState
    node_mappings: tuple[CanonicalIdMapping, ...]
    edge_mappings: tuple[CanonicalIdMapping, ...]
    diagnostics: tuple[MergeDiagnostic, ...]


class GraphMergeUseCase(Protocol):
    def merge(
        self,
        existing: GraphState,
        incoming: GraphState,
        schema: SchemaDefinition,
    ) -> GraphMergeResult: ...
