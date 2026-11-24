"""Small semantic proposals; graph edits retain GraphManager's operation vocabulary."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from pydantic.json_schema import SkipJsonSchema

from cortex.schema import NodeContent, SourceReference
from cortex.schema.models import EdgeType


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IdentityGroup(Model):
    members: tuple[str, ...] = Field(min_length=2)
    meaning: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Distinction(Model):
    members: tuple[str, str]
    reason: str = Field(min_length=1)


class IdentityDecision(Model):
    groups: tuple[IdentityGroup, ...] = ()
    distinctions: tuple[Distinction, ...] = ()


class Element(Model):
    kind: Literal["node", "edge"]
    id: str
    alternative_id: str | None = None


class Change(Model):
    before: tuple[Element, ...] = Field(min_length=1)
    after: tuple[Element, ...] = ()
    explanation: str = Field(min_length=1)


class GraphEdit(Model):
    operation: Literal[
        "create_node",
        "update_node",
        "delete_node",
        "set_conflict",
        "create_edge",
        "update_edge",
        "delete_edge",
    ]
    node: dict[str, Any] | None = None
    edge: dict[str, Any] | None = None
    node_id: str | None = None
    edge_id: str | None = None

    @model_validator(mode="after")
    def require_operation_payload(self):
        field = (
            "node_id"
            if self.operation == "delete_node"
            else "edge_id"
            if self.operation == "delete_edge"
            else "node"
            if self.operation in {"create_node", "update_node", "set_conflict"}
            else "edge"
        )
        if getattr(self, field) is None:
            raise ValueError(f"{self.operation} requires {field}")
        if any(
            getattr(self, key) is not None
            for key in ("node", "edge", "node_id", "edge_id")
            if key != field
        ):
            raise ValueError(f"{self.operation} accepts only operation and {field}")
        return self


class IntegrationProposal(Model):
    edits: tuple[GraphEdit, ...] = ()
    relation_contracts: tuple[EdgeType, ...] = ()
    changes: tuple[Change, ...] = ()
    # A concrete identity counterexample sends control back to the identity step.
    identity_feedback: str | None = None
    representation_error: str | None = None


class ReviewReference(Model):
    fields: tuple[str, ...] = ()
    kind: Literal["node", "edge", "contract"]
    id: str


class RationaleCheck(Model):
    node_id: str
    supported: bool
    unsupported_claims: tuple[str, ...] = ()

    @model_validator(mode="after")
    def no_unsupported_claims_on_pass(self):
        if self.supported and self.unsupported_claims:
            raise ValueError(
                "A supported rationale cannot contain unsupported claims; repair the reason or mark unsupported"
            )
        return self

    explanation: str = Field(
        min_length=1,
        description="One sentence comparing the Rule reason's claim with the actual supplied evidence; a correct action alone is insufficient.",
    )


class IntegrationReview(Model):
    verdict: Literal["accept", "revise", "revisit_identity"]
    decision_check: str = Field(
        min_length=1,
        description="Identity and joint compatibility: compare shared decisions in related Rules, overlapping conditions and required behaviors, including compound actions kept as distinct identities. Individual Gold support is not mutual consistency.",
    )
    quality_check: str = Field(
        min_length=1,
        description="Three short findings: node content AND justification; individual relation roles/limits; consistency of the active predicate vocabulary. Include retained input knowledge.",
    )
    preservation_check: str = Field(min_length=1)
    rationale_checks: tuple[RationaleCheck, ...] = ()
    issues: tuple[str, ...] = ()
    issue_refs: tuple[ReviewReference, ...] = Field(
        default=(),
        description="Objects involved in unresolved issues; use supplied node/edge/contract IDs. Empty when accepted.",
    )

    @model_validator(mode="after")
    def consistent_verdict(self):
        if self.verdict == "accept" and (
            self.issues
            or self.issue_refs
            or any(not c.supported for c in self.rationale_checks)
        ):
            raise ValueError("An accepted review cannot contain unresolved issues")
        return self


def review_output_model(required_ids):
    """Check review coverage, not whether the model's semantic judgment is true."""
    expected = set(required_ids)

    @model_validator(mode="after")
    def rationale_coverage(self):
        found = [c.node_id for c in self.rationale_checks]
        if set(found) != expected or len(found) != len(set(found)):
            raise ValueError(
                f"Provide exactly one rationale_check for each Rule ID: {sorted(expected)}"
            )
        return self

    return create_model(
        "IntegrationReview",
        __base__=IntegrationReview,
        __validators__={"rationale_coverage": rationale_coverage},
    )


class InspectionCheck(Model):
    inspection_id: str
    conclusion: Literal["keep", "changed", "failed"]
    reason: str = Field(min_length=1)
    decision_relation: (
        Literal["compatible", "conditional", "conflict", "different_decision"] | None
    ) = None
    result_ids: tuple[str, ...] = ()


class StageCompletion(Model):
    verdict: Literal["complete", "revisit_identity", "failed"]
    explanation: str = Field(min_length=1)
    inspection_checks: tuple[InspectionCheck, ...] = ()
    rationale_checks: tuple[RationaleCheck, ...] = ()


# Tool-boundary shapes expose only fields valid for each operation. The compiler
# retains GraphEdit as its common internal representation.
class NodeWrite(Model):
    operation: Literal["create_node", "update_node", "set_conflict"]
    node: dict[str, Any]


class EdgeWrite(Model):
    operation: Literal["create_edge", "update_edge"]
    edge: dict[str, Any]


class NodeDelete(Model):
    operation: Literal["delete_node"]
    node_id: str


class EdgeDelete(Model):
    operation: Literal["delete_edge"]
    edge_id: str


SubmissionEdit = NodeWrite | EdgeWrite | NodeDelete | EdgeDelete


class CompleteAlternative(Model):
    alternative_id: str
    content: NodeContent
    sources: SkipJsonSchema[tuple[SourceReference, ...]] = ()
    evidence_ids: tuple[str, ...] = Field(default=(), description="Existing alternative IDs retain exact sources when omitted. For new alternatives or additional support cite displayed evidence IDs, never reconstruct SourceReference objects.")
