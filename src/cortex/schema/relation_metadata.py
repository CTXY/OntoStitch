"""Group statements refer to authored edges, never infer new relations."""

from __future__ import annotations

from copy import deepcopy

from pydantic import Field, field_validator

from cortex.schema.models import _SchemaModel


class RelationMetadata(_SchemaModel):
    id: str = Field(min_length=1)
    edges: list[str] = Field(min_length=2)
    description: str = Field(min_length=1)

    @field_validator("id", "description")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("relation_metadata text must be nonempty")
        return value.strip()

    @field_validator("edges")
    @classmethod
    def distinct_edges(cls, values):
        if any(not v.strip() for v in values) or len(values) != len(set(values)):
            raise ValueError("relation_metadata requires distinct nonempty edge IDs")
        return values


def validate_metadata_items(items):
    if not isinstance(items, list):
        raise TypeError("relation_metadata must be a list")
    groups = [RelationMetadata.model_validate(item) for item in items]
    if len({g.id for g in groups}) != len(groups):
        raise ValueError("relation_metadata IDs must be unique within the Concept")
    return groups


def validate_relation_metadata(graph, *, owner_ids=None):
    from cortex.application.ontology_graph import GraphManager

    edges = {e.id: e for e in graph.edges}
    for node in graph.nodes:
        if owner_ids is not None and node.id not in owner_ids:
            continue
        variants = [(None, node.properties)] + [
            (a.alternative_id, GraphManager.resolve_alternative(node, a).properties)
            for a in node.alternatives
        ]
        for alternative, props in variants:
            if "relation_metadata" not in props:
                continue
            if node.node_type != "concept":
                raise ValueError("relation_metadata is only supported on Concept")
            for group in validate_metadata_items(props["relation_metadata"]):
                signature = set()
                branches = {}
                for ref in group.edges:
                    if ref not in edges:
                        raise ValueError(f"relation_metadata {node.id}/{group.id} references missing edge {ref}")
                    edge = edges[ref]
                    if node.id not in (edge.from_node_id, edge.to_node_id):
                        raise ValueError("relation_metadata edges must be adjacent to their owner")
                    outgoing = edge.from_node_id == node.id
                    signature.add((edge.edge_type, outgoing))
                    allowed = set(edge.properties.get("alternative_ids", []))
                    if allowed:
                        if alternative and outgoing and alternative not in allowed:
                            raise ValueError("relation_metadata edge is unavailable in its owner alternative")
                        source = edge.from_node_id
                        branches[source] = branches.get(source, allowed) & allowed
                if len(signature) != 1:
                    raise ValueError("relation_metadata edges require the same relation type and direction")
                if any(not allowed for allowed in branches.values()):
                    raise ValueError("relation_metadata edges cannot mix exclusive alternatives")


def remap_metadata_edges(properties, edge_mappings):
    """Edge identity maps stay separate from node identity maps."""
    result = deepcopy(properties)
    if isinstance(result.get("properties"), dict):
        result["properties"] = remap_metadata_edges(result["properties"], edge_mappings)
    for key in ("relation_metadata", "properties.relation_metadata"):
        for item in (result.get(key) or []):
            item["edges"] = [edge_mappings.get(ref, ref) for ref in item["edges"]]
    return result


def filter_metadata(properties, retained_edge_ids):
    """Omit incomplete statements in a view; do not mutate source knowledge."""
    result = dict(properties)
    if "relation_metadata" in result:
        groups = [g for g in result["relation_metadata"] if set(g["edges"]) <= retained_edge_ids]
        if groups:
            result["relation_metadata"] = groups
        else:
            result.pop("relation_metadata")
    return result
