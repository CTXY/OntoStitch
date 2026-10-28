"""Read unresolved alternatives as reference contracts without resolving the Graph."""

from cortex.schema import GraphNode


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


def reference_contract(node: GraphNode, conflict: GraphNode, render) -> dict:
    return {
        "status": conflict.consolidation_status,
        "common": {
            key: getattr(node, key)
            for key in ("name", "description", "aliases", "tags", "properties")
        },
        "alternatives": [
            {
                "alternative_id": item.alternative_id,
                "overrides": item.overrides,
                "sources": [s.model_dump(mode="json") for s in item.sources],
            }
            for item in conflict.alternatives
        ],
        "conflict_points": [
            item.model_dump(mode="json") for item in conflict.conflict_points
        ],
    }
