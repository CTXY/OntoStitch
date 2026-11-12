from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cortex.schema import GraphState, SchemaDefinition


class _MergeApplicationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiagnosticCode(StrEnum):
    CONSOLIDATION_PRESERVED_UNRESOLVED = "CONSOLIDATION_PRESERVED_UNRESOLVED"
    MODEL_FAILURE = "MODEL_FAILURE"
    INVALID_SELECTION = "INVALID_SELECTION"
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    EVIDENCE_LIMIT_REACHED = "EVIDENCE_LIMIT_REACHED"
    CROSS_TYPE_AMBIGUOUS = "CROSS_TYPE_AMBIGUOUS"
    EDGE_CONTENT_VARIANT = "EDGE_CONTENT_VARIANT"
    STALE_REFERENCE_EDGE_DROPPED = "STALE_REFERENCE_EDGE_DROPPED"
    INVALIDATED_RELATIONSHIP_DROPPED = "INVALIDATED_RELATIONSHIP_DROPPED"


class CanonicalIdMapping(_MergeApplicationModel):
    input_id: str
    canonical_id: str


class MergeDiagnostic(_MergeApplicationModel):
    code: DiagnosticCode
    element_kind: Literal["node", "edge", "merge"]
    element_id: str | None = None
    message: str


class MergeElementRef(_MergeApplicationModel):
    kind: Literal["node", "edge"]
    id: str
    view: Literal["existing", "incoming", "result"]
    alternative_id: str | None = None


class StructuralRewrite(_MergeApplicationModel):
    """A joint semantic correspondence, never an automatic ID substitution."""

    before: tuple[MergeElementRef, ...]
    after: tuple[MergeElementRef, ...]
    explanation: str
    base_revision: str
    result_revision: str


class GraphMergeResult(_MergeApplicationModel):
    run_id: str | None = None
    state: GraphState
    node_mappings: tuple[CanonicalIdMapping, ...]
    edge_mappings: tuple[CanonicalIdMapping, ...]
    diagnostics: tuple[MergeDiagnostic, ...]
    rewrites: tuple[StructuralRewrite, ...] = ()
    existing_node_mappings: tuple[CanonicalIdMapping, ...] = ()
    existing_edge_mappings: tuple[CanonicalIdMapping, ...] = ()


class GraphMergeUseCase(Protocol):
    def merge(
        self,
        existing: GraphState,
        incoming: GraphState,
        schema: SchemaDefinition,
        *,
        run_id: str | None = None,
    ) -> GraphMergeResult: ...
