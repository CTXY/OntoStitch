"""Read unresolved alternatives as reference contracts without resolving the Graph."""

from cortex.schema import GraphNode, NodeMergeConflict


def alternative_node(node: GraphNode, content: dict) -> GraphNode:
    return GraphNode(
        id=node.id,
        node_type=node.node_type,
        name=str(content.get("name") or node.name),
        description=content.get("description"),
        aliases=content.get("aliases") or [],
        tags=content.get("tags") or [],
        properties=content.get("properties") or {},
    )


def contract_references(node: GraphNode) -> set[str]:
    refs = set(map(str, node.properties.get("references") or []))
    expression = node.properties.get("expression")
    if isinstance(expression, dict) and isinstance(expression.get("refs"), dict):
        refs.update(map(str, expression["refs"].values()))
    for target in node.properties.get("targets") or []:
        if isinstance(target, dict):
            refs.update(map(str, target.get("references") or []))
            refs.update(
                str(item["node_id"])
                for item in target.get("semantic_targets") or []
                if isinstance(item, dict) and item.get("node_id")
            )
    if node.properties.get("column_ref"):
        refs.add(str(node.properties["column_ref"]))
    if node.properties.get("table_ref"):
        refs.add(str(node.properties["table_ref"]))
    return refs


def reference_contract(node: GraphNode, conflict: NodeMergeConflict, render) -> dict:
    return {
        "status": "unresolved reference alternatives; no confirmed default",
        "alternatives": [
            {
                "alternative_id": item.alternative_id,
                "name": item.content.get("name"),
                "contract": render(alternative_node(node, item.content)),
            }
            for item in conflict.alternatives
        ],
        "conflict_points": [item.model_dump(mode="json") for item in conflict.conflict_points],
    }
