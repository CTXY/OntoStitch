from __future__ import annotations

from typing import Any

from pydantic import Field

from cortex.capabilities.graph_merge.models import NodeContent, _GraphMergeModel


class AddNodeProposal(_GraphMergeModel):
    temporary_id: str = Field(min_length=1)
    node_type: str
    content: NodeContent


class AddEdgeProposal(_GraphMergeModel):
    temporary_id: str = Field(min_length=1)
    edge_type: str
    from_ref: str
    to_ref: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphRewriteOperations(_GraphMergeModel):
    add_nodes: tuple[AddNodeProposal, ...] = ()
    add_edges: tuple[AddEdgeProposal, ...] = ()
    remove_edge_ids: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.add_nodes or self.add_edges or self.remove_edge_ids)


class GraphRewriteProposal(_GraphMergeModel):
    miner_id: str
    component_id: str
    proposal_key: str
    reason: str
    operations: GraphRewriteOperations
