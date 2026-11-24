"""Shared immutable model configuration for current Graph Merge contracts."""
from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
