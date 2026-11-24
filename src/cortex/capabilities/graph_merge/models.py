"""Shared Graph Merge contracts and mutable task descriptors."""

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RelationFollowup(Model):
    edge_ids: tuple[str, ...] = Field(
        min_length=1,
        description="Stable original edge IDs displayed in the fixed group input.",
    )
    problem: str = Field(min_length=1)
    required_outcome: str = Field(min_length=1)

    @field_validator("edge_ids")
    @classmethod
    def unique_edges(cls, values):
        if any(not value.strip() for value in values) or len(set(values)) != len(
            values
        ):
            raise ValueError("Followup edge IDs must be nonempty and distinct")
        return values

    @field_validator("problem", "required_outcome")
    @classmethod
    def meaningful_text(cls, value):
        if not value.strip():
            raise ValueError("Relation followup text must be nonempty")
        return value


@dataclass
class LocalTask:
    id: str
    phase: str
    targets: set[str]
    checks: list[dict] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)
    feedback: list[dict | str] = field(default_factory=list)
