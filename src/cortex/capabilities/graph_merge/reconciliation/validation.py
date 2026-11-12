"""Structural guards; semantic coverage belongs to the Assessment agent."""

from __future__ import annotations

from cortex.capabilities.graph_merge.identity import exact_identity_key
from cortex.capabilities.graph_merge.reconciliation.evidence import evidence_complete
from cortex.schema import GraphNode, GraphSpace, IdentityMode, validate_graph_space
from cortex.schema.references import property_reference_ids
from cortex.schema.semantic_validation import validate_semantic_properties


def validate_proposal(proposal, group, schema, dependencies, evidence_by_ref):
    expected = {m.ref for m in group.members}
    assigned = [ref for c in proposal.candidates for ref in c.supporting_refs]
    assigned.extend(proposal.unresolved_refs)
    if set(assigned) != expected or len(assigned) != len(set(assigned)):
        raise ValueError(
            "Assign every member exactly once to a candidate or unresolved_refs"
        )
    if proposal.identity_mismatch and (
        len(group.members) < 2
        or proposal.candidates
        or proposal.difference_points
        or set(proposal.unresolved_refs) != expected
    ):
        raise ValueError(
            "Identity correction must preserve every original without candidates or conflict points"
        )
    by_ref = {m.ref: m for m in group.members}
    evidence_ids = {e.id for items in evidence_by_ref.values() for e in items}
    for explanation in proposal.analysis.differences:
        if not set(explanation.supporting_refs) <= expected:
            raise ValueError("Analysis refers to an unknown member")
        if not set(explanation.evidence_ids) <= evidence_ids:
            raise ValueError("Analysis refers to unknown evidence")
    node_type = schema.node_type_index()[group.node_type]
    known_ids = {n.id for n in dependencies} - {m.ref.ref_id for m in group.members}
    known_ids.discard(group.existing_node_id)
    for i, candidate in enumerate(proposal.candidates):
        members = tuple(by_ref[ref] for ref in candidate.supporting_refs)
        if not evidence_complete(members, evidence_by_ref):
            raise ValueError(
                "Candidate members lack question/SQL evidence; preserve their originals"
            )
        node = GraphNode(
            id=f"candidate.{i}",
            node_type=group.node_type,
            **candidate.content.model_dump(mode="python"),
        )
        validate_graph_space(GraphSpace(nodes=(node,)), schema)
        refs = candidate.content.properties.get("references", [])
        if not isinstance(refs, list) or any(
            not isinstance(r, str) or r not in known_ids for r in refs
        ):
            raise ValueError(
                "Candidate references must resolve outside its own identity group"
            )
        validate_semantic_properties(group.node_type, candidate.content.properties)
        actual_refs = property_reference_ids(candidate.content.properties)
        if not actual_refs <= known_ids:
            raise ValueError(
                "Candidate nested references must resolve outside its own identity group"
            )
        allowed_types = {
            "metric": {"concept", "metric", "table", "column", "dimension_value"},
            "generation_rule": {
                "event",
                "metric",
                "concept",
                "table",
                "column",
                "dimension_value",
            },
            "concept": {"table", "column", "dimension_value"},
            "event": {"table", "column", "dimension_value"},
        }.get(group.node_type)
        if allowed_types and any(
            not any(
                schema.is_node_type_compatible(n.node_type, t) for t in allowed_types
            )
            for n in dependencies
            if n.id in actual_refs
        ):
            raise ValueError(
                "Candidate dependency type is incompatible with its node type"
            )
        if node_type.identity_policy.mode == IdentityMode.EXACT and any(
            exact_identity_key(candidate.content, node_type.identity_policy)
            != exact_identity_key(m.content, node_type.identity_policy)
            for m in members
        ):
            raise ValueError("Candidate changed exact identity")
    validate_conflict_points(proposal, group)


def validate_conflict_points(proposal, group):
    resolved = {ref for c in proposal.candidates for ref in c.supporting_refs}
    points = proposal.difference_points
    ids = [p.point_id for p in points]
    if len(ids) != len(set(ids)):
        raise ValueError("Conflict point IDs must be unique")
    if bool(points) != (len(proposal.candidates) > 1):
        raise ValueError(
            "Multiple complete candidates require unresolved conflict points"
        )
    source_ids = {
        m.ref: {s.case_id for s in m.sources if s.case_id is not None}
        for m in group.members
    }
    signatures = {ref: [] for ref in resolved}
    for point in points:
        if point.resolution != "conflict" or len(point.positions) < 2:
            raise ValueError("Publish only conflicts with at least two positions")
        refs = [m.representation_ref for p in point.positions for m in p.members]
        if set(refs) != resolved or len(refs) != len(set(refs)):
            raise ValueError(
                "Conflict positions must partition resolved members exactly once"
            )
        for i, position in enumerate(point.positions):
            for member in position.members:
                if set(member.source_case_ids) != source_ids[member.representation_ref]:
                    raise ValueError(
                        "Conflict source IDs differ from supplied provenance"
                    )
                signatures[member.representation_ref].append(i)
    seen = set()
    for candidate in proposal.candidates:
        choices = {tuple(signatures[ref]) for ref in candidate.supporting_refs}
        if len(choices) != 1 or (points and next(iter(choices)) in seen):
            raise ValueError(
                "Candidate grouping disagrees with complete conflict positions"
            )
        seen.update(choices)


def source_checklist(proposal, evidence_by_ref):
    if proposal.identity_mismatch:
        return {
            (ref, evidence.id)
            for ref in proposal.unresolved_refs
            for evidence in evidence_by_ref.get(ref, ())
        }
    return {
        (ref, evidence.id)
        for c in proposal.candidates
        for ref in c.supporting_refs
        for evidence in evidence_by_ref.get(ref, ())
    }


def validate_review(review, proposal, evidence_by_ref):
    expected = source_checklist(proposal, evidence_by_ref)
    actual = [
        (row.supporting_ref, row.evidence_id) for row in review.coverage_by_source
    ]
    if set(actual) != expected or len(actual) != len(set(actual)):
        raise ValueError(
            "Coverage must include each candidate member/source pair exactly once"
        )


def review_passes(review, proposal, evidence_by_ref):
    validate_review(review, proposal, evidence_by_ref)
    return (
        review.generalization.verdict == "acceptable"
        and all(row.verdict == "covered" for row in review.coverage_by_source)
        and (not proposal.candidates or bool(review.coverage_by_source))
    )
