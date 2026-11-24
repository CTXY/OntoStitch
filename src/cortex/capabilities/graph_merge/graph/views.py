"""Read graph snapshots and render bounded object views without stage dependencies."""

def graph_view(context, view, *, before=None, candidate=None):
    """Never substitute input data for a missing before/candidate snapshot."""
    if view == "input":
        return context.nodes, context.edges, context.union_state().graph
    if view == "before":
        state = before or getattr(context, "task_before", None)
        if state is None and getattr(context, "active_batch", None) is not None:
            state = context.active_batch.base
    elif view == "candidate":
        state = candidate or context.candidate
    else:
        raise ValueError("view must be input, before, or candidate")
    if state is None:
        raise ValueError(f"No {view} graph snapshot is available")
    return ({node.id: node for node in state.graph.nodes},
            {edge.id: edge for edge in state.graph.edges}, state.graph)


def node_view(node, *, full=False, evidence_ids=None):
    result = {"id": node.id, "node_type": node.node_type, "name": node.name,
              "aliases": node.aliases}
    keys = ("definition", "action", "condition", "relation_metadata")
    properties = dict(node.properties) if full else {k: node.properties[k] for k in keys if k in node.properties}
    if properties:
        result["properties"] = properties
    if node.description and node.description != node.properties.get("definition"):
        result["description"] = node.description
    if node.alternatives:
        result["alternative_ids"] = [a.alternative_id for a in node.alternatives]
        result["conflict_points"] = [p.model_dump(mode="json") for p in node.conflict_points]
    common = getattr(node, "common_sources", ())
    if common and evidence_ids is not None:
        result["common_evidence_ids"] = sorted({i for source in common for i in evidence_ids.get(source, ())})
    return result


def physical_view(node, nodes, edges=()):
    """A physical ID is readable without exposing samples or catalog metadata."""
    row = {"id": node.id, "node_type": node.node_type, "name": node.name}
    if node.node_type == "table":
        row["table"] = node.properties.get("table_name", node.name)
    elif node.node_type == "column":
        table = nodes.get(node.properties.get("table_ref"))
        row["table"] = table.properties.get("table_name", table.name) if table else node.properties.get("table_ref")
        row["column"] = node.properties.get("column_name", node.name)
    elif node.node_type == "dimension_value":
        column = nodes.get(node.properties.get("column_ref"))
        if column is not None:
            parent = physical_view(column, nodes)
            row.update({key: parent[key] for key in ("table", "column") if key in parent})
        row["value"] = node.properties.get("value", node.name)
    owners = [edge for edge in edges if edge.edge_type == "grounded_on" and edge.to_node_id == node.id
              and nodes[edge.from_node_id].node_type == "concept"]
    if owners:
        row["owners"] = [{"id": edge.from_node_id, "edge_id": edge.id,
                          **({"alternative_ids": edge.properties["alternative_ids"]}
                             if edge.properties.get("alternative_ids") else {})} for edge in owners]
    return row


def edge_view(edge, evidence_ids):
    row = {"id": edge.id, "from_node_id": edge.from_node_id, "edge_type": edge.edge_type,
           "to_node_id": edge.to_node_id, "properties": dict(edge.properties),
           "evidence_ids": sorted({i for s in edge.sources for i in evidence_ids.get(s, ())})}
    return row


def semantic_summary(content):
    """Only relationship-relevant meaning, never the complete formula/reason twice."""
    row = {"name": content.name, "aliases": content.aliases}
    if content.description:
        row["description"] = content.description
    row["properties"] = {k: v for k, v in content.properties.items()
                         if k in {"definition", "condition", "action", "relation_metadata"}}
    return row

