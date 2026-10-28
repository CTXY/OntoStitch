"""Compatibility imports; retrieval is owned by retrieve_db."""

from cortex.capabilities.retrieve_db.retrieval.normalization import (
    normalize_text,
    tokenize,
)

__all__ = ["normalize_text", "tokenize"]
