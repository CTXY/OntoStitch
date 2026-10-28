"""Compatibility API; production retrieval uses retrieval/ and workflow/."""

from cortex.capabilities.retrieve_db.legacy.closure import (
    ClosureResult,
    TypedGraphClosure,
)

__all__ = ["ClosureResult", "TypedGraphClosure"]
