"""Compatibility imports; retrieval is owned by retrieve_db."""

from cortex.capabilities.retrieve_db.retrieval.index import (
    RepresentationIndex,
    _canonical_document,
    _column_context,
    _content_document,
    _contexts_for,
    _label_matches_question,
    _labels,
    _list,
    _meaningful,
    _physical_labels,
    _searchable,
    _sql_identifiers,
)

__all__ = [
    "RepresentationIndex",
    "_canonical_document",
    "_column_context",
    "_content_document",
    "_contexts_for",
    "_label_matches_question",
    "_labels",
    "_list",
    "_meaningful",
    "_physical_labels",
    "_searchable",
    "_sql_identifiers",
]
