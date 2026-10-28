"""Construct JOIN evidence for all three discovery stages; never choose a path."""

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.retrieve_db.join_paths import (
    join_path_candidates as graph_join_path_candidates,
)
from cortex.capabilities.retrieve_db.join_paths import (
    semantic_tables_from_sql as graph_semantic_tables_from_sql,
)
from cortex.schema import GraphNode, GraphSpace, GraphState


def semantic_tables_from_sql(sql: str | None, state: GraphState) -> set[str]:
    return graph_semantic_tables_from_sql(sql, state.graph)


def join_path_candidates(
    state: GraphState,
    initial_sql: str | None,
    *,
    semantic_tables: set[str] | None = None,
) -> list[dict]:
    """Enumerate physical keys; query semantics determine the SQL JOIN operator."""
    endpoints = (
        semantic_tables
        if semantic_tables is not None
        else semantic_tables_from_sql(initial_sql, state)
    )
    graph = _with_rule_alternatives(state)
    return graph_join_path_candidates(graph, endpoints)


def discovery_join_candidates(state: GraphState) -> list[dict]:
    """Endpoints are possible roles, not an obligation to connect every retrieved table."""
    graph = _with_rule_alternatives(state)
    definitions = {
        node.id: node for node in graph.nodes if node.node_type == "generation_rule"
    }
    paths = graph_join_path_candidates(
        graph,
        {node.id for node in graph.nodes if node.node_type == "table"},
    )
    result = []
    for path in paths:
        rule_id = path["joinability_rule_id"]
        rule = definitions.get(rule_id)
        result.append(
            {
                **path,
                "statement": render_join_path(path),
                "graph_refs": [*path["edge_ids"], *([rule_id] if rule_id else [])],
                "supporting_definition": rule.model_dump(mode="json") if rule else None,
            }
        )
    return result


def _with_rule_alternatives(state: GraphState) -> GraphSpace:
    # The shared path enumerator skips unresolved parent rules. Expose each complete
    # alternative under its real evidence ID so conflicts remain visible to discovery.
    nodes = list(state.graph.nodes)
    for conflict in state.conflict_nodes:
        parent = next((node for node in nodes if node.id == conflict.id), None)
        if parent is None or parent.node_type != "generation_rule":
            continue
        for alternative in conflict.alternatives:
            nodes.append(
                GraphNode(
                    id=alternative.alternative_id,
                    node_type="generation_rule",
                    name=parent.name,
                    properties=GraphManager.resolve_alternative(
                        parent, alternative
                    ).properties,
                )
            )
    return GraphSpace(nodes=tuple(nodes), edges=state.graph.edges)


def render_join_path(path: dict) -> str:
    joins = path["joins"]
    parts = []
    for join in joins:
        keys = " AND ".join(
            f"{pair['from_column_ref'].removeprefix('column.')} = "
            f"{pair['to_column_ref'].removeprefix('column.')}"
            for pair in join["column_pairs"]
        )
        cardinality = f" ({join['cardinality']})" if join.get("cardinality") else ""
        parts.append(f"{keys}{cardinality}")
    return "; ".join(parts)
