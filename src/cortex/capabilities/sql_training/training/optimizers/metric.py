from __future__ import annotations

from typing import Any

from cortex.capabilities.sql_training.contracts.metric import (
    metric_dependency_diagnostics,
    validate_metric_contract,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.training.optimizers.base import (
    BaseAssetOptimizer,
    _base_retry_instruction,
)
from cortex.capabilities.sql_training.training.repair.contracts import OptimizationOwner
from cortex.capabilities.sql_training.training.repair.plan import CandidateInitializationResult


class MetricOptimizer(BaseAssetOptimizer):
    """Create or revise reusable metric calculation contracts."""

    def __init__(self, provider: Any):
        super().__init__(
            provider,
            owner=OptimizationOwner.METRIC,
            prompt_name="train_residual_metric_optimizer",
        )

    def validate_initialization_identity(
        self,
        base_asset: NodeAsset | None,
        result: CandidateInitializationResult,
    ) -> None:
        if base_asset is None:
            return
        for candidate in result.candidates:
            if candidate.asset.name != base_asset.name:
                raise ValueError("Metric update changed the accepted metric name")

    def validate_refinement_identity(
        self,
        original: NodeAsset,
        replacement: NodeAsset,
    ) -> None:
        if replacement.name != original.name:
            raise ValueError("Metric refinement changed the metric name")

    def validate_assets(
        self,
        assets: list[NodeAsset],
        *,
        working_assets: list[NodeAsset],
        evidence: dict[str, Any],
    ) -> None:
        del evidence
        for asset in assets:
            validate_metric_candidate_contract(asset)
        current_metrics = [
            asset for asset in working_assets if asset.node_type == NodeType.METRIC
        ]
        diagnostics = metric_dependency_diagnostics(current_metrics, assets)
        if diagnostics:
            raise ValueError(str([item.as_feedback() for item in diagnostics]))

    def retry_instruction(self, error: str, *, refinement: bool = False) -> str:
        base = _base_retry_instruction(error, refinement=refinement)
        if "MISSING_METRIC_DEPENDENCY" in error:
            return (
                f"{base} The referenced Metric does not exist in working_ontology or "
                "dependency_assets. Reuse an exact visible Metric ID, or create the missing "
                "Metric in dependency_assets. Write the dependency in both expression.refs "
                "and properties.references. Do not copy a visible Metric into dependency_assets "
                "or replace an independently meaningful Metric with a raw Column."
            )
        if "must not contain complete SQL" in error:
            return (
                f"{base} Keep generation-plan steps short and imperative. Describe reusable "
                "operations without SELECT, FROM, WITH, or a complete SQL statement."
            )
        if "pattern placeholders" in error:
            return (
                f"{base} Make every pattern placeholder resolve through expression.refs. "
                "Do not add query filter, breakdown, ranking, or output-format details."
            )
        if "query-instance values" in error:
            return (
                f"{base} Remove every reported query-instance value and its surrounding time, "
                "entity, grouping, or report-scope instruction from description, pattern, "
                "application, and steps. Keep only the scope-free Metric calculation; do not "
                "replace a rejected concrete scope with an example of the same scope."
            )
        if "dependencies not used by expression" in error:
            return (
                f"{base} Remove the listed unused references. A Metric declares only its direct "
                "expression operands; plan-only physical tokens stay local to each plan. Do not "
                "copy transitive physical references from child Metrics."
            )
        return (
            f"{base} Preserve the reusable calculation contract and use only accepted visible "
            "Metric dependencies or grounded physical references."
        )


def validate_metric_candidate_contract(
    asset: NodeAsset,
    *,
    database_dialect: str | None = None,
) -> None:
    """Apply the reusable Metric quality gates before a candidate reaches a trial."""

    validate_metric_contract(asset, require_generation_plan=True, require_pattern=True)
    if str(database_dialect or "").casefold() != "mysql":
        return
    plan_text = " ".join(
        [
            asset.properties.pattern,
            *(
                text
                for plan in asset.properties.sql_generation_plans
                for text in (plan.application, *plan.steps)
            ),
        ]
    ).upper()
    forbidden = [
        token
        for token in ("FULL OUTER JOIN", "NULLS FIRST", "NULLS LAST")
        if token in plan_text
    ]
    if forbidden:
        raise ValueError(f"Metric generation guidance is not valid for MySQL: {forbidden}")
