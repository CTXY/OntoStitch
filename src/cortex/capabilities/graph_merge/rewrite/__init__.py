"""Validated graph rewrite contracts and atomic proposal application."""

from cortex.capabilities.graph_merge.rewrite.applier import GraphRewriteApplier, GraphRewriteResult
from cortex.capabilities.graph_merge.rewrite.contracts import (
    AddEdgeProposal,
    AddNodeProposal,
    GraphRewriteOperations,
    GraphRewriteProposal,
)

__all__ = [
    "AddEdgeProposal",
    "AddNodeProposal",
    "GraphRewriteApplier",
    "GraphRewriteOperations",
    "GraphRewriteProposal",
    "GraphRewriteResult",
]
