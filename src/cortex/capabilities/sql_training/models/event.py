"""Business occurrence types anchored by explicit, evidenced business relations."""

from pydantic import Field, field_validator, model_validator

from cortex.capabilities.sql_training.core.ids import normalize_refs
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.schema.semantic_validation import validate_matching_targets


class EventProperties(NodeModel):
    targets: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Optional physical matching groups for an evidenced event type. Each group jointly "
            "identifies a match. Event creation still requires an evidenced action, participants "
            "and occurrence context connected by explicitly described business relations."
        ),
    )
    target_expressions: dict[str, str] = Field(
        default_factory=dict,
        description="Optional SQL matching expressions keyed by zero-based target group index.",
    )

    @field_validator("targets")
    @classmethod
    def normalize_targets(cls, groups: list[list[str]]) -> list[list[str]]:
        normalized = [normalize_refs(group) for group in groups]
        if any(not ref.startswith(("table.", "column.", "dimension_value."))
               for group in normalized for ref in group):
            raise ValueError("Matching targets must use physical Table, Column or governed Value IDs")
        return normalized

    @model_validator(mode="after")
    def validate_targets(self):
        validate_matching_targets(
            self.targets, self.target_expressions, required=False, node_type="Event"
        )
        return self
