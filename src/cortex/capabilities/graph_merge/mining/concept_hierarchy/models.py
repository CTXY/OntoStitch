from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from cortex.capabilities.graph_merge.models import NodeContent, _GraphMergeModel
from cortex.capabilities.graph_merge.rewrite.contracts import GraphRewriteOperations
from cortex.schema import GraphEdge, SourceReference


class ConceptNodeAlternative(_GraphMergeModel):
    content: NodeContent
    sources: tuple[SourceReference, ...] = ()


class ConceptNodeView(_GraphMergeModel):
    id: str
    node_type: str
    content: NodeContent
    alternatives: tuple[ConceptNodeAlternative, ...] = ()
    sources: tuple[SourceReference, ...] = ()


class ConceptTrigger(_GraphMergeModel):
    kind: Literal[
        "concept_maps_to_multiple_values",
        "value_mapped_by_multiple_concepts",
    ]
    subject_id: str
    related_ids: tuple[str, ...]


class ConceptLocalSubgraph(_GraphMergeModel):
    component_id: str
    triggers: tuple[ConceptTrigger, ...]
    nodes: tuple[ConceptNodeView, ...]
    edges: tuple[GraphEdge, ...]


class ConceptMinerOutcome(_GraphMergeModel):
    action: Literal["no_change", "rewrite"]
    operations: GraphRewriteOperations | None = None
    reason: str

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.action == "no_change" and self.operations is not None:
            raise ValueError("no_change outcome cannot contain rewrite operations")
        if self.action == "rewrite" and (
            self.operations is None or self.operations.is_empty()
        ):
            raise ValueError("rewrite outcome requires non-empty operations")
        return self
