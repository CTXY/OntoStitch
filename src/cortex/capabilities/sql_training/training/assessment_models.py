"""Compact assessment conclusions and deterministic acceptance policy."""

from typing import Literal

from pydantic import Field, model_validator

from cortex.capabilities.sql_training.models.base import NodeModel


class LearningFeedback(NodeModel):
    kind: Literal["quality", "retrieval", "selection", "application", "target", "regression"]
    location: str = Field(description="Exact supplied node or relation ID, not a field path. Put field details in gap.")
    gap: str = Field(min_length=1)
    required_change: str = Field(min_length=1, description="Required semantic correction; the Learning Agent chooses graph edits.")
    preserve: str = Field(min_length=1)


def quality_from_checks(checks):
    outcomes = set(checks.values())
    return "invalid" if "invalid" in outcomes else "inconclusive" if "inconclusive" in outcomes else "valid"


class UnitResult(NodeModel):
    unit_id: str
    quality: Literal["valid", "invalid", "inconclusive"]
    quality_checks: dict[str, Literal["valid", "invalid", "inconclusive"]] = Field(
        default_factory=dict,
        description="Return every key in this unit's quality_requirements exactly once. Overall quality is invalid if any check is invalid, otherwise inconclusive if any check is inconclusive, otherwise valid.",
    )
    application: Literal["applied", "partially_applied", "not_applied", "not_attributed", "not_applicable", "inconclusive"] = Field(
        description="Follow the unit's application_requirement, not its node/edge kind. sql forbids not_applicable. applied requires complete local SQL behavior AND inclusion in every attempt; partial/not_applied/not_attributed/inconclusive require retrieval, selection or application feedback. semantic/none may use not_applicable when no SQL behavior is required."
    )
    feedback: list[LearningFeedback] = Field(default_factory=list, description="For unsuccessful quality include quality feedback; for unsuccessful application include retrieval/selection/application feedback, even if quality or target feedback also exists. location is an exact supplied node/edge ID.")

    @model_validator(mode="before")
    @classmethod
    def derive_missing_summary(cls, value):
        # Old checkpoints retain their explicit summary and consistency checks.
        # New Agent output supplies only the authoritative individual judgments.
        if isinstance(value, dict) and "quality" not in value:
            checks = value.get("quality_checks", {})
            if isinstance(checks, dict):
                return {**value, "quality": quality_from_checks(checks)}
        return value


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


def application_options(unit, attempts):
    """Mechanically legal labels only; do not decide whether SQL implements a unit."""
    options = ["applied", "partially_applied", "not_applied", "not_attributed", "not_applicable", "inconclusive"]
    included = [context_included(fact) for fact in unit["context_facts"]]
    requirement = unit.get("application_requirement", "none" if unit["change_kind"] == "delete" else "sql")
    if requirement == "sql":
        options.remove("not_applicable")
    if not attempts or not all(included):
        options.remove("applied")
    if not any(included):
        options.remove("partially_applied")
    if included and all(included):
        options.remove("not_attributed")
    return options


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
        errors = []

        def invalid(path, message):
            errors.append(f"{path}: {message}")

        def validate_feedback(rows, owner):
            for index, feedback in enumerate(rows):
                path = f"{owner}.feedback[{index}]"
                if feedback.location not in locations:
                    invalid(f"{path}.location", f"{feedback.location!r} is not a node/relation ID. "
                        "A target_id belongs in target_results.target_id, not feedback.location. "
                        "Use an affected node/relation ID from the supplied graph."
                    )
                missing = [key for key in ("gap", "required_change", "preserve")
                           if not getattr(feedback, key).strip()]
                if missing:
                    invalid(path, f"requires nonempty fields: {missing}")

        for result in self.unit_results:
            unit = units[result.unit_id]
            path = f"unit_results[{result.unit_id}]"
            options = application_options(unit, attempts)
            requirements = unit.get("quality_requirements")
            if requirements is not None:
                if set(result.quality_checks) != set(requirements):
                    invalid(path + ".quality_checks", f"must cover exactly {requirements}")
                expected = quality_from_checks(result.quality_checks)
                if result.quality != expected:
                    invalid(path + ".quality", f"overall quality must agree with every required quality check: expected {expected!r}, received {result.quality!r}. Omit this derived field from Agent output; keep the actual check judgments and all defect feedback.")
            candidate = unit.get("candidate") or {}
            if (
                "predicate_precision" in result.quality_checks
                and candidate.get("type") not in relation_contracts
                and result.quality_checks["predicate_precision"] == "valid"
            ):
                invalid(path + ".quality_checks.predicate_precision", "A relation without a supplied contract cannot have valid predicate_precision")
            facts = unit["context_facts"]
            covered = [ident for fact in facts for ident in fact["attempt_ids"]]
            if any(not f["attempt_ids"] for f in facts) or len(covered) != len(set(covered)) or set(covered) != attempts:
                invalid(path, "context facts must cover every validation attempt")
            included = [context_included(f) for f in facts]
            requirement = unit.get(
                "application_requirement",
                "none" if unit["change_kind"] == "delete" else "sql",
            )
            if requirement not in {"sql", "semantic", "none"}:
                invalid(path, "unknown application requirement")
            if requirement == "none" and unit["change_kind"] != "delete":
                invalid(path, "only deleted units have no current application contract")
            if result.application == "not_applicable" and requirement == "sql":
                invalid(path + ".application", "SQL application is required for this unit (application_requirement=sql); not_applicable is forbidden. Judge actual local SQL behavior and context inclusion; use applied, partially_applied, not_applied, not_attributed or inconclusive. An unsuccessful state requires application/retrieval/selection feedback.")
            if result.application == "applied" and (not attempts or not all(included)):
                invalid(path + ".application", "application attribution requires context inclusion in every attempt")
            if result.application == "partially_applied" and not any(included):
                invalid(path + ".application", "partial application attribution requires context inclusion")
            if result.application == "not_attributed" and included and all(included):
                invalid(path + ".application", "recorded context inclusion cannot be classified as not_attributed")
            if result.application not in options:
                invalid(path + ".application", f"allowed labels for the recorded requirement/inclusion are {options}; choose by actual SQL behavior, not by a desire to accept the unit")
            kinds = {f.kind for f in result.feedback}
            if result.quality != "valid" and "quality" not in kinds:
                invalid(path + ".feedback", "unsuccessful quality needs specific quality feedback")
            if result.application not in {"applied", "not_applicable"} and not kinds & {"retrieval", "selection", "application"}:
                invalid(path + ".feedback", f"unsuccessful application needs specific Learning feedback: application={result.application!r} requires kind retrieval, selection or application. Quality/target feedback does not replace it; explain the observed gap, required correction and behavior to preserve using an affected node/edge ID.")
            validate_feedback(result.feedback, f"unit_results[{result.unit_id}]")
        for target in self.target_results:
            if target.status == "resolved" and not attempts:
                invalid(f"target_results[{target.target_id}].status", "target resolution requires validation attempts")
            if target.status != "resolved" and not any(f.kind == "target" for f in target.feedback):
                invalid(f"target_results[{target.target_id}].feedback", "an unresolved target needs specific target feedback")
            validate_feedback(target.feedback, f"target_results[{target.target_id}]")
        refs = {p["evidence_ref"] for p in payload["prior_evidence"]}
        for regression in self.regressions:
            if regression.prior_evidence_ref not in refs or not set(regression.attributable_unit_ids) <= units.keys():
                invalid(f"regressions[{regression.prior_evidence_ref}]", "regression references unknown prior evidence or units")
            if regression.attribution == "supported" and not regression.attributable_unit_ids:
                invalid(f"regressions[{regression.prior_evidence_ref}]", "supported regression attribution requires a unit")
            if any(f.kind != "regression" for f in regression.feedback):
                invalid(f"regressions[{regression.prior_evidence_ref}].feedback", "a regression needs specific regression feedback")
            validate_feedback(regression.feedback, f"regressions[{regression.prior_evidence_ref}]")
        if any(len(row) < 2 or len(set(row)) != len(row) or not set(row) <= units.keys() for row in self.coupled_unit_ids):
            invalid("coupled_unit_ids", "must name distinct known units")
        if errors:
            raise ValueError("Assessment contract violations:\n- " + "\n- ".join(errors))

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
