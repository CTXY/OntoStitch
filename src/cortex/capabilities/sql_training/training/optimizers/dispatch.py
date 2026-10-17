from __future__ import annotations

from typing import Any

from cortex.capabilities.sql_training.training.optimizers.generation_rule import (
    GenerationRuleOptimizer,
)
from cortex.capabilities.sql_training.training.optimizers.metric import MetricOptimizer
from cortex.capabilities.sql_training.training.optimizers.semantic_mapping import (
    SemanticMappingOptimizer,
)
from cortex.capabilities.sql_training.training.repair.contracts import OptimizationOwner


class OptimizerDispatcher:
    """Resolve a frozen Repair Group owner to its concrete asset optimizer."""

    def __init__(self, provider: Any):
        self.optimizers = {
            OptimizationOwner.SEMANTIC_MAPPING: SemanticMappingOptimizer(provider),
            OptimizationOwner.METRIC: MetricOptimizer(provider),
            OptimizationOwner.GENERATION_RULE: GenerationRuleOptimizer(provider),
        }

    def for_owner(self, owner: OptimizationOwner):
        try:
            return self.optimizers[owner]
        except KeyError as exc:
            raise ValueError(f"no optimizer for owner: {owner.value}") from exc
