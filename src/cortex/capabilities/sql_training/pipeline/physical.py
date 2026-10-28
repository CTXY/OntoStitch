"""Run-level physical preparation shared by the default and experiment entrypoints."""

from __future__ import annotations

from typing import Any

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.extractors.sql_normalizer import (
    collect_physical_column_references,
)
from cortex.capabilities.sql_training.graph.join_edges import (
    constraint_cardinality,
    extract_gold_join_edges,
    join_edge_key,
    merge_join_edges,
    pure_routing_paths,
)
from cortex.capabilities.sql_training.pipeline.grounding.bootstrap import (
    TrainingDimensionValueBootstrap,
)
from cortex.capabilities.sql_training.training.trace import (
    TrainTraceLogger,
)
from cortex.schema import GraphEdge


class PhysicalPreparation:
    def __init__(self, config, workspace, database, existing_join_edges=()):
        self.config, self.workspace, self.database = config, workspace, database
        self._existing_join_edges = existing_join_edges
        self._columns = workspace.read_draft_assets(NodeType.COLUMN)
        self._tables = workspace.read_draft_assets(NodeType.TABLE)
        self._table_names = {
            table.id: table.properties.table_name for table in self._tables
        }
        self._table_columns: dict[str, set[str]] = {}
        for column in self._columns:
            owner = next(
                ref for ref in column.properties.references if ref.startswith("table.")
            )
            self._table_columns.setdefault(self._table_names[owner], set()).add(
                column.properties.column_name
            )

        columns = self._columns
        self._metadata_by_ref = {
            asset.id: {
                "column": asset.properties.column_name,
                "table": next(
                    (
                        self._table_names.get(reference)
                        for reference in asset.properties.references
                        if reference.startswith("table.")
                    ),
                    None,
                ),
                "data_type": asset.properties.data_type,
                "description": asset.description,
                "source_metadata": asset.properties.source_metadata,
                "sample_values": list(asset.properties.sample_values),
                "nullable": asset.properties.nullable,
                "semantic_role": (
                    asset.properties.semantic_role.value
                    if asset.properties.semantic_role is not None
                    else None
                ),
                "key_role": (
                    asset.properties.key_role.value
                    if asset.properties.key_role is not None
                    else None
                ),
            }
            for asset in columns
        }

    def add_gold_column_metadata(self, record: dict[str, Any]) -> dict[str, Any]:
        """Attach compact metadata for Columns actually used by the Gold SQL."""

        metadata_by_ref = self._metadata_by_ref
        gold_refs = collect_physical_column_references(
            str(record["sql"]),
            available_column_refs=set(metadata_by_ref),
            dialect=self.config.database.dialect,
            table_columns=self._table_columns,
        )
        return {
            **record,
            "gold_column_metadata": {
                reference: metadata_by_ref[reference]
                for reference in sorted(gold_refs)
                if reference in metadata_by_ref
            },
        }

    def bootstrap_dimension_values(
        self, records: list[dict[str, Any]], run_id: str
    ) -> dict:
        return (
            TrainingDimensionValueBootstrap(
                self.config,
                self.workspace,
                self.database,
            )
            .run(records, run_id=run_id)
            .to_dict()
        )

    def prepare_gold_join_edges(
        self,
        records: list[dict[str, Any]],
        trace_logger: TrainTraceLogger,
    ) -> None:
        table_columns = self._table_columns
        existing = merge_join_edges(
            self._existing_join_edges,
            self.workspace.read_catalog_edges(),
        )
        extracted_edges: list[GraphEdge] = []
        case_edges: list[list[GraphEdge]] = []
        tables = {table.id: table.properties for table in self._tables}
        for index, record in enumerate(records, start=1):
            source_id = str(record["source_id"])
            skipped = []
            edges = extract_gold_join_edges(
                sql=str(record["sql"]),
                dialect=self.config.database.dialect,
                source_id=source_id,
                table_columns=table_columns,
                skipped=skipped,
            )
            constrained = []
            for edge in edges:
                left, right = tables[edge.from_node_id], tables[edge.to_node_id]
                left_refs = {
                    pair["from_column_ref"] for pair in edge.properties["column_pairs"]
                }
                right_refs = {
                    pair["to_column_ref"] for pair in edge.properties["column_pairs"]
                }
                cardinality = constraint_cardinality(
                    left_refs,
                    right_refs,
                    [
                        [make_node_id(NodeType.COLUMN, left.table_name, c) for c in key]
                        for key in [left.primary_keys, *left.unique_keys]
                    ],
                    [
                        [
                            make_node_id(NodeType.COLUMN, right.table_name, c)
                            for c in key
                        ]
                        for key in [right.primary_keys, *right.unique_keys]
                    ],
                )
                properties = dict(edge.properties)
                if cardinality:
                    properties["cardinality"] = cardinality
                constrained.append(edge.model_copy(update={"properties": properties}))
            edges = constrained
            extracted_edges.extend(edges)
            case_edges.append(edges)
            trace_logger.log(
                source_id=source_id,
                case_index=index,
                step="gold_join_edges_validated",
                phase="gold_join_edge_preparation",
                output={"edge_ids": [edge.id for edge in edges], "skipped": skipped},
                summary={"edge_count": len(edges), "skipped_count": len(skipped)},
            )
        merged = merge_join_edges(existing, extracted_edges)
        merged_by_key = {join_edge_key(edge): edge for edge in merged}
        for record, extracted in zip(records, case_edges, strict=True):
            canonical_edges = [merged_by_key[join_edge_key(edge)] for edge in extracted]
            record["routing_join_paths"] = pure_routing_paths(
                sql=str(record["sql"]),
                dialect=self.config.database.dialect,
                case_edges=canonical_edges,
            )
            endpoint_pairs = {
                frozenset(path["semantic_tables"])
                for path in record["routing_join_paths"]
            }
            record["direct_join_alternatives"] = [
                {
                    "edge_id": edge.id,
                    "tables": [edge.from_node_id, edge.to_node_id],
                    "column_pairs": edge.properties["column_pairs"],
                    **(
                        {"cardinality": edge.properties["cardinality"]}
                        if "cardinality" in edge.properties
                        else {}
                    ),
                }
                for edge in merged
                if frozenset({edge.from_node_id, edge.to_node_id}) in endpoint_pairs
            ]
        self.workspace.write_catalog_edges(merged)
        trace_logger.log(
            source_id=None,
            step="gold_join_edges_published_to_training_graph",
            phase="train_start",
            output={"edge_ids": [edge.id for edge in merged]},
            summary={
                "catalog_edge_count": len(existing),
                "gold_edge_count": len(extracted_edges),
                "merged_edge_count": len(merged),
            },
        )
