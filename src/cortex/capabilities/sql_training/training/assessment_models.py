"""Compact assessment conclusions and deterministic acceptance policy."""

from typing import Literal

from pydantic import Field, model_validator

from cortex.capabilities.sql_training.models.base import NodeModel


class LearningFeedback(NodeModel):
    kind: Literal["quality", "sql_error", "target", "regression"]
    location: str = Field(description="Exact supplied node or relation ID, not a field path. Put field details in gap.")
    gap: str = Field(min_length=1)
    required_change: str = Field(min_length=1, description="Required semantic correction; the Learning Agent chooses graph edits.")
    preserve: str = Field(min_length=1)


class QualityFeedback(LearningFeedback):
    kind: Literal["quality"] = "quality"


class SqlErrorFeedback(LearningFeedback):
    kind: Literal["sql_error"] = "sql_error"
    attempt_ids: list[int] = Field(min_length=1)
    sql_excerpt: str = Field(min_length=1)


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
    feedback: list[QualityFeedback | SqlErrorFeedback] = Field(default_factory=list, description="Only concrete ontology quality defects or related SQL errors. A SQL error must identify the affected knowledge, cited attempt IDs, the SQL excerpt, the actual error and necessary correction. Do not report unused knowledge as a defect.")

    @model_validator(mode="before")
    @classmethod
    def derive_missing_summary(cls, value):
        # The model supplies individual judgments; code derives their summary.
        # Explicit summaries in saved v7 evidence must still agree with them.
        if isinstance(value, dict) and "quality" not in value:
            checks = value.get("quality_checks", {})
            if isinstance(checks, dict):
                return {**value, "quality": quality_from_checks(checks)}
        return value


class TargetFeedback(LearningFeedback):
    kind: Literal["target"] = "target"


class RegressionFeedback(LearningFeedback):
    kind: Literal["regression"] = "regression"


class TargetResult(NodeModel):
    target_id: str
    status: Literal["resolved", "unresolved", "inconclusive"]
    feedback: list[TargetFeedback] = Field(default_factory=list)


class Regression(NodeModel):
    prior_evidence_ref: str
    attributable_unit_ids: list[str]
    attribution: Literal["supported", "inconclusive"]
    feedback: list[RegressionFeedback] = Field(min_length=1)


def exact_ids(rows, expected, key, label):
    actual = [getattr(row, key) for row in rows]
    if set(actual) != set(expected) or len(actual) != len(set(actual)):
        raise ValueError(f"Assessment must cover every {label} exactly once")


def context_included(fact):
    if fact.get("present_in_context") is False:
        return False
    return bool(fact.get("present_in_context") is True or fact["selected"] or fact["included_via"])


class RetrievalResult(NodeModel):
    unit_id: str
    required: bool
    status: Literal["injected", "not_injected", "unknown", "removed"]
    injected_attempt_ids: list[int]
    missing_attempt_ids: list[int]
    unknown_attempt_ids: list[int]


def retrieval_result(unit, attempts):
    injected, missing, unknown = [], [], []
    for fact in unit["context_facts"]:
        target = (injected if context_included(fact) else
                  missing if fact.get("present_in_context") is False else unknown)
        target.extend(fact["attempt_ids"])
    removed = unit["change_kind"] == "delete"
    status = ("removed" if removed else "not_injected" if missing else
              "unknown" if unknown or not attempts else "injected")
    return RetrievalResult(unit_id=unit["unit_id"], required=bool(unit.get("retrieval_required")) and not removed,
                           status=status, injected_attempt_ids=sorted(injected),
                           missing_attempt_ids=sorted(missing), unknown_attempt_ids=sorted(unknown))


def feedback_locations(payload):
    definitions = [item for unit in payload['units']
                   for item in (unit.get('candidate'), unit.get('accepted_before')) if item]
    related = payload['related_knowledge']
    definitions += payload['physical_context'] + related['nodes'] + related['edges'] + related['prior_edges']
    if payload['round_baseline']:
        definitions += payload['round_baseline']['nodes'] + payload['round_baseline']['edges']
    return {item['id'] for item in definitions}


class AssessmentFeedback(NodeModel):
    unit_results: list[UnitResult]
    target_results: list[TargetResult]
    regressions: list[Regression] = Field(default_factory=list)
    coupled_unit_ids: list[list[str]] = Field(default_factory=list)
    retrieval_results: list[RetrievalResult] = Field(default_factory=list, description="Code-owned injection observations, never assessor-authored judgments.")

    def validate_evidence(self, payload):
        units = {u["unit_id"]: u for u in payload["units"]}
        exact_ids(self.unit_results, units, "unit_id", "unit")
        attempts = {a["attempt"] for a in payload["attempts"]}
        targets = {t["target_id"] for t in payload["fixed_targets"]}
        exact_ids(self.target_results, targets, "target_id", "fixed target")
        locations = feedback_locations(payload)
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
            requirements = unit.get("quality_requirements")
            if requirements is not None:
                if set(result.quality_checks) != set(requirements):
                    invalid(path + ".quality_checks", f"must cover exactly the unit's quality_requirements: {requirements}")
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
            kinds = {f.kind for f in result.feedback}
            if not kinds <= {"quality", "sql_error"}:
                invalid(path + ".feedback", "unit feedback is limited to quality defects and related SQL errors")
            if result.quality != "valid" and "quality" not in kinds:
                invalid(path + ".feedback", "unsuccessful quality needs specific quality feedback")
            for index, feedback in enumerate(result.feedback):
                if feedback.kind != "sql_error":
                    continue
                error_path = f"{path}.feedback[{index}]"
                if feedback.location != result.unit_id:
                    invalid(error_path + ".location", "attach the related SQL error to its affected reviewed unit")
                ids = feedback.attempt_ids
                if not ids or len(ids) != len(set(ids)) or not set(ids) <= attempts:
                    invalid(error_path + ".attempt_ids", "a related SQL error must cite distinct current attempt IDs")
                excerpt = feedback.sql_excerpt.strip()
                sql_by_id = {a["attempt"]: a.get("sql") or "" for a in payload["attempts"]}
                if not excerpt or any(excerpt not in sql_by_id.get(ident, "") for ident in ids):
                    invalid(error_path + ".sql_excerpt", "a related SQL error needs a verbatim excerpt present in each cited attempt")
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
        expected_retrieval = [retrieval_result(unit, attempts) for unit in payload["units"]]
        if self.retrieval_results and self.retrieval_results != expected_retrieval:
            raise ValueError("Assessment retrieval results disagree with recorded injection evidence")
        self.retrieval_results = expected_retrieval

    def accepted_members(self, groups):
        expected = {key for group in groups for key in group.member_ids}
        exact_ids(self.unit_results, expected, "unit_id", "unit")
        eligible = {
            r.unit_id for r in self.unit_results
            if r.quality == "valid"
            and all(value == "valid" for value in r.quality_checks.values())
            and not r.feedback
        }
        eligible.difference_update(r.unit_id for r in self.retrieval_results
                                   if r.required and r.status != "injected")
        for regression in self.regressions:
            if regression.attribution == "supported":
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
        ) and not any(r.attribution == "supported" for r in self.regressions)
