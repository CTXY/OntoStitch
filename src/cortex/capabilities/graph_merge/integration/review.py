"""One review contract over an immutable candidate and explicit source views."""
from typing import Literal

from pydantic import Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from cortex.capabilities.graph_merge.integration.context import PHYSICAL
from cortex.capabilities.graph_merge.integration.models import Model
from cortex.schema.relation_contracts import effective_schema


class ReviewReference(Model):
    kind: Literal["node", "edge", "contract"]
    id: str
    view: Literal["input", "before", "candidate"] = "candidate"
    fields: tuple[str, ...] = ()


class AbsenceClaim(Model):
    """A checkable absence premise, never a command to create a relation."""

    kind: Literal["relation_absent", "concept_owner_absent"]
    edge_type: str = Field(min_length=1)
    to_node_id: str = Field(min_length=1)
    from_node_id: str | None = None
    alternative_id: str | None = None
    view: Literal["input", "before", "candidate"] = "candidate"

    @model_validator(mode="after")
    def precise_scope(self):
        if self.kind == "concept_owner_absent" and (
            self.edge_type != "grounded_on" or self.from_node_id or self.alternative_id
        ):
            raise ValueError("Owner absence checks all Concept owners of one physical target; use grounded_on without a source or branch filter")
        if self.alternative_id and not self.from_node_id:
            raise ValueError("A branch-specific relation claim needs its source node ID")
        return self


class ReviewIssue(Model):
    refs: tuple[ReviewReference, ...] = Field(min_length=1)
    problem: str = Field(min_length=1)
    basis: tuple[ReviewReference, ...] = ()
    required_outcome: str = Field(min_length=1)
    claim: AbsenceClaim | None = None


class Review(Model):
    verdict: Literal["accept", "revise", "needs_context", "revisit_identity"]
    issues: tuple[ReviewIssue, ...] = ()
    candidate_hash: SkipJsonSchema[str | None] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def coherent(self):
        if (self.verdict == "accept") != (not self.issues):
            raise ValueError("accept has no issues; other verdicts require concrete issues")
        if self.verdict in {"revise", "revisit_identity"} and any(not issue.basis for issue in self.issues):
            raise ValueError("A blocking judgment needs an input, before, or candidate basis for each issue")
        return self

    @property
    def issue_refs(self):
        """Controller-derived scope, not a second model-managed reference list."""
        return tuple(dict.fromkeys(ref for issue in self.issues for ref in issue.refs))


def graph_view(context, view, *, before=None, candidate=None):
    """Never substitute input data for a missing before/candidate snapshot."""
    if view == "input":
        return context.nodes, context.edges, context.union_state().graph
    if view == "before":
        state = before or getattr(context, "task_before", None)
        if state is None and getattr(context, "active_batch", None) is not None:
            state = context.active_batch.base
    elif view == "candidate":
        state = candidate or context.candidate
    else:
        raise ValueError("view must be input, before, or candidate")
    if state is None:
        raise ValueError(f"No {view} graph snapshot is available")
    return ({node.id: node for node in state.graph.nodes},
            {edge.id: edge for edge in state.graph.edges}, state.graph)


class ReviewFactError(ValueError):
    def __init__(self, message, *, counterexamples=()):
        super().__init__(message)
        self.counterexamples = tuple(counterexamples)


def absence_counterexamples(context, claim, *, before=None, candidate=None):
    """Search the full authoritative snapshot, independent of packet pagination."""
    nodes, edges, graph = graph_view(context, claim.view, before=before, candidate=candidate)
    if claim.edge_type not in effective_schema(context.schema, graph).edge_type_index():
        raise ReviewFactError(f"Unknown absence predicate: {claim.edge_type}")
    if claim.to_node_id not in nodes or (claim.from_node_id and claim.from_node_id not in nodes):
        raise ReviewFactError("Absence claims require exact existing endpoint IDs in the selected view")
    if claim.kind == "concept_owner_absent" and nodes[claim.to_node_id].node_type not in PHYSICAL:
        raise ReviewFactError("Owner absence requires a physical target ID")
    if claim.alternative_id and claim.alternative_id not in {
        a.alternative_id for a in nodes[claim.from_node_id].alternatives
    }:
        raise ReviewFactError("The selected branch does not belong to the source node")
    return tuple({"id": edge.id, "edge_type": edge.edge_type,
                  "from_node_id": edge.from_node_id, "to_node_id": edge.to_node_id,
                  **({"alternative_ids": edge.properties["alternative_ids"]}
                     if edge.properties.get("alternative_ids") else {})}
                 for edge in edges.values()
                 if edge.edge_type == claim.edge_type and edge.to_node_id == claim.to_node_id
                 and (not claim.from_node_id or edge.from_node_id == claim.from_node_id)
                 and (claim.kind != "concept_owner_absent" or nodes[edge.from_node_id].node_type == "concept")
                 and (not claim.alternative_id or not edge.properties.get("alternative_ids")
                      or claim.alternative_id in edge.properties["alternative_ids"]))


def validate_review(review, context, *, before=None, candidate=None):
    """Bind a review to its actual candidate and refute false mechanical premises."""
    from cortex.capabilities.graph_merge.integration.reconciliation_contract import AssessmentRecord
    if isinstance(review, AssessmentRecord):
        runtime = getattr(context, 'reconciliation_runtime', None)
        if runtime is None:
            raise ReviewFactError('Canonical assessment requires its bound local runtime')
        return runtime.validate_review(review)
    review = Review.model_validate(review)
    state = candidate or context.candidate
    if state is None:
        raise ReviewFactError("A review requires an actual candidate snapshot")
    for issue in review.issues:
        for ref in (*issue.refs, *issue.basis):
            nodes, edges, graph = graph_view(context, ref.view, before=before, candidate=state)
            objects = nodes if ref.kind == "node" else edges if ref.kind == "edge" else effective_schema(context.schema, graph).edge_type_index()
            if ref.id not in objects and review.verdict != "needs_context":
                raise ReviewFactError(f"Review {ref.kind} reference {ref.id!r} does not exist in {ref.view}")
            if any(field in {"sources", "common_sources", "reference_origin"} for field in ref.fields):
                raise ReviewFactError("Review semantic content and evidence IDs, not internal source dictionaries")
            if ref.id in objects:
                obj = objects[ref.id]
                raw = obj.model_dump(mode="json")
                for field in ref.fields:
                    value = raw
                    found = True
                    for part in field.split("."):
                        if not isinstance(value, dict) or part not in value:
                            found = False
                            break
                        value = value[part]
                    if found:
                        continue
                    pieces = field.split(".")
                    if ref.kind == "node":
                        declared = context.schema.effective_node_fields(obj.node_type)
                    elif ref.kind == "edge":
                        declared = effective_schema(context.schema, graph).edge_type_index()[obj.edge_type].fields
                    else:
                        declared = {}
                    if len(pieces) != 2 or pieces[0] != "properties" or pieces[1] not in declared:
                        raise ReviewFactError(f"Unknown review field {field!r} on {ref.kind} {ref.id!r}")
        if issue.claim:
            found = absence_counterexamples(context, issue.claim, before=before, candidate=state)
            if found:
                raise ReviewFactError("The asserted absence is contradicted by the selected graph; revise this issue, not the graph", counterexamples=found)
    from cortex.capabilities.graph_merge.integration.task_state import canonical_hash
    fingerprint = canonical_hash(state)
    return review.model_copy(update={"candidate_hash": fingerprint})
