from __future__ import annotations

from collections.abc import Iterable

from cortex.capabilities.sql_training.graph.relationships import compile_all_relationships
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.schema import GraphEdge


def project_case_edges(assets: Iterable[NodeAsset]) -> tuple[GraphEdge, ...]:
    """Compile the open GraphFragments persisted for one training case."""

    return compile_all_relationships(assets).outgoing_edges
