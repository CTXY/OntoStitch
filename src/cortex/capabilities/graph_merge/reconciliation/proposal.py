"""Whole-group proposal checks and bounded generalization/review loop."""

from __future__ import annotations

from cortex.capabilities.graph_merge.models import (
    ConsolidationProposal,
    GeneralizationVerification,
    ReconcileOutcome,
    ReconciliationGroup,
)
from cortex.capabilities.graph_merge.reconciliation.consolidation import (
    active_memory,
    fingerprint,
    invoke,
)
from cortex.capabilities.graph_merge.reconciliation.coverage import (
    evidence_complete,
    feedback_kind,
    sources_covered,
)
from cortex.capabilities.graph_merge.reconciliation.generalization import (
    _generation_rule_misstates_rank_semantics,
    _plans_covered,
)
from cortex.infrastructure.llm.diagnostics import exception_details
from cortex.schema import GraphNode, GraphSpace, IdentityMode, validate_graph_space


def preserved_proposal(group, reason):
    return ConsolidationProposal(
        candidates=(), unresolved_refs=tuple(m.ref for m in group.members), reason=reason
    )


def validate_proposal(proposal, group, schema, dependency_nodes):
    # Reuse the published conflict-position invariants, not the old shared/residual contract.
    from cortex.capabilities.graph_merge.identity import exact_identity_key
    from cortex.capabilities.graph_merge.reconciliation.service import (
        _reusable_selector,
        _valid_difference_points,
    )

    expected = {member.ref for member in group.members}
    assigned = [ref for c in proposal.candidates for ref in c.supporting_refs]
    assigned.extend(proposal.unresolved_refs)
    if len(assigned) != len(set(assigned)) or set(assigned) != expected:
        raise ValueError("Assign each input ref exactly once to a candidate or unresolved_refs")
    groups = tuple(
        ReconciliationGroup(
            supporting_refs=c.supporting_refs,
            baseline_ref=c.supporting_refs[0],
            relation="equivalent",
            reason_code="joint_proposal",
            reason=proposal.reason,
        )
        for c in proposal.candidates
    ) + tuple(
        ReconciliationGroup(
            supporting_refs=(ref,),
            baseline_ref=ref,
            relation="equivalent",
            reason_code="preserve_unchanged",
            reason="Unresolved original",
        )
        for ref in proposal.unresolved_refs
    )
    outcome = ReconcileOutcome(
        groups=groups,
        difference_points=proposal.difference_points,
        unresolved_refs=proposal.unresolved_refs,
        reason=proposal.reason,
    )
    if any(p.resolution != "conflict" or len(p.positions) < 2 for p in proposal.difference_points):
        raise ValueError("Publish only unresolved competitive difference points with >=2 positions")
    if not _valid_difference_points(outcome, group.members):
        raise ValueError("Invalid conflict positions, source case IDs, or unexplained splitting")
    # Opposing positions may never collapse into the same complete candidate.
    for point in proposal.difference_points:
        positions = {
            member.representation_ref: i
            for i, position in enumerate(point.positions)
            for member in position.members
        }
        if any(len({positions[r] for r in c.supporting_refs}) > 1 for c in proposal.candidates):
            raise ValueError("A candidate combines opposing conflict positions")
    node_type = schema.node_type_index()[group.node_type]
    by_ref = {m.ref: m for m in group.members}
    known_ids = {n.id for n in dependency_nodes} | {m.ref.ref_id for m in group.members}
    provenance_ids = {m.ref.ref_id for m in group.members}
    for index, candidate in enumerate(proposal.candidates):
        selectors = [candidate.content.properties.get("condition")]
        selectors.extend(
            plan.get("application")
            for plan in candidate.content.properties.get("sql_generation_plans", [])
            if isinstance(plan, dict)
        )
        if any(isinstance(s, str) and not _reusable_selector(s, provenance_ids) for s in selectors):
            raise ValueError("Applicability must not select by case/source/representation IDs")
        node = GraphNode(
            id=f"candidate.{index}",
            node_type=group.node_type,
            **candidate.content.model_dump(mode="python"),
        )
        validate_graph_space(GraphSpace(nodes=(node,), edges=()), schema)
        references = candidate.content.properties.get("references", [])
        if not isinstance(references, list) or any(
            not isinstance(ref, str) or ref not in known_ids for ref in references
        ):
            raise ValueError("Candidate references must resolve in the supplied dependency context")
        if provenance_ids.intersection(references):
            raise ValueError(
                "Candidate may not reference its own identity group; inline its content"
            )
        if node_type.identity_policy.mode == IdentityMode.EXACT and any(
            exact_identity_key(candidate.content, node_type.identity_policy)
            != exact_identity_key(by_ref[r].content, node_type.identity_policy)
            for r in candidate.supporting_refs
        ):
            raise ValueError("Candidate changed exact identity")
        if _generation_rule_misstates_rank_semantics(candidate.content, node_type=node_type):
            raise ValueError("RANK skips rank numbers after ties; correct the explanation")


def review_checklists(proposal, group, evidence_by_ref):
    assigned = {r for c in proposal.candidates for r in c.supporting_refs}
    members = tuple(m for m in group.members if m.ref in assigned)
    return {
        "representation_checklist": [m.ref.model_dump(mode="json") for m in members],
        "preserved_refs": [r.model_dump(mode="json") for r in proposal.unresolved_refs],
        "source_checklist": [
            {"supporting_ref": m.ref.model_dump(mode="json"), "evidence_id": ev.id}
            for m in members
            for ev in evidence_by_ref.get(m.ref, ())
        ],
        "plan_checklist": [
            {
                "supporting_ref": m.ref.model_dump(mode="json"),
                "source_plan_id": str(p.get("id", "")),
            }
            for m in members
            for p in m.content.properties.get("sql_generation_plans", [])
            if isinstance(p, dict)
        ],
    }


def validate_review(review, proposal, group, evidence_by_ref):
    """Reject malformed ledgers separately from a valid negative semantic judgment."""
    assigned = {r for c in proposal.candidates for r in c.supporting_refs}
    members = tuple(m for m in group.members if m.ref in assigned)
    actual = [v.supporting_ref for v in review.verdicts]
    if len(actual) != len(set(actual)) or set(actual) != assigned:
        raise ValueError("Representation verdicts must cover candidate members exactly once")
    expected_sources = {(m.ref, ev.id) for m in members for ev in evidence_by_ref.get(m.ref, ())}
    actual_sources = [(v.supporting_ref, v.evidence_id) for v in review.source_verdicts]
    if len(actual_sources) != len(set(actual_sources)) or set(actual_sources) != expected_sources:
        raise ValueError("Source verdicts must cover the complete source checklist exactly once")
    expected_plans = {
        (m.ref, str(p.get("id", "")))
        for m in members
        for p in m.content.properties.get("sql_generation_plans", [])
        if isinstance(p, dict)
    }
    actual_plans = [(v.supporting_ref, v.source_plan_id) for v in review.plan_verdicts]
    if len(actual_plans) != len(set(actual_plans)) or set(actual_plans) != expected_plans:
        raise ValueError("Plan verdicts must cover the complete plan checklist exactly once")
    evidence_ids = {ev.id for items in evidence_by_ref.values() for ev in items}
    for verdict in (*review.verdicts, *review.plan_verdicts):
        if not set(verdict.counterevidence_ids) <= evidence_ids:
            raise ValueError("Counterevidence must refer to supplied whole-group evidence")
        if verdict.verdict == "not_covered" and not verdict.counterevidence_ids:
            raise ValueError("Negative behavior verdicts require supplied counterevidence IDs")


def review_passes(review, proposal, group, evidence_by_ref):
    if review.consistency.verdict != "acceptable":
        return False
    if not proposal.candidates:
        return True  # Original preservation, never a semantic generalization acceptance.
    if review.generalization.verdict != "acceptable" and not (
        group.node_type == "concept" and review.generalization.verdict == "not_applicable"
    ):
        return False
    by_ref = {m.ref: m for m in group.members}
    evidence_ids = {ev.id for items in evidence_by_ref.values() for ev in items}
    for candidate in proposal.candidates:
        members = tuple(by_ref[r] for r in candidate.supporting_refs)
        refs = set(candidate.supporting_refs)
        local = GeneralizationVerification(
            verdicts=tuple(v for v in review.verdicts if v.supporting_ref in refs),
            source_verdicts=tuple(v for v in review.source_verdicts if v.supporting_ref in refs),
            plan_verdicts=tuple(v for v in review.plan_verdicts if v.supporting_ref in refs),
            reason=review.reason,
        )
        if not (
            evidence_complete(members, evidence_by_ref)
            and all(v.verdict == "covered" for v in local.verdicts)
            and sources_covered(local, members, evidence_by_ref)
            and _plans_covered(local.plan_verdicts, members, candidate.content, evidence_ids)
        ):
            return False
    return True


def consolidate(
    group, *, schema, evidence_by_ref, dependency_nodes, dependency_conflicts, proposer, checker
):
    memory = active_memory.get()
    proposal = None
    needs_proposal = True
    previous_failure = None
    for round_index in range(1, memory.max_rounds + 1):
        memory.round = round_index
        if needs_proposal:
            feedback_start = len(memory.feedback)
            try:
                proposal = invoke(
                    "consolidate",
                    proposer.propose,
                    group=group,
                    schema=schema,
                    evidence_by_ref=evidence_by_ref,
                    dependency_nodes=dependency_nodes,
                    dependency_conflicts=dependency_conflicts,
                )
                memory.current_proposal = proposal.model_dump(mode="json")
                validate_proposal(proposal, group, schema, dependency_nodes)
                assigned = {r for c in proposal.candidates for r in c.supporting_refs}
                if not evidence_complete(
                    tuple(m for m in group.members if m.ref in assigned), evidence_by_ref
                ):
                    raise ValueError(
                        "Candidate members lack complete question/SQL evidence; preserve the "
                        "affected originals using unresolved_refs"
                    )
            except Exception as exc:
                if len(memory.feedback) == feedback_start and not memory.stop_reason:
                    memory.reject(
                        "contract_error", target="analysis", error_chain=exception_details(exc)
                    )
                if memory.stop_reason:
                    break
                continue
        record = {
            "round": round_index,
            "proposal": proposal.model_dump(mode="json"),
            "accepted": False,
            "review_status": "pending",
        }
        memory.history.append(record)
        feedback_start = len(memory.feedback)
        try:
            review = invoke(
                "consolidation_check",
                checker.check,
                proposal=proposal,
                group=group,
                schema=schema,
                evidence_by_ref=evidence_by_ref,
                dependency_nodes=dependency_nodes,
                dependency_conflicts=dependency_conflicts,
            )
            validate_review(review, proposal, group, evidence_by_ref)
        except Exception as exc:
            record["review_status"] = "model_or_contract_failure"
            if len(memory.feedback) == feedback_start and not memory.stop_reason:
                memory.reject(
                    "contract_error", target="checker", error_chain=exception_details(exc)
                )
            needs_proposal = False
            if memory.stop_reason:
                break
            continue
        memory.current_review = review.model_dump(mode="json")
        accepted = review_passes(review, proposal, group, evidence_by_ref)
        record.update(
            review=review.model_dump(mode="json"), accepted=accepted, review_status="complete"
        )
        if accepted:
            memory.stop_reason = (
                "accepted"
                if not proposal.unresolved_refs
                else "partial_preservation"
                if proposal.candidates
                else "preserved_unresolved"
            )
            return proposal
        kind = (
            "unnecessary_specialization"
            if review.generalization.verdict == "revise"
            else (feedback_kind(review) if proposal.candidates else "insufficient_evidence")
        )
        memory.reject(kind, target="analysis", review=review.model_dump(mode="json"))
        failure = fingerprint(
            {
                "proposal": proposal.model_dump(mode="json"),
                "review": review.model_dump(mode="json"),
            }
        )
        if failure == previous_failure:
            memory.stop_reason = "repeated_failed_proposal"
            break
        previous_failure = failure
        needs_proposal = True
    memory.stop_reason = memory.stop_reason or "round_budget_exhausted"
    return preserved_proposal(group, memory.stop_reason)
