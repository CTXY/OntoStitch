"""Explicit experiment entry: empty semantic Initial, one existing repair cycle.

Production training defaults remain in TrainPipeline. No alternate prompts or
acceptance rules are defined here.
"""

from __future__ import annotations

from cortex.capabilities.sql_training.pipeline.train import TrainPipeline


class RepairOnlyTrainPipeline(TrainPipeline):
    skip_direct = True
    frozen_physical = True

    def run(self, **kwargs):
        for name in ("max_rounds", "max_iterations"):
            if kwargs.get(name, 1) != 1:
                raise ValueError(f"repair_only requires {name}=1")
            kwargs[name] = 1
        return super().run(**kwargs)
