"""Compatibility API; production retrieval uses retrieval/ and workflow/."""

from cortex.capabilities.retrieve_db.legacy.normalization import (
    normalize_text,
    tokenize,
)

__all__ = ["normalize_text", "tokenize"]
