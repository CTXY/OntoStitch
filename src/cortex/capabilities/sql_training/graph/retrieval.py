from __future__ import annotations

from collections.abc import Sequence

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.adapter import NodeGraphAdapter
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.schema import GraphSpace, SchemaDefinition

_DATABASE_ASSET_TYPES = (
    NodeType.TABLE,
    NodeType.COLUMN,
    NodeType.DIMENSION_VALUE,
    NodeType.JOIN_PATH,
)
_CASE_SEMANTIC_TYPES = {
    NodeType.CONCEPT,
    NodeType.METRIC,
    NodeType.GENERATION_RULE,
}


class CaseRetrievalGraphError(ValueError):
    """The case-local retrieval graph cannot be assembled safely."""


class CaseRetrievalGraphBuilder:
    """Build the current database graph plus exactly the supplied case semantic assets."""

    def __init__(self, workspace: Workspace, schema: SchemaDefinition) -> None:
        self._workspace = workspace
        self._schema = schema

    def build(self, semantic_assets: Sequence[NodeAsset]) -> GraphSpace:
        invalid = sorted(
            asset.id for asset in semantic_assets if asset.node_type not in _CASE_SEMANTIC_TYPES
        )
        if invalid:
            raise CaseRetrievalGraphError(
                f"case retrieval graph only accepts semantic assets: {invalid}"
            )
        semantic_ids = [asset.id for asset in semantic_assets]
        if len(semantic_ids) != len(set(semantic_ids)):
            raise CaseRetrievalGraphError("case retrieval graph contains duplicate semantic IDs")

        database_assets = [
            asset
            for node_type in _DATABASE_ASSET_TYPES
            for asset in self._workspace.read_draft_assets(node_type)
        ]
        database_ids = {asset.id for asset in database_assets}
        collisions = sorted(database_ids.intersection(semantic_ids))
        if collisions:
            raise CaseRetrievalGraphError(
                f"semantic assets collide with database assets: {collisions}"
            )
        missing_values = sorted(
            {
                reference
                for asset in semantic_assets
                for reference in asset.references()
                if reference.startswith("dimension_value.")
                and reference not in database_ids
            }
        )
        if missing_values:
            raise CaseRetrievalGraphError(
                "semantic assets reference Dimension Values absent from verified database "
                f"assets: {missing_values}"
            )

        graph, warnings = NodeGraphAdapter(self._workspace).to_authoring_graph(
            self._schema,
            assets=(*database_assets, *semantic_assets),
        )
        if warnings:
            raise CaseRetrievalGraphError(
                f"case retrieval graph projection is incomplete: {list(warnings)}"
            )
        node_types = {node.node_type for node in graph.nodes}
        if "table" not in node_types or "column" not in node_types:
            raise CaseRetrievalGraphError(
                "case retrieval graph requires current-database table and column nodes"
            )
        if "join_path" in node_types:
            raise CaseRetrievalGraphError("join paths must be represented as graph edges")
        return graph


__all__ = ["CaseRetrievalGraphBuilder", "CaseRetrievalGraphError"]
