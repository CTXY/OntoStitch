"""Explicit experiment entry: empty semantic Initial, one existing repair cycle.

Production training defaults remain in TrainPipeline. No alternate prompts or
acceptance rules are defined here.
"""

from __future__ import annotations

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.pipeline.train import TrainPipeline


class RepairOnlyTrainPipeline(TrainPipeline):
    def run(self, **kwargs):
        for name in ("max_rounds", "max_iterations"):
            if kwargs.get(name, 1) != 1:
                raise ValueError(f"repair_only requires {name}=1")
            kwargs[name] = 1
        return super().run(**kwargs)

    def _extract_case_candidates(self, record, **kwargs):
        kwargs["trace_logger"].log(
            source_id=str(record["source_id"]),
            case_index=kwargs["case_index"],
            step="ablation_initial_extraction_skipped",
            output={"candidate_node_ids": [], "setting": "repair_only"},
        )
        return {"candidate_counts": {}, "initial_metric_candidate_sets": [], "warnings": []}, []

    def _prepare_gold_join_edges(self, records, provider, trace_logger):
        trace_logger.log(
            source_id=None,
            step="ablation_frozen_physical_preparation",
            output={"catalog_edge_count": len(self.workspace.read_catalog_edges())},
        )

    def _bootstrap_dimension_values(self, records, run_id):
        return {
            "selected_column_refs": [],
            "dimension_value_count": len(
                self.workspace.read_draft_assets(NodeType.DIMENSION_VALUE)
            ),
            "reused_frozen_snapshot": True,
        }
