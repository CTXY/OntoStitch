"""Shared Schema, graph, source, and knowledge data definitions."""

from cortex.schema.graph import (
    ConflictAlternative,
    ConflictChoice,
    ConflictPoint,
    GraphEdge,
    GraphFragment,
    GraphNode,
    GraphSpace,
    GraphState,
    MergeStatus,
    NodeMergeConflict,
    SourceReference,
)
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
from cortex.schema.ontology import effective_concept_grounding
from cortex.schema.retrieval import (
    RetrievalSection,
    RetrievalSnapshot,
    RetrievalSourceDocument,
    RetrievalUnit,
)
from cortex.schema.roots import load_root_schema
from cortex.schema.sources import SourceChunk, SourceCorpus, SourceDocument, SourceSection
from cortex.schema.validation import (
    GraphSchemaError,
    InvalidGraphState,
    calculate_schema_hash,
    validate_graph_space,
    validate_graph_state,
    validate_incoming_graph_state,
)

__all__ = [
    "EdgeType",
    "EdgeDirectionality",
    "ConflictAlternative",
    "ConflictChoice",
    "ConflictPoint",
    "FieldDefinition",
    "GraphEdge",
    "GraphNode",
    "GraphSchemaError",
    "InvalidGraphState",
    "GraphSpace",
    "GraphState",
    "GraphFragment",
    "MergeStatus",
    "NodeMergeConflict",
    "IdentityMode",
    "IdentityPolicy",
    "MergeRole",
    "NodeType",
    "RetrievalSection",
    "RetrievalSnapshot",
    "RetrievalSourceDocument",
    "RetrievalUnit",
    "SchemaDefinition",
    "SchemaFamily",
    "SourceChunk",
    "SourceCorpus",
    "SourceDocument",
    "SourceReference",
    "SourceSection",
    "calculate_schema_hash",
    "effective_concept_grounding",
    "load_root_schema",
    "validate_graph_space",
    "validate_graph_state",
    "validate_incoming_graph_state",
]
