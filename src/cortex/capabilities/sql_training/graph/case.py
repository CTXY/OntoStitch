"""Construct case fragments using real physical endpoints and GraphManager."""

from collections.abc import Iterable

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.sql_training.graph.nodes import build_node
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.schema import GraphSpace, GraphState, load_root_schema


def project_case_graph(
    assets: Iterable[NodeAsset], *, physical_graph: GraphSpace
) -> GraphSpace:
    state = GraphState(
        graph=GraphSpace(nodes=tuple(build_node(a) for a in assets))
    )
    return (
        GraphManager(load_root_schema())
        .prepare_fragment(state, existing=physical_graph)
        .graph
    )
