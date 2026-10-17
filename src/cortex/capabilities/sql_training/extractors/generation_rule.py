from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cortex.capabilities.sql_training.contracts.references import physical_reference_catalog
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_generation_rule_id, normalize_refs
from cortex.capabilities.sql_training.extractors.common import (
    CandidateSource,
    TypedCandidateExtractor,
    ensure_list,
    normalize_properties_payload,
)
from cortex.capabilities.sql_training.extractors.joinability_routes import (
    exact_gold_join_fragments,
    gold_join_column_reference_sets,
    pure_routing_reference_sets,
)
from cortex.capabilities.sql_training.llm.parsing import run_llm_extraction
from cortex.capabilities.sql_training.models import GenerationRuleTriggers

_GENERATION_RULE_BIZ_KEYS = {
    "references",
    "operation_intent",
    "triggers",
    "condition",
    "action",
    "reason",
}


def _normalize_rule_name(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return _truncate_display_name(text) or None


def _truncate_display_name(value: str, *, limit: int = 100) -> str:
    if len(value) <= limit:
        return value
    prefix = value[:limit].rstrip()
    if " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0].rstrip()
    return prefix or value[:limit]


def _uses_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def _matches_cjk_question(value: str) -> bool:
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", value))
    latin_count = len(re.findall(r"[A-Za-z]", value))
    return cjk_count >= 2 and cjk_count / max(cjk_count + latin_count, 1) >= 0.3


def _action_prose(value: str) -> str:
    """Separate business prose from the labelled local SQL pattern suffix."""
    return re.split(r"\bSQL\s+pattern\s*:", value, maxsplit=1, flags=re.IGNORECASE)[0]


def _question_trigger_is_grounded(phrase: str, question: str) -> bool:
    return " ".join(phrase.casefold().split()) in " ".join(question.casefold().split())


class InitialQuestionTriggerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrases: list[str] = Field(
        min_length=1,
        description=(
            "One or more compact phrases copied from the Question. Each phrase is an independent "
            "OR retrieval signal. Preserve the exact source words and current values here; put "
            "parameterized applicability in condition."
        ),
    )

    @field_validator("phrases", mode="before")
    @classmethod
    def _normalize_phrases(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("phrases must be a list of strings")
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise ValueError("phrases must be a list of strings")
            phrase = " ".join(item.split())
            key = phrase.casefold()
            if phrase and key not in seen:
                seen.add(key)
                normalized.append(phrase)
        return normalized


class InitialGenerationRuleTriggersPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: InitialQuestionTriggerPayload = Field(
        description="Required exact Question-side retrieval phrases."
    )


InitialOperationIntent = Literal[
    "joinability",
    "count_aggregation",
    "existence_check",
    "boolean_output_encoding",
    "result_deduplication",
    "extreme_value_selection",
    "top_k_selection",
    "rank_output",
]


class InitialGenerationRuleEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gold_sql_grounding: list[str] = Field(
        min_length=1,
        description=(
            "Smallest exact substrings of Gold SQL that jointly prove the action. Include all "
            "behavior-defining operators and no unrelated query shell. This evidence is not "
            "persisted in the Generation Rule."
        ),
    )

    @field_validator("gold_sql_grounding", mode="before")
    @classmethod
    def _normalize_gold_grounding(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("gold_sql_grounding must be a list of strings")
        normalized = [str(item).strip() for item in value if str(item).strip()]
        return list(dict.fromkeys(normalized))


class GenerationRuleCandidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_intent: InitialOperationIntent = Field(
        description=(
            "Exact Initial Operation Registry intent that guided discovery. This field does not "
            "replace condition/action, define Rule identity, or prove the Rule correct."
        )
    )
    evidence: InitialGenerationRuleEvidencePayload
    triggers: InitialGenerationRuleTriggersPayload = Field(
        description="Initial retrieval evidence contains exact Question phrases only.",
    )
    condition: str = Field(
        description=(
            "The smallest positive applicability condition supported only by the Question's NL "
            "meaning. Every fact must be entailed by the exact Question triggers. Do not "
            "mention or infer a table, column, row, key, JOIN, subquery, CTE, SQL clause, schema "
            "cardinality, Gold, predicted SQL, error, or current runtime value. Use the Question's "
            "business words and refer naturally to a supplied date, threshold, or requested count; "
            "do not use role placeholders."
        ),
    )
    action: str = Field(
        description=(
            "One complete reusable behavior required by the condition and proved by Gold. SQL "
            "operations and necessary table or column bindings may be explicit. Keep several "
            "steps only when each is needed for the same behavior. Use business words in prose; "
            "role placeholders are allowed inside a local SQL pattern whose roles the prose "
            "explains. Include that pattern when operation choice or order needs clarification. "
            "Omit independently reusable behavior and current values."
        ),
    )
    reason: str = Field(
        description=(
            "Compare Gold's action with another plausible implementation of the same operation "
            "intent and state the observable row, value, grain, order, count, tie, or NULL-result "
            "difference. The action and reason must assign the same rows, cardinality, order, "
            "ties, values, and NULL behavior to the stated action; describe those outcomes only "
            "as the alternative when they belong to the alternative. Omitting the requested "
            "operation is not an alternative implementation."
        ),
    )
    references: list[str] = Field(
        default_factory=list,
        description=(
            "Exact supplied Node IDs required by the condition or by the action's governed operand "
            "or target. All references must hold together for the complete Rule contract. "
            "Replaceable implementation details and reason evidence are not references. Same-run "
            "Concept and Metric candidate IDs are allowed."
        ),
    )

    @field_validator("condition", "action", "reason", mode="before")
    @classmethod
    def _normalize_rule_text(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("condition, action, and reason must be non-empty strings")
        return value.strip()

    @field_validator("references")
    @classmethod
    def _normalize_references(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_refs(value)))


class NamedGenerationRuleCandidatePayload(GenerationRuleCandidatePayload):
    rule_name: str = Field(
        min_length=1,
        description=(
            "Short operation name in the Question's language. Start from the Question's operation "
            "phrase and preserve concrete business inputs kept by the condition, action, or "
            "references. Do not replace a retained business term with a broader class word. Use "
            "a general name only when the complete Rule uses replaceable roles."
        ),
    )

    @field_validator("rule_name", mode="before")
    @classmethod
    def _normalize_rule_name(cls, value: Any) -> str:
        normalized = _normalize_rule_name(value)
        if normalized is None:
            raise ValueError("rule_name must be a stable behavior-oriented name")
        return normalized


class GenerationRuleExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assets: list[NamedGenerationRuleCandidatePayload] = Field(
        default_factory=list,
        description=(
            "The complete minimal set of Initial Generation Rules proved by this case. Return [] "
            "when no Question operation matches the Registry and passes every extraction test."
        ),
    )


def extract_generation_rule_assets(
    llm: Any,
    record: dict[str, Any],
    prompt: str,
    *,
    trace_capture: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    sm_assets = record.get("current_case_sm_candidates") or record.get("existing_sm_assets") or []
    if not isinstance(sm_assets, (list, tuple)):
        sm_assets = []
    metric_assets = (
        record.get("current_case_metric_candidates") or record.get("existing_metric_assets") or []
    )
    if not isinstance(metric_assets, (list, tuple)):
        metric_assets = []
    human_payload = {
        "question": str(record.get("question") or ""),
        "evidence": record.get("evidence"),
        "sql": str(record.get("sql") or ""),
        "database_dialect": str(record.get("database_dialect") or ""),
        "gold_column_metadata": record.get("gold_column_metadata") or {},
        "current_case_sm_candidates": _summarize_sm_assets(sm_assets),
        "current_case_metric_candidates": _summarize_metric_assets(metric_assets),
        "database_context": record.get("database_context") or {},
        "direct_join_alternatives": record.get("direct_join_alternatives") or [],
        "gold_join_fragments": exact_gold_join_fragments(
            str(record.get("sql") or ""),
            dialect=str(record.get("database_dialect") or "") or None,
        ),
    }
    physical_ids = physical_reference_catalog(human_payload["database_context"])
    human_payload["physical_reference_catalog"] = sorted(physical_ids)
    same_run_semantic_ids = {
        str(asset.get("id") or "")
        for asset in (
            *human_payload["current_case_sm_candidates"],
            *human_payload["current_case_metric_candidates"],
        )
        if str(asset.get("id") or "")
    }
    allowed_reference_ids = physical_ids | same_run_semantic_ids
    human_payload["condition_reference_catalog"] = sorted(allowed_reference_ids)
    gold_routing_paths = pure_routing_reference_sets(
        human_payload["sql"], dialect=human_payload["database_dialect"]
    )
    gold_join_column_paths = gold_join_column_reference_sets(
        human_payload["sql"], dialect=human_payload["database_dialect"]
    )
    human_payload["gold_join_paths_with_intermediate_table"] = [
        sorted(path) for path in sorted(gold_routing_paths, key=lambda item: tuple(sorted(item)))
    ]
    human_payload["gold_join_path_column_sets"] = [
        sorted(path)
        for path in sorted(gold_join_column_paths, key=lambda item: tuple(sorted(item)))
    ]
    errors: list[str] = []
    for attempt in range(1, 3):
        request = dict(human_payload)
        if errors:
            language_instruction = (
                " This Question is Chinese: rule_name, condition, action, and reason must each be "
                "written in Chinese prose. Keep SQL syntax and role placeholders in the local "
                "SQL pattern; explain their roles in Chinese."
                if _uses_cjk(human_payload["question"])
                else ""
            )
            request["validation_feedback"] = {
                "error": errors[-1],
                "instruction": (
                    "Return the complete minimal Rule set. Every Rule needs an NL phrase, "
                    "an exact Initial Operation Registry operation_intent, and exact Gold SQL "
                    "grounding. Every question trigger must be an exact Question phrase. "
                    "use only exact condition or action-governed dependencies from "
                    "condition_reference_catalog, and remove replaceable or query-coincident "
                    "references. "
                    "Write all Rule prose in the Question's language; language correction must "
                    "retain a valid local SQL pattern and its operation choice/order. "
                    "Build condition only from "
                    "Question NL; remove inferred SQL, Schema, JOIN, subquery, key, and "
                    "cardinality facts from condition." + language_instruction
                ),
            }
        candidates = run_llm_extraction(
            llm=llm,
            prompt=prompt,
            output_model=GenerationRuleExtractionOutput,
            human_payload=request,
            map_result=lambda parsed: [
                candidate
                for item in _payload_assets(parsed)
                if (candidate := _generation_rule_candidate_to_raw_candidate(item)) is not None
            ],
            trace_capture=trace_capture if attempt == 1 else None,
        )
        invalid_initial_rules = sorted(
            {
                candidate.get("name", "")
                for candidate in candidates
                if not candidate.get("triggers", {}).get("question", {}).get("phrases")
            }
        )
        ungrounded_triggers = sorted(
            {
                candidate.get("name", "")
                for candidate in candidates
                if any(
                    not _question_trigger_is_grounded(phrase, human_payload["question"])
                    for phrase in candidate.get("triggers", {})
                    .get("question", {})
                    .get("phrases", [])
                )
            }
        )
        unresolved = sorted(
            {
                reference
                for candidate in candidates
                for reference in candidate.get("references", [])
                if reference not in allowed_reference_ids
            }
        )
        ungrounded_gold = {
            candidate.get("name", ""): [
                grounding
                for grounding in candidate.get("_gold_sql_grounding", [])
                if grounding not in human_payload["sql"]
            ]
            for candidate in candidates
        }
        ungrounded_gold = {
            name: grounding for name, grounding in ungrounded_gold.items() if grounding
        }
        language_mismatches = sorted(
            {
                candidate.get("name", "")
                for candidate in candidates
                if _uses_cjk(human_payload["question"])
                and not all(
                    _matches_cjk_question(
                        _action_prose(str(candidate.get(field) or ""))
                        if field == "action"
                        else str(candidate.get(field) or "")
                    )
                    for field in ("name", "condition", "action", "reason")
                )
            }
        )
        invalid_joinability_paths = sorted(
            {
                candidate.get("name", "")
                for candidate in candidates
                if candidate.get("operation_intent") == "joinability"
                and not any(
                    path
                    == {
                        reference
                        for reference in candidate.get("references", [])
                        if str(reference).startswith("column.")
                    }
                    for path in gold_join_column_paths
                )
            }
        )
        if (
            not invalid_initial_rules
            and not ungrounded_triggers
            and not unresolved
            and not ungrounded_gold
            and not language_mismatches
            and not invalid_joinability_paths
        ):
            if trace_capture is not None:
                trace_capture["construction_attempt"] = attempt
                trace_capture["construction_errors"] = errors
                trace_capture["accepted_gold_sql_grounding"] = {
                    candidate.get("id", ""): candidate.get("_gold_sql_grounding", [])
                    for candidate in candidates
                }
            for candidate in candidates:
                candidate.pop("_gold_sql_grounding", None)
            return candidates
        rejected_names = (
            set(invalid_initial_rules)
            | set(ungrounded_triggers)
            | set(language_mismatches)
            | set(ungrounded_gold)
            | set(invalid_joinability_paths)
        )
        valid_candidates = [
            {
                **candidate,
                "references": [
                    reference
                    for reference in candidate.get("references", [])
                    if reference in allowed_reference_ids
                ],
            }
            for candidate in candidates
            if candidate.get("name", "") not in rejected_names
        ]
        accepted_gold_sql_grounding = {
            candidate.get("id", ""): candidate.get("_gold_sql_grounding", [])
            for candidate in valid_candidates
        }
        for candidate in valid_candidates:
            candidate.pop("_gold_sql_grounding", None)
        errors.append(
            "Initial Generation Rule contract failed: "
            f"invalid_nl_only_rules={invalid_initial_rules}, "
            f"ungrounded_question_triggers={ungrounded_triggers}, "
            f"ungrounded_gold_sql={ungrounded_gold}, "
            f"unresolved_references={unresolved}, "
            f"question_language_mismatches={language_mismatches}, "
            f"invalid_joinability_paths={invalid_joinability_paths}"
        )
        if attempt == 2:
            if trace_capture is not None:
                trace_capture["construction_attempt"] = attempt
                trace_capture["construction_errors"] = errors
                trace_capture["discarded_candidate_names"] = sorted(rejected_names)
                trace_capture["discarded_unresolved_references"] = unresolved
                trace_capture["accepted_gold_sql_grounding"] = accepted_gold_sql_grounding
            return valid_candidates
    raise ValueError(f"Generation Rule construction failed after 2 attempts: {errors[-1]}")


def _summarize_sm_assets(sm_assets: list[Any]) -> list[dict[str, Any]]:
    """Extract only the key fields from SM assets for the prompt."""
    summaries: list[dict[str, Any]] = []
    for asset in sm_assets:
        if isinstance(asset, dict):
            summaries.append(
                {
                    "id": asset.get("id", ""),
                    "node_type": asset.get("node_type", NodeType.CONCEPT.value),
                    "name": asset.get("name", ""),
                    "targets": (asset.get("properties") or {}).get("targets", []),
                }
            )
    return summaries


def _summarize_metric_assets(metric_assets: list[Any]) -> list[dict[str, Any]]:
    """Extract only the key fields from Metric assets for the prompt."""
    summaries: list[dict[str, Any]] = []
    for asset in metric_assets:
        if isinstance(asset, dict):
            properties = asset.get("properties") or asset
            expression = properties.get("expression") or {}
            summaries.append(
                {
                    "id": asset.get("id", ""),
                    "node_type": asset.get("node_type", NodeType.METRIC.value),
                    "name": asset.get("name", ""),
                    "expression": expression if isinstance(expression, dict) else {},
                    "references": properties.get("references", []),
                }
            )
    return summaries


def _payload_assets(payload: Any) -> list[NamedGenerationRuleCandidatePayload]:
    if isinstance(payload, GenerationRuleExtractionOutput):
        return list(payload.assets)
    raw_assets = payload.get("assets", []) if isinstance(payload, dict) else []
    if not isinstance(raw_assets, list):
        raw_assets = [raw_assets]
    allowed_fields = set(NamedGenerationRuleCandidatePayload.model_fields.keys())
    validated: list[NamedGenerationRuleCandidatePayload] = []
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        stripped = {k: v for k, v in item.items() if k in allowed_fields}
        try:
            validated.append(NamedGenerationRuleCandidatePayload.model_validate(stripped))
        except Exception:
            continue  # missing required fields
    return validated


def render_generation_rule_text(*, condition: str, action: str, reason: str) -> str:
    return f"WHEN {condition} MUST {action} BECAUSE {reason}"


def _generation_rule_candidate_to_raw_candidate(
    item: NamedGenerationRuleCandidatePayload,
) -> dict[str, Any] | None:
    phrases = item.triggers.question.phrases
    operations: list[str] = []
    references = list(item.references)
    if not (phrases or operations or references):
        return None

    rendered = render_generation_rule_text(
        condition=item.condition,
        action=item.action,
        reason=item.reason,
    )
    return {
        "node_type": NodeType.GENERATION_RULE.value,
        "id": make_generation_rule_id(
            rule_name=item.rule_name,
            question_phrases=phrases,
            sql_operations=operations,
            references=references,
            condition=item.condition,
        ),
        "name": _generation_rule_display_name(
            rule_name=item.rule_name,
            phrases=phrases,
            operations=operations,
            references=references,
            condition=item.condition,
        ),
        "aliases": [],
        "description": rendered,
        "tags": [],
        "references": references,
        "operation_intent": item.operation_intent,
        "triggers": {
            "question": {"phrases": phrases},
            "sql": {"columns": [], "operations": []},
        },
        "condition": item.condition,
        "action": item.action,
        "reason": item.reason,
        "_gold_sql_grounding": item.evidence.gold_sql_grounding,
    }


def _generation_rule_display_name(
    *,
    rule_name: str | None,
    phrases: list[str],
    operations: list[str],
    references: list[str],
    condition: str,
) -> str:
    if rule_name:
        return _truncate_display_name(rule_name)
    seed = next(iter(phrases or operations or references), condition)
    return _truncate_display_name(seed)


class GenerationRuleExtractor(TypedCandidateExtractor):
    asset_type = NodeType.GENERATION_RULE

    def _normalize(self, raw_asset: dict[str, Any], source: CandidateSource) -> dict[str, Any]:
        normalized, properties = normalize_properties_payload(
            raw_asset, property_keys=_GENERATION_RULE_BIZ_KEYS
        )
        rule_name = _normalize_rule_name(normalized.pop("rule_name", None))
        normalized["node_type"] = NodeType.GENERATION_RULE

        condition = str(properties.get("condition", "")).strip()
        action = str(properties.get("action", "")).strip()
        reason = str(properties.get("reason", "")).strip()
        raw_references = [str(item) for item in ensure_list(properties.get("references"))]
        try:
            references = list(dict.fromkeys(normalize_refs(raw_references)))
        except ValueError:
            references = raw_references
        try:
            triggers = GenerationRuleTriggers.model_validate(properties.get("triggers"))
        except Exception:
            triggers = None

        stable_behavior_name = (
            rule_name or _normalize_rule_name(normalized.get("name")) or action or condition
        )

        if not normalized.get("id"):
            normalized["id"] = make_generation_rule_id(
                rule_name=stable_behavior_name,
                question_phrases=triggers.question.phrases if triggers else [],
                sql_operations=triggers.sql.operations if triggers else [],
                references=references,
                condition=condition,
            )

        rendered = render_generation_rule_text(
            condition=condition,
            action=action,
            reason=reason,
        )
        normalized["description"] = rendered

        if not normalized.get("name"):
            normalized["name"] = _generation_rule_display_name(
                rule_name=stable_behavior_name,
                phrases=triggers.question.phrases if triggers else [],
                operations=triggers.sql.operations if triggers else [],
                references=references,
                condition=condition,
            )

        normalized["aliases"] = [str(item) for item in ensure_list(normalized.get("aliases"))]
        normalized["tags"] = []

        properties["references"] = references
        if triggers is not None:
            properties["triggers"] = triggers.model_dump(mode="json")
        normalized["properties"] = properties
        return super()._normalize(normalized, source)
