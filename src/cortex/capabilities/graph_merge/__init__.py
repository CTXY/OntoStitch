"""Protocol-independent structured graph merge capability."""

from cortex.capabilities.graph_merge.edges import normalize_edges
from cortex.capabilities.graph_merge.identity import IdentityGrouper, exact_identity_key
from cortex.capabilities.graph_merge.operator import GraphMergeOperator

__all__ = [
    "GraphMergeOperator",
    "IdentityGrouper",
    "exact_identity_key",
    "normalize_edges",
]
