from __future__ import annotations

from cortex.capabilities.graph_merge.models import (
    NodeRepresentation,
    ReconcileOutcome,
    ReconciliationGroup,
    SharedSemantic,
)


def complete_single_shared_group(
    outcome: ReconcileOutcome,
    members: tuple[NodeRepresentation, ...],
) -> ReconcileOutcome:
    """Copy all refs into a sole group only when its own shared claim covers them."""

    if len(outcome.groups) != 1 or any(
        point.resolution == "conflict" for point in outcome.difference_points
    ):
        return outcome
    group = outcome.groups[0]
    expected = tuple(member.ref for member in members)
    expected_set = set(expected)
    if (
        group.baseline_ref not in expected_set
        or not set(group.supporting_refs) <= expected_set
        or len(group.supporting_refs) == len(expected)
        or not any(set(shared.supporting_refs) == expected_set for shared in group.shared)
    ):
        return outcome
    return outcome.model_copy(
        update={
            "groups": (
                group.model_copy(update={"supporting_refs": expected}),
            )
        }
    )


def compile_single_conflict_point(
    outcome: ReconcileOutcome,
    members: tuple[NodeRepresentation, ...],
) -> ReconcileOutcome:
    """Map an explicit LLM conflict partition to groups without judging semantics."""

    if len(outcome.groups) != 1 or len(outcome.difference_points) != 1:
        return outcome
    original = outcome.groups[0]
    point = outcome.difference_points[0]
    if point.resolution != "conflict":
        return outcome
    expected = {member.ref for member in members}
    refs = [
        member.representation_ref
        for position in point.positions
        for member in position.members
    ]
    if len(refs) != len(expected) or set(refs) != expected or len(point.positions) < 2:
        return outcome
    position_sets = [
        {member.representation_ref for member in position.members}
        for position in point.positions
    ]
    if any(
        residual.applies_when is not None
        or not any(set(residual.supporting_refs) <= refs for refs in position_sets)
        for residual in original.residuals
    ) or any(
        relation.relation != "conflict" for relation in original.residual_relations
    ):
        return outcome
    groups = []
    for position in point.positions:
        position_refs = tuple(member.representation_ref for member in position.members)
        groups.append(
            ReconciliationGroup(
                supporting_refs=position_refs,
                baseline_ref=position_refs[0],
                relation="equivalent",
                reason_code="conflict_position",
                reason=f"This group preserves one unresolved choice: {position.statement}",
                shared=(
                    SharedSemantic(
                        semantic_point=point.aspect,
                        statement=position.statement,
                        supporting_refs=position_refs,
                    ),
                ),
            )
        )
    return outcome.model_copy(update={"groups": tuple(groups)})
