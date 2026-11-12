"""Shared Schema, graph, source, and knowledge data definitions."""

from cortex.schema.graph import (
    ConflictAlternative,
    ConflictChoice,
    ConflictPoint,
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    MergeStatus,
    NodeContent,
    NodeMergeConflict,
    SourceReference,
)
from cortex.schema.graph_io import export_inline_graph, with_conflicts
from cortex.schema.models import (
    EdgeDirectionality,
    EdgeType,
    FieldDefinition,
    IdentityMode,
    IdentityPolicy,
    MergeRole,
    NodeType,
    SchemaDefinition,
    SchemaFamily,
)
from cortex.schema.roots import load_root_schema
from cortex.schema.validation import (
    GraphSchemaError,
    InvalidGraphState,
    calculate_schema_hash,
    validate_graph_space,
    validate_graph_state,
    validate_incoming_graph_state,
)

__all__ = [
    "ConflictAlternative",
    "ConflictChoice",
    "ConflictPoint",
    "EdgeDirectionality",
    "EdgeType",
    "FieldDefinition",
    "GraphEdge",
    "GraphNode",
    "GraphSchemaError",
    "GraphSpace",
    "GraphState",
    "IdentityMode",
    "IdentityPolicy",
    "InvalidGraphState",
    "MergeRole",
    "MergeStatus",
    "NodeContent",
    "NodeMergeConflict",
    "NodeType",
    "SchemaDefinition",
    "SchemaFamily",
    "SourceReference",
    "calculate_schema_hash",
    "export_inline_graph",
    "load_root_schema",
    "validate_graph_space",
    "validate_graph_state",
    "validate_incoming_graph_state",
    "with_conflicts",
]
