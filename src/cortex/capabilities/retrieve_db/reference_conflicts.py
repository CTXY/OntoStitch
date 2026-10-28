"""Compatibility API; production retrieval uses retrieval/ and workflow/."""

from cortex.capabilities.retrieve_db.legacy.reference_conflicts import (
    alternative_node,
    reference_contract,
)

__all__ = ["alternative_node", "reference_contract"]
