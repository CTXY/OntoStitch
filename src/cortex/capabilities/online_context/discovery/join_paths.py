"""Construct JOIN evidence for all three discovery stages; never choose a path."""

from cortex.schema import GraphNode, GraphSpace, GraphState
from cortex.schema.join_retrieval import join_path_candidates as graph_join_path_candidates
from cortex.schema.join_retrieval import semantic_tables_from_sql as graph_semantic_tables_from_sql


def semantic_tables_from_sql(sql: str | None, state: GraphState) -> set[str]:
    return graph_semantic_tables_from_sql(sql, state.graph)


def join_path_candidates(
    state: GraphState,
    initial_sql: str | None,
    *,
    semantic_tables: set[str] | None = None,
) -> list[dict]:
    """Enumerate paths without replacing graph JOIN types with the attempted SQL."""
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
    definitions = {node.id: node for node in graph.nodes if node.node_type == "generation_rule"}
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
    for conflict in state.node_conflicts:
        parent = next((node for node in nodes if node.id == conflict.node_id), None)
        if parent is None or parent.node_type != "generation_rule":
            continue
        for alternative in conflict.alternatives:
            nodes.append(
                GraphNode(
                    id=alternative.alternative_id,
                    node_type="generation_rule",
                    name=parent.name,
                    properties=alternative.content.get("properties") or {},
                )
            )
    return GraphSpace(nodes=tuple(nodes), edges=state.graph.edges)


def render_join_path(path: dict) -> str:
    joins = path["joins"]
    parts = [f"FROM {joins[0]['from_table'].removeprefix('table.')}"]
    for join in joins:
        condition = " AND ".join(
            f"{item['left_column_ref'].removeprefix('column.')} {item['operator']} "
            f"{item['right_column_ref'].removeprefix('column.')}"
            for item in join["conditions"]
        )
        parts.append(
            f"{join['join_type']} JOIN {join['to_table'].removeprefix('table.')} ON {condition}"
        )
    return " ".join(parts)
