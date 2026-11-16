from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from cortex.capabilities.sql_training.models.base import NodeModel


class RepairType(StrEnum):
    SEMANTIC = "semantic"
    OPERATION = "operation"
    REPRESENTATION = "representation"
    COUPLED = "coupled"
    UNCERTAIN = "uncertain"


class RepairGroupOwner(StrEnum):
    SEMANTIC_MAPPING = "semantic_mapping"
    METRIC = "metric"
    GENERATION_RULE = "generation_rule"


class RepairNecessity(StrEnum):
    NECESSARY = "necessary"
    LATENT_RISK = "latent_risk"
    NEED_REVIEW = "need_review"
    UNCERTAIN = "uncertain"
    INVALID_ABLATION = "invalid_ablation"


class ErrorCandidate(NodeModel):
    id: str = Field(description="Stable within this report, such as E1 or E2.")
    error_key: str = Field(
        description=(
            "Short snake_case semantic key, stable across surface SQL rewrites when the "
            "same underlying error remains."
        )
    )
    difference_point: str = Field(description="Concise human-readable issue.")
    gold_logic: str = Field(
        description="Expected logic shown by the gold SQL or gold result."
    )
    predicted_logic: str = Field(
        description="Incorrect logic shown by the predicted SQL or predicted result."
    )
    repair_type: RepairType = RepairType.UNCERTAIN
    necessity: RepairNecessity = RepairNecessity.NEED_REVIEW

    @field_validator(
        "id",
        "error_key",
        "difference_point",
        "gold_logic",
        "predicted_logic",
        mode="before",
    )
    @classmethod
    def none_to_empty_text(cls, value: Any) -> Any:
        if value is None:
            return ""
        return value

    @model_validator(mode="after")
    def validate_required_text(self) -> ErrorCandidate:
        missing = [
            name
            for name, value in {
                "id": self.id,
                "error_key": self.error_key,
                "difference_point": self.difference_point,
                "gold_logic": self.gold_logic,
                "predicted_logic": self.predicted_logic,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise ValueError(f"error candidate missing required fields: {', '.join(missing)}")
        return self


class RepairAction(NodeModel):
    action: Literal["repair", "finish"]
    repair_type: RepairType = RepairType.UNCERTAIN
    edit_key: str = ""
    edit_summary: str = ""
    changed_clause: str = ""
    predicted_logic_before: str = ""
    gold_logic_after: str = ""
    repaired_sql: str = ""
    rationale: str = ""
    finish_reason: str = ""

    @field_validator(
        "edit_key",
        "edit_summary",
        "changed_clause",
        "predicted_logic_before",
        "gold_logic_after",
        "repaired_sql",
        "rationale",
        "finish_reason",
        mode="before",
    )
    @classmethod
    def none_to_empty_text(cls, value: Any) -> Any:
        if value is None:
            return ""
        return value

    @model_validator(mode="after")
    def validate_action_payload(self) -> RepairAction:
        if self.action == "finish":
            if not self.finish_reason.strip():
                raise ValueError("finish action requires finish_reason")
            return self
        if "repair_type" not in self.model_fields_set:
            raise ValueError("repair action missing required fields: repair_type")
        required = {
            "edit_key": self.edit_key,
            "edit_summary": self.edit_summary,
            "changed_clause": self.changed_clause,
            "predicted_logic_before": self.predicted_logic_before,
            "gold_logic_after": self.gold_logic_after,
            "repaired_sql": self.repaired_sql,
            "rationale": self.rationale,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"repair action missing required fields: {', '.join(missing)}")
        return self


class RepairRound(NodeModel):
    round_index: int
    repair_type: RepairType = RepairType.UNCERTAIN
    edit_key: str
    edit_summary: str
    changed_clause: str
    predicted_logic_before: str
    gold_logic_after: str
    input_sql: str
    repaired_sql: str
    execution_success: bool
    result_equal: bool | None = None
    evaluation_reason: str = ""
    execution_error: str = ""


class ConfirmedError(NodeModel):
    id: str = Field(
        description=(
            "Unique ID for one confirmed operation point. When one incoming error contains "
            "several points, create a distinct ID for each split point."
        )
    )
    error_key: str = Field(
        description="Short semantic key for this one operation point, not the whole query repair."
    )
    difference_point: str = Field(
        description=(
            "Exactly one predicted-to-Gold difference, stated as complete input, one primary "
            "behavior kind, and direct output. Use one three-part chain only. Never append a "
            "second behavior that consumes that output; return it under another ID."
        )
    )
    gold_logic: str
    predicted_logic: str
    source_rounds: list[int] = Field(default_factory=list)
    repair_type: RepairType = Field(
        default=RepairType.UNCERTAIN,
        description="How SQL was edited. This label never determines the ontology owner.",
    )
    execution_error: str = ""
    failing_sql_fragment: str = ""

    @field_validator(
        "id",
        "error_key",
        "difference_point",
        "gold_logic",
        "predicted_logic",
        mode="before",
    )
    @classmethod
    def none_to_empty_text(cls, value: Any) -> Any:
        if value is None:
            return ""
        return value

    @model_validator(mode="after")
    def validate_required_text(self) -> ConfirmedError:
        missing = [
            name
            for name, value in {
                "id": self.id,
                "error_key": self.error_key,
                "difference_point": self.difference_point,
                "gold_logic": self.gold_logic,
                "predicted_logic": self.predicted_logic,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise ValueError(f"confirmed error missing required fields: {', '.join(missing)}")
        return self


class RepairGroupingEvidence(NodeModel):
    logical_identity: str = Field(min_length=1)
    causal_intervention: str = Field(min_length=1)
    same_repair_domain: bool
    strict_subset_repairable: bool


class RepairGroup(NodeModel):
    id: str = Field(description="Stable within this review, such as RG1 or RG2.")
    error_ids: list[str] = Field(min_length=1)
    owner: RepairGroupOwner = Field(
        description=(
            "The single ontology responsibility that owns this knowledge unit: semantic_mapping "
            "for a local stored meaning, metric for the complete formula of a named quantity, or "
            "generation_rule for applying already-complete knowledge. A partial defect in a named "
            "Metric's formula remains metric-owned even when that Metric is absent from Working. "
            "A Rule cannot own a Metric formula or an unnamed aggregation, alignment, or "
            "combination step required only to construct that Metric."
        )
    )
    repair_instruction: str = Field(
        description=(
            "The frozen owner-local behavior diff for this Repair Group. Use only two sections: "
            "WRONG_BEHAVIOR for the observed incorrect behavior and CORRECT_BEHAVIOR for the "
            "Gold-proved required behavior. Do not add root-cause theories, domain "
            "generalizations, asset drafts, full SQL plans, case-only values, aliases, evaluation "
            "keys, or behavior owned by another Repair Group."
        )
    )
    base_asset_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "After owner selection, use the exact accepted Working Asset ID when the obligation "
            "corrects, narrows, broadens, or completes the same business term or the same "
            "runtime-visible condition and behavior. Matching identity does not require identical "
            "wording or already-correct content. Null freezes CREATE and is valid only when no "
            "same-owner Working Asset has that identity. A Rule that only produces this new "
            "Rule's complete input is upstream and cannot be its UPDATE base."
        ),
    )
    grouping_reason: str = Field(default="", description="Only for a multi-error group; omit for one error.")
    grouping_evidence: RepairGroupingEvidence | None = Field(default=None, description="Required only for multiple errors. Inapplicable singleton grouping annotations are normalized away before validation.")

    @model_validator(mode="before")
    @classmethod
    def singleton_annotations(cls, value):
        # Grouping evidence relates multiple errors, never the repair itself.
        # Normalize at the shared model boundary, before any nested schema check.
        if isinstance(value, dict) and isinstance(value.get("error_ids"), (list, tuple)) and len(value["error_ids"]) == 1:
            value = dict(value)
            value.pop("grouping_evidence", None)
            value["grouping_reason"] = ""
        return value

    @field_validator("id", "repair_instruction", "grouping_reason", mode="before")
    @classmethod
    def none_to_empty_text(cls, value: Any) -> Any:
        if value is None:
            return ""
        return value

    @model_validator(mode="after")
    def validate_group(self) -> RepairGroup:
        if not self.id.strip():
            raise ValueError("repair group missing id")
        if not self.repair_instruction.strip():
            raise ValueError("repair group missing repair_instruction")
        if self.base_asset_id is not None and not self.base_asset_id.strip():
            raise ValueError("repair group base_asset_id cannot be empty")
        if len(self.error_ids) != len(set(self.error_ids)):
            raise ValueError(f"repair group contains duplicate error IDs: {self.id}")
        if any(not error_id.strip() for error_id in self.error_ids):
            raise ValueError(f"repair group contains empty error ID: {self.id}")
        if len(self.error_ids) > 1 and not self.grouping_reason.strip():
            raise ValueError(
                f"multi-error repair group requires grouping_reason: {self.id}"
            )
        if len(self.error_ids) > 1:
            evidence = self.grouping_evidence
            if evidence is None:
                raise ValueError(
                    f"multi-error repair group requires grouping_evidence: {self.id}"
                )
            if not evidence.same_repair_domain:
                raise ValueError(
                    f"multi-error repair group crosses repair domains: {self.id}"
                )
            if evidence.strict_subset_repairable:
                raise ValueError(
                    f"multi-error repair group contains independently repairable errors: {self.id}"
                )
        elif self.grouping_evidence is not None:
            raise ValueError(
                f"singleton repair group cannot carry grouping_evidence: {self.id}"
            )
        return self


class ExecutionFailureAnalysis(NodeModel):
    summary: str = Field(min_length=1)
    confirmed_errors: list[ConfirmedError] = Field(min_length=1)
    repair_groups: list[RepairGroup] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_partition(self) -> ExecutionFailureAnalysis:
        ids = [error.id for error in self.confirmed_errors]
        if len(ids) != len(set(ids)):
            raise ValueError("execution failure analysis contains duplicate error IDs")
        grouped_ids = [error_id for group in self.repair_groups for error_id in group.error_ids]
        if sorted(grouped_ids) != sorted(ids):
            raise ValueError(
                "execution failure repair groups must cover every confirmed error exactly once"
            )
        return self


class RepairReview(NodeModel):
    summary: str = ""
    confirmed_errors: list[ConfirmedError] = Field(min_length=1)
    repair_groups: list[RepairGroup] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_review_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        migrated_legacy_errors = False
        if "summary" not in data and "clean_summary" in data:
            data["summary"] = data.pop("clean_summary")
        if "confirmed_errors" not in data and "minimal_required_edits" in data:
            data["confirmed_errors"] = data.pop("minimal_required_edits")
            migrated_legacy_errors = True
        # Accept stored reviews produced before candidate selection moved downstream.
        data.pop("generation_rule_candidates", None)
        data.pop("generation_rule_decisions", None)
        data.pop("ignored_edits", None)
        confirmed_errors = []
        for error in data.get("confirmed_errors") or []:
            if isinstance(error, dict):
                error = dict(error)
                if migrated_legacy_errors and error.get("necessity") != "necessary":
                    continue
                error.pop("necessity", None)
                error.pop("question_evidence", None)
            confirmed_errors.append(error)
        data["confirmed_errors"] = confirmed_errors
        if migrated_legacy_errors and not confirmed_errors:
            raise ValueError("legacy repair review requires at least 1 item")
        if "repair_groups" not in data and confirmed_errors and migrated_legacy_errors:
            data["repair_groups"] = [
                {
                    "id": f"RG{index}",
                    "error_ids": [error.get("id")],
                    "repair_instruction": error.get("gold_logic"),
                    "grouping_reason": "",
                }
                for index, error in enumerate(confirmed_errors, start=1)
                if isinstance(error, dict) and error.get("id")
            ]
        return data

    @model_validator(mode="after")
    def validate_review_partition(self) -> RepairReview:
        error_ids = [error.id for error in self.confirmed_errors]
        group_ids = [group.id for group in self.repair_groups]
        if len(error_ids) != len(set(error_ids)):
            raise ValueError("confirmed errors contain duplicate IDs")
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("repair groups contain duplicate IDs")
        grouped_error_ids = [
            error_id for group in self.repair_groups for error_id in group.error_ids
        ]
        duplicated_group_errors = sorted(
            {
                error_id
                for error_id in grouped_error_ids
                if grouped_error_ids.count(error_id) > 1
            }
        )
        missing_group_errors = sorted(set(error_ids) - set(grouped_error_ids))
        unknown_group_errors = sorted(set(grouped_error_ids) - set(error_ids))
        if duplicated_group_errors or missing_group_errors or unknown_group_errors:
            raise ValueError(
                "repair groups must partition confirmed errors exactly once: "
                f"missing={missing_group_errors}, unknown={unknown_group_errors}, "
                f"duplicate={duplicated_group_errors}"
            )
        errors_by_id = {error.id: error for error in self.confirmed_errors}
        for group in self.repair_groups:
            representation_ids = [
                error_id
                for error_id in group.error_ids
                if errors_by_id[error_id].repair_type == RepairType.REPRESENTATION
            ]
            if representation_ids and len(group.error_ids) != 1:
                raise ValueError(
                    "representation errors require singleton repair groups: "
                    f"group={group.id}, representation_errors={representation_ids}"
                )
        return self


class RepairAblationProposal(NodeModel):
    ablated_sql: str = ""
    rationale: str = ""

    @field_validator("ablated_sql", "rationale", mode="before")
    @classmethod
    def none_to_empty_text(cls, value: Any) -> Any:
        if value is None:
            return ""
        return value


class RepairAblation(NodeModel):
    round_index: int
    repair_type: RepairType = RepairType.UNCERTAIN
    ablated_sql: str = ""
    execution_success: bool
    result_equal: bool | None = None
    necessity: RepairNecessity
    rationale: str = ""
    execution_error: str = ""
    evaluation_reason: str = ""

    @field_validator(
        "ablated_sql",
        "rationale",
        "execution_error",
        "evaluation_reason",
        mode="before",
    )
    @classmethod
    def none_to_empty_text(cls, value: Any) -> Any:
        if value is None:
            return ""
        return value


class RepairJointAblation(NodeModel):
    round_indexes: list[int]
    ablated_sql: str = ""
    execution_success: bool
    result_equal: bool | None = None
    necessity: RepairNecessity
    rationale: str = ""
    execution_error: str = ""
    evaluation_reason: str = ""

    @model_validator(mode="after")
    def validate_round_indexes(self) -> RepairJointAblation:
        if len(self.round_indexes) < 2:
            raise ValueError("joint ablation requires at least two round indexes")
        if len(self.round_indexes) != len(set(self.round_indexes)):
            raise ValueError("joint ablation round indexes must be unique")
        return self


class RepairSession(NodeModel):
    status: Literal[
        "matched",
        "budget_exhausted",
        "analysis_incomplete",
        "oracle_unavailable",
        "complete_equal_result",
        "repair_invalid",
    ]
    original_sql: str
    final_sql: str
    rounds: list[RepairRound] = Field(default_factory=list)
    ablations: list[RepairAblation] = Field(default_factory=list)
    joint_ablations: list[RepairJointAblation] = Field(default_factory=list)
    review: RepairReview | None = None


class ErrorAnalysisReport(NodeModel):
    mode: Literal["minimal_repair"] = "minimal_repair"
    summary: str = ""
    final_comparison: dict[str, Any] = Field(default_factory=dict)
    points: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    confirmed_errors: list[ErrorCandidate] = Field(default_factory=list)
    error_candidates: list[ErrorCandidate] = Field(default_factory=list)
    repair_groups: list[RepairGroup] = Field(default_factory=list)
    non_errors: list[dict[str, Any]] = Field(default_factory=list)
    inconclusive: list[dict[str, Any]] = Field(default_factory=list)
    latent_risk_points: list[dict[str, Any]] = Field(default_factory=list)
    completeness: dict[str, Any] = Field(default_factory=dict)
    repair_session: RepairSession | None = None

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_rule_candidate_selection(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data.pop("generation_rule_candidates", None)
        data.pop("generation_rule_decisions", None)
        return data
