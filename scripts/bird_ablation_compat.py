"""Explicit read compatibility for immutable pre-removal BIRD graph snapshots.

The normal authoring schema remains unchanged. Historical relation groups retain
their original bytes and all graph-wide edge/branch invariants remain enforced.
"""
from cortex.schema import FieldDefinition


def archived_graph_read_schema(schema):
    types = []
    for node_type in schema.node_types:
        if node_type.id == "concept":
            fields = dict(node_type.fields)
            fields["relation_metadata"] = FieldDefinition(
                type="array",
                required=False,
                description="Legacy archived relation groups: id, edges, description. Read compatibility only; preserve the immutable source graph.",
            )
            node_type = node_type.model_copy(update={"fields": fields})
        types.append(node_type)
    return schema.model_copy(update={"node_types": tuple(types)})
