"""Deterministic completeness checks; semantic judgments belong to the verifier."""

from __future__ import annotations


def evidence_complete(representations, evidence_by_ref):
    for member in representations:
        items = evidence_by_ref.get(member.ref, ())
        if not member.sources or {item.source for item in items} != set(member.sources):
            return False
        if any(not item.content.get("question") or not item.content.get("sql") for item in items):
            return False
    return True


def sources_covered(review, representations, evidence_by_ref):
    expected = {
        (member.ref, item.id)
        for member in representations
        for item in evidence_by_ref.get(member.ref, ())
    }
    actual = [(item.supporting_ref, item.evidence_id) for item in review.source_verdicts]
    return (
        bool(expected)
        and len(actual) == len(set(actual))
        and set(actual) == expected
        and all(
            item.verdict == "covered" and item.condition_verdict == "covered"
            for item in review.source_verdicts
        )
    )


def feedback_kind(review, representations=(), evidence_by_ref=None):
    if evidence_by_ref is not None:
        expected = {
            (member.ref, ev.id)
            for member in representations
            for ev in evidence_by_ref.get(member.ref, ())
        }
        actual = [(item.supporting_ref, item.evidence_id) for item in review.source_verdicts]
        if set(actual) != expected or len(actual) != len(set(actual)):
            return "contract_error"
        refs = [item.supporting_ref for item in review.verdicts]
        expected_plans = {
            (member.ref, str(plan.get("id", "")))
            for member in representations
            for plan in member.content.properties.get("sql_generation_plans", [])
            if isinstance(plan, dict)
        }
        plans = [(item.supporting_ref, item.source_plan_id) for item in review.plan_verdicts]
        if (
            set(refs) != {member.ref for member in representations}
            or len(refs) != len(set(refs))
            or set(plans) != expected_plans
            or len(plans) != len(set(plans))
        ):
            return "contract_error"
    # Classification is a request to re-examine analysis, never proof of conflict.
    kinds = {item.feedback_kind for item in review.source_verdicts if item.feedback_kind}
    for kind in (
        "unsupported_condition",
        "dependency_misinterpretation",
        "unresolved_competition",
        "insufficient_evidence",
        "contract_error",
        "model_failure",
    ):
        if kind in kinds:
            return kind
    if any(item.condition_verdict == "not_covered" for item in review.source_verdicts):
        return "unsupported_condition"
    verdicts = (*review.verdicts, *review.source_verdicts, *review.plan_verdicts)
    if any(item.verdict == "unknown" for item in verdicts) or any(
        item.condition_verdict == "unknown" for item in review.source_verdicts
    ):
        return "insufficient_evidence"
    return "candidate_defect"
