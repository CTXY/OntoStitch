"""Run-level physical preparation shared by the default and experiment entrypoints."""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import expressions as exp

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.extractors.sql_normalizer import (
    build_column_table_map,
    collect_physical_column_references,
    normalize_sql_aliases,
)
from cortex.capabilities.sql_training.graph.join_edges import (
    build_gold_join_edges_from_llm_proposals,
    join_edge_key,
    merge_join_edges,
    pure_routing_paths,
)
from cortex.capabilities.sql_training.llm.prompts import load_prompt
from cortex.capabilities.sql_training.pipeline.grounding.bootstrap import (
    TrainingDimensionValueBootstrap,
)
from cortex.capabilities.sql_training.training.trace import (
    TracingLLMProvider,
    TrainTraceLogger,
)
from cortex.schema import GraphEdge


class PhysicalPreparation:
    def __init__(self, config, workspace, database, existing_join_edges=()):
        self.config, self.workspace, self.database = config, workspace, database
        self._existing_join_edges = existing_join_edges

    def add_gold_column_metadata(self, record: dict[str, Any]) -> dict[str, Any]:
        """Attach compact metadata for Columns actually used by the Gold SQL."""

        columns = self.workspace.read_draft_assets(NodeType.COLUMN)
        metadata_by_ref = {
            asset.id: {
                "column": asset.properties.column_name,
                "table": next(
                    (
                        reference.removeprefix("table.")
                        for reference in asset.properties.references
                        if reference.startswith("table.")
                    ),
                    None,
                ),
                "data_type": asset.properties.data_type,
                "description": asset.description,
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
        column_rows = [
            {
                "column_name": asset.properties.column_name,
                "table_name": metadata_by_ref[asset.id]["table"],
            }
            for asset in columns
        ]
        normalized_sql = normalize_sql_aliases(
            str(record["sql"]),
            column_table_map=build_column_table_map(column_rows),
            dialect=self.config.database.dialect,
        )
        gold_refs = collect_physical_column_references(
            normalized_sql,
            available_column_refs=set(metadata_by_ref),
            dialect=self.config.database.dialect,
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
        provider: TracingLLMProvider,
        trace_logger: TrainTraceLogger,
    ) -> None:
        if not getattr(provider.provider, "supports_join_edge_extraction", False):
            return
        table_columns: dict[str, set[str]] = {}
        for asset in self.workspace.read_draft_assets(NodeType.COLUMN):
            for reference in asset.properties.references:
                if reference.startswith("table."):
                    table_columns.setdefault(
                        reference.removeprefix("table."), set()
                    ).add(asset.properties.column_name)
        existing = merge_join_edges(
            self._existing_join_edges,
            self.workspace.read_catalog_edges(),
        )
        proposed_edges: list[GraphEdge] = []
        case_edges: list[list[GraphEdge]] = []
        for index, record in enumerate(records, start=1):
            source_id = str(record["source_id"])
            tree = sqlglot.parse_one(record["sql"], read=self.config.database.dialect)
            has_relation = any(tree.find_all(exp.Join)) or any(
                isinstance(eq.left, exp.Column)
                and isinstance(eq.right, exp.Column)
                and eq.left.table
                and eq.right.table
                and eq.left.table != eq.right.table
                for eq in tree.find_all(exp.EQ)
            )
            if not has_relation:
                case_edges.append([])
                continue
            provider.set_context(
                source_id=source_id,
                case_index=index,
                phase="gold_join_edge_preparation",
            )
            proposals = provider.extract_assets(
                {
                    "source_id": source_id,
                    "sql": record["sql"],
                    "database_dialect": self.config.database.dialect,
                    "table_columns": {
                        table: sorted(columns)
                        for table, columns in table_columns.items()
                    },
                },
                context={
                    "prompt_name": "gold_join_edges",
                    "prompt": load_prompt("gold_join_edges"),
                },
            )
            proposals, dropped_proposals = _drop_self_gold_join_proposals(
                proposals,
                table_columns=table_columns,
            )
            if dropped_proposals:
                trace_logger.log(
                    source_id=source_id,
                    case_index=index,
                    step="gold_join_edges_dropped",
                    phase="gold_join_edge_preparation",
                    output={"dropped": dropped_proposals},
                    summary={
                        "dropped_count": len(dropped_proposals),
                        "reasons": sorted(
                            {str(item["reason"]) for item in dropped_proposals}
                        ),
                    },
                )
            edges = build_gold_join_edges_from_llm_proposals(
                proposals=proposals,
                source_id=source_id,
                table_columns=table_columns,
            )
            proposed_edges.extend(edges)
            case_edges.append(edges)
            trace_logger.log(
                source_id=source_id,
                case_index=index,
                step="gold_join_edges_validated",
                phase="gold_join_edge_preparation",
                output={"edge_ids": [edge.id for edge in edges]},
                summary={"edge_count": len(edges)},
            )
        merged = merge_join_edges(existing, proposed_edges)
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
                    "join_type": edge.properties.get("default_join_type"),
                    "conditions": edge.properties.get("conditions"),
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
                "gold_proposal_count": len(proposed_edges),
                "merged_edge_count": len(merged),
            },
        )


def _drop_self_gold_join_proposals(
    proposals: list[Any],
    *,
    table_columns: dict[str, set[str]],
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Drop LLM self-relationship proposals while keeping other validation strict."""

    canonical_tables = {str(table).lower(): str(table) for table in table_columns}
    filtered: list[Any] = []
    dropped: list[dict[str, Any]] = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            filtered.append(proposal)
            continue
        from_table = canonical_tables.get(str(proposal.get("from_table") or "").lower())
        to_table = canonical_tables.get(str(proposal.get("to_table") or "").lower())
        if from_table is not None and from_table == to_table:
            dropped.append(
                {
                    "index": index,
                    "reason": "self_relationship",
                    "from_table": from_table,
                    "to_table": to_table,
                    "evidence_fragment": proposal.get("evidence_fragment"),
                    "sql_construct_type": proposal.get("sql_construct_type"),
                }
            )
            continue
        filtered.append(proposal)
    return filtered, dropped
