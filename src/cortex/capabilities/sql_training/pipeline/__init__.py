from __future__ import annotations

from typing import Any

__all__ = [
    "CompilePipeline",
    "DimensionValuePipeline",
    "ScanCatalogPipeline",
    "PrepareQASQLPipeline",
    "TrainPipeline",
]


def __getattr__(name: str) -> Any:
    if name == "CompilePipeline":
        from cortex.capabilities.sql_training.pipeline.compile import CompilePipeline

        return CompilePipeline
    if name == "DimensionValuePipeline":
        from cortex.capabilities.sql_training.pipeline.values import DimensionValuePipeline

        return DimensionValuePipeline
    if name == "ScanCatalogPipeline":
        from cortex.capabilities.sql_training.pipeline.init import ScanCatalogPipeline

        return ScanCatalogPipeline
    if name == "PrepareQASQLPipeline":
        from cortex.capabilities.sql_training.pipeline.prepare import PrepareQASQLPipeline

        return PrepareQASQLPipeline
    if name == "TrainPipeline":
        from cortex.capabilities.sql_training.pipeline.train import TrainPipeline

        return TrainPipeline
    raise AttributeError(
        f"module 'cortex.capabilities.sql_training.pipeline' has no attribute {name!r}"
    )
