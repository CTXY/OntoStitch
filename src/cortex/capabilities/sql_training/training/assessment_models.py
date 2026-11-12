"""Compact assessment conclusions and deterministic acceptance policy."""

from typing import Literal

from pydantic import Field

from cortex.capabilities.sql_training.models.base import NodeModel


class LearningFeedback(NodeModel):
    kind: Literal["quality", "retrieval", "selection", "application", "target", "regression"]
    location: str = Field(description="Exact supplied node or relation ID, not a field path. Put field details in gap.")
    gap: str = Field(min_length=1)
    required_change: str = Field(min_length=1, description="Required semantic correction; the Learning Agent chooses graph edits.")
    preserve: str = Field(min_length=1)


class UnitResult(NodeModel):
    unit_id: str
    quality: Literal["valid", "invalid", "inconclusive"]
    quality_checks: dict[str, Literal["valid", "invalid", "inconclusive"]] = Field(
        default_factory=dict,
        description="Return every key in this unit's quality_requirements exactly once. Overall quality is invalid if any check is invalid, otherwise inconclusive if any check is inconclusive, otherwise valid.",
    )
    application: Literal["applied", "partially_applied", "not_applied", "not_attributed", "not_applicable", "inconclusive"]
    feedback: list[LearningFeedback] = Field(default_factory=list)


class TargetResult(NodeModel):
    target_id: str
    status: Literal["resolved", "unresolved", "inconclusive"]
    feedback: list[LearningFeedback] = Field(default_factory=list)


class Regression(NodeModel):
    prior_evidence_ref: str
    attributable_unit_ids: list[str]
    attribution: Literal["supported", "inconclusive"]
    feedback: list[LearningFeedback] = Field(min_length=1)


def exact_ids(rows, expected, key, label):
    actual = [getattr(row, key) for row in rows]
    if set(actual) != set(expected) or len(actual) != len(set(actual)):
        raise ValueError(f"Assessment must cover every {label} exactly once")


def context_included(fact):
    if fact.get("present_in_context") is False:
        return False
    return bool(fact.get("present_in_context") is True or fact["selected"] or fact["included_via"])


class AssessmentFeedback(NodeModel):
    unit_results: list[UnitResult]
    target_results: list[TargetResult]
    regressions: list[Regression] = Field(default_factory=list)
    coupled_unit_ids: list[list[str]] = Field(default_factory=list)

    def validate_evidence(self, payload):
        units = {u["unit_id"]: u for u in payload["units"]}
        exact_ids(self.unit_results, units, "unit_id", "unit")
        attempts = {a["attempt"] for a in payload["attempts"]}
        targets = {t["target_id"] for t in payload["fixed_targets"]}
        exact_ids(self.target_results, targets, "target_id", "fixed target")
        definitions = [item for u in payload["units"] for item in (u.get("candidate"), u.get("accepted_before")) if item]
        definitions += payload["physical_context"] + payload["related_knowledge"]["nodes"] + payload["related_knowledge"]["edges"] + payload["related_knowledge"]["prior_edges"]
        if payload["round_baseline"]:
            definitions += payload["round_baseline"]["nodes"] + payload["round_baseline"]["edges"]
        locations = {item["id"] for item in definitions}
        relation_contracts = {item["type"] for item in payload["related_knowledge"].get("relation_definitions", [])}

        def validate_feedback(rows):
            if any(f.location not in locations or not all(v.strip() for v in (f.gap, f.required_change, f.preserve)) for f in rows):
                raise ValueError("Learning feedback requires an existing location, gap, required change and preserved behavior")

        for result in self.unit_results:
            unit = units[result.unit_id]
            requirements = unit.get("quality_requirements")
            if requirements is not None:
                if set(result.quality_checks) != set(requirements):
                    raise ValueError("quality_checks must cover exactly the unit's quality_requirements")
                outcomes = set(result.quality_checks.values())
                expected = "invalid" if "invalid" in outcomes else "inconclusive" if "inconclusive" in outcomes else "valid"
                if result.quality != expected:
                    raise ValueError("overall quality must agree with every required quality check")
            candidate = unit.get("candidate") or {}
            if (
                "predicate_precision" in result.quality_checks
                and candidate.get("type") not in relation_contracts
                and result.quality_checks["predicate_precision"] == "valid"
            ):
                raise ValueError("A relation without a supplied contract cannot have valid predicate_precision")
            facts = unit["context_facts"]
            covered = [ident for fact in facts for ident in fact["attempt_ids"]]
            if any(not f["attempt_ids"] for f in facts) or len(covered) != len(set(covered)) or set(covered) != attempts:
                raise ValueError("context facts must cover every validation attempt")
            included = [context_included(f) for f in facts]
            requirement = unit.get(
                "application_requirement",
                "none" if unit["change_kind"] == "delete" else "sql",
            )
            if requirement not in {"sql", "semantic", "none"}:
                raise ValueError("unknown application requirement")
            if requirement == "none" and unit["change_kind"] != "delete":
                raise ValueError("only deleted units have no current application contract")
            if result.application == "not_applicable" and requirement == "sql":
                raise ValueError("SQL application is required for this unit")
            if result.application == "applied" and (not attempts or not all(included)):
                raise ValueError("application attribution requires context inclusion in every attempt")
            if result.application == "partially_applied" and not any(included):
                raise ValueError("partial application attribution requires context inclusion")
            if result.application == "not_attributed" and included and all(included):
                raise ValueError("recorded context inclusion cannot be classified as not_attributed")
            kinds = {f.kind for f in result.feedback}
            if result.quality != "valid" and "quality" not in kinds:
                raise ValueError("unsuccessful quality needs specific quality feedback")
            if result.application not in {"applied", "not_applicable"} and not kinds & {"retrieval", "selection", "application"}:
                raise ValueError("unsuccessful application needs specific Learning feedback")
            validate_feedback(result.feedback)
        for target in self.target_results:
            if target.status == "resolved" and not attempts:
                raise ValueError("target resolution requires validation attempts")
            if target.status != "resolved" and not any(f.kind == "target" for f in target.feedback):
                raise ValueError("an unresolved target needs specific target feedback")
            validate_feedback(target.feedback)
        refs = {p["evidence_ref"] for p in payload["prior_evidence"]}
        for regression in self.regressions:
            if regression.prior_evidence_ref not in refs or not set(regression.attributable_unit_ids) <= units.keys():
                raise ValueError("regression references unknown prior evidence or units")
            if regression.attribution == "supported" and not regression.attributable_unit_ids:
                raise ValueError("supported regression attribution requires a unit")
            if any(f.kind != "regression" for f in regression.feedback):
                raise ValueError("a regression needs specific regression feedback")
            validate_feedback(regression.feedback)
        if any(len(row) < 2 or len(set(row)) != len(row) or not set(row) <= units.keys() for row in self.coupled_unit_ids):
            raise ValueError("coupled_unit_ids must name distinct known units")

    def accepted_members(self, groups):
        expected = {key for group in groups for key in group.member_ids}
        exact_ids(self.unit_results, expected, "unit_id", "unit")
        eligible = {
            r.unit_id for r in self.unit_results
            if r.quality == "valid" and r.application in {"applied", "not_applicable"}
            and all(value == "valid" for value in r.quality_checks.values())
            and not r.feedback
        }
        if any(r.attribution == "inconclusive" for r in self.regressions):
            eligible.clear()
        for regression in self.regressions:
            eligible.difference_update(regression.attributable_unit_ids)
        targets = {t.target_id: t for t in self.target_results}
        for group in groups:
            if any(t in targets and (targets[t].status != "resolved" or targets[t].feedback) for t in group.target_ids):
                eligible.difference_update(group.member_ids)
        # Group/coupling constraints only remove eligibility; they never manufacture it.
        constraints = [set(g.member_ids) for g in groups] + [set(row) for row in self.coupled_unit_ids]
        while True:
            before = set(eligible)
            for row in constraints:
                if not row <= eligible:
                    eligible.difference_update(row)
            if eligible == before:
                break
        return sorted(eligible)

    def acceptable(self, groups):
        return set(self.accepted_members(groups)) == {m for g in groups for m in g.member_ids} and all(
            t.status == "resolved" and not t.feedback for t in self.target_results
        ) and not self.regressions
