from __future__ import annotations

import json
import re
from typing import Any

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlglot import expressions

from cortex.capabilities.sql_training.contracts.metric import (
    metric_dependency_diagnostics,
    validate_metric_contract,
)
from cortex.capabilities.sql_training.contracts.references import physical_reference_catalog
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.extractors.common import (
    CandidateSource,
    TypedCandidateExtractor,
    ensure_list,
    normalize_properties_payload,
)
from cortex.capabilities.sql_training.llm.parsing import run_llm_extraction
from cortex.capabilities.sql_training.models import NodeAsset

_METRIC_BIZ_KEYS = {
    "references",
    "metric_kind",
    "pattern",
    "expression",
    "aggregation_policy",
    "calculation_policy",
    "sql_generation_plans",
}


class MetricExpressionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expr: str = Field(
        description=(
            "The complete calculation formula for this Metric only. Do not include other "
            "Question concepts, filters, grouping, ranking, ordering, or display instructions. "
            "Bind operands "
            "with ${name} placeholders; names may contain letters, underscores, and numbers after "
            "the first character, but no spaces, parentheses, or hyphens. Keep every operation, "
            "condition, constant, internal grain, and NULL/zero behavior required by the formula, "
            "including ones not spelled out in the Question. The plan cannot supply a missing "
            "calculation. Read every Column placeholder as that stored value; if a required "
            "calculation is then missing, write it in expr. Preserve Gold's aggregate modifier: "
            "COUNT and COUNT(DISTINCT ...) are "
            "different formulas, and DISTINCT cannot be added from an assumption. Preserve every "
            "Gold function, cast, date conversion, operator, and operation order used by the "
            "Metric formula; plan prose cannot replace them."
        )
    )
    refs: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map every placeholder key, without ${}, to one canonical column.<table>.<column> or "
            "metric.<exact asset name> ID. Keys must be identifier-like and exactly match the "
            "placeholders in expr; for example ${free_meal_count} uses the key free_meal_count. "
            "A Column binding denotes the stored value, not an implicit aggregate or derivation. "
            "Inline unnamed derivations in expr; reference a derived value through a Metric only "
            "when it has valid independent identity. For a Metric emitted in this result, use "
            "its name, not its plan ID or a SQL alias."
        ),
    )


class SQLGenerationPlanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,99}$",
        description=(
            "A stable unique local plan ID within this Metric, such as overall_revenue. Use "
            "value-free snake_case; this is not an ontology Node ID."
        ),
    )
    application: str = Field(
        description=(
            "One sentence describing this calculation context. It may mention real fields used to "
            "select input records or organize results, but no comparison direction, equality, "
            "range, membership test, value, or predicate, without making that use mandatory. "
            "Omit the "
            "field role when it would imply the request's condition or mandatory grouping. Do not "
            "include ranking, ordering, limits, "
            "or display behavior. "
            "Keep a condition or constant required by the formula."
        )
    )
    steps: list[str] = Field(
        min_length=1,
        max_length=8,
        description=(
            "One to eight source-to-result steps that explain the complete expression. State "
            "each real field's business role and operation without relying on the Question. A "
            "separately "
            "requested filter or grouping may be mentioned where it acts, using its actual field, "
            "but say only that the field selects input records or organizes results. Do not state "
            "a comparison direction, equality, range, membership test, value, or predicate, and do "
            "not make the use mandatory. Keep formula "
            "conditions. Omit a request field role when it would repeat or imply the request's "
            "predicate, value, or mandatory grouping. Include required constants, grain, order, "
            "keys, cardinality, and NULL behavior. "
            "Do not include result selection, ranking, ordering, limits, or display behavior. "
            "Every physical table must be <table> "
            "and every physical column must be fully qualified as <table.column>. Never use "
            "<column>, bare column names, ontology IDs, or complete SQL."
        ),
    )


class MetricCandidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        description=(
            "Briefly explain the business meaning and calculation of the same Metric as "
            "expression, in the Question's language. Do not include other Question concepts, "
            "filters or grouping separately requested from the Metric calculation. Do not include "
            "output instructions. Keep conditions required by the formula even when Question or "
            "evidence explains them outside the Metric name. Do not add an operation absent from "
            "expression and Gold; in "
            "particular, call a count distinct only when expression uses DISTINCT."
        )
    )
    name: str = Field(
        description=(
            "The exact smallest Question term that needs this complete calculation. Only a "
            "supporting dependency may use a trustworthy business-facing Gold output alias. Do not "
            "use a helper alias, formula part, or intermediate value. Split out smaller terms that "
            "separately match an entity, stored meaning, existing Metric, or requested SQL "
            "operation. If the phrase only asks to apply an operation to a complete entity, stored "
            "value, or existing Metric, do not use the combined phrase. Keep the complete term "
            "when it names a calculation of its own."
        )
    )
    pattern: str = Field(
        min_length=1,
        description=(
            "A stable readable business calculation pattern in the Question's primary language. "
            "State the operands, formula conditions, relationships, constants, and boundaries. "
            "Do not copy another Question concept, filter, grouping, or output instruction."
        ),
    )
    expression: MetricExpressionPayload
    sql_generation_plan: SQLGenerationPlanPayload = Field(
        description=(
            "One physical plan for the complete Metric calculation evidenced by this Question and "
            "SQL. Keep the proved calculation and, when useful, the roles of real fields used to "
            "filter or group current inputs; omit their request values and predicates. Omit the "
            "role itself when it would imply that predicate or a mandatory grouping. "
            "For a composite Metric, explain each source's operations and how the results align, "
            "without copying filters between sources or inventing alternatives. "
            "Do not use prose placeholders such as {question_filters} or "
            "{grouping_fields}. Write prose in the Question's language."
        )
    )

    @model_validator(mode="before")
    @classmethod
    def _discard_legacy_derived_fields(cls, value: Any) -> Any:
        """Accept old Initial traces while keeping derived fields out of the LLM schema."""

        if not isinstance(value, dict):
            return value
        cleaned = dict(value)
        cleaned.pop("aliases", None)
        cleaned.pop("references", None)
        return cleaned


class MetricExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_sets: list[MetricCandidateSetPayload] = Field(
        default_factory=list,
        description=(
            "Independent formula-dependency components returned by this invocation. Put Metrics "
            "that are only filtered, grouped, projected, or returned together in separate sets. "
            "Return [] when this invocation finds no qualifying smallest Question term whose "
            "one-to-one implementation necessarily includes a calculation."
        ),
    )


class MetricCandidateSetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(
        min_length=1,
        description=(
            "Unique value-free ID for this temporary set, such as revenue_growth_set. It is not "
            "an ontology asset ID and is not persisted."
        ),
    )
    assets: list[MetricCandidatePayload] = Field(
        min_length=1,
        description=(
            "One connected Metric formula component: normally one directly requested root plus "
            "only its new Metric dependencies. Several roots may share a set only through a "
            "shared new dependency. Independent roots must be returned in separate candidate sets "
            "even when one query returns them together. Do not output "
            "asset IDs, aliases, references, or root/dependency labels. Code derives IDs and "
            "references from names and expression refs; plan tokens remain plan-local."
        ),
    )


def extract_metric_assets(
    llm: Any,
    record: dict[str, Any],
    prompt: str,
    *,
    trace_capture: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    current_metrics = tuple(
        NodeAsset.model_validate(row)
        for row in record.get("current_metrics", [])
        if isinstance(row, (dict, NodeAsset))
    )
    prior_case_metrics = tuple(
        _raw_metric_asset(row)
        for row in record.get("prior_case_metric_candidates", [])
        if isinstance(row, dict)
    )
    prior_case_metric_ids = {asset.id for asset in prior_case_metrics}
    human_payload = {
        "question": str(record.get("question") or ""),
        "evidence": record.get("evidence"),
        "sql": str(record.get("sql") or ""),
        "database_dialect": str(record.get("database_dialect") or ""),
        "gold_column_metadata": record.get("gold_column_metadata") or {},
        "database_context": record.get("database_context") or {},
        "physical_reference_catalog": sorted(
            physical_reference_catalog(record.get("database_context") or {})
        ),
        "current_metrics": [_metric_prompt_projection(asset) for asset in current_metrics],
    }
    if prior_case_metrics:
        human_payload["prior_case_metric_candidates"] = [
            _metric_prompt_projection(asset) for asset in prior_case_metrics
        ]
    if record.get("extraction_self_check"):
        human_payload["extraction_self_check"] = record["extraction_self_check"]
    column_refs = _column_ref_index(
        sql=human_payload["sql"],
        database_context=human_payload["database_context"],
    )
    columns_by_table = _columns_by_table(
        sql=human_payload["sql"],
        database_context=human_payload["database_context"],
    )
    gold_output_aliases = _gold_output_aliases(human_payload["sql"])
    final_accepted: dict[str, list[dict[str, Any]]] = {}
    preserved_accepted: dict[str, list[dict[str, Any]]] = {}
    extraction_payload = human_payload
    generation_attempts: list[dict[str, Any]] = []
    terminal_contract_errors: list[dict[str, Any]] = []
    for generation_attempt in range(1, 3):
        attempt_trace: dict[str, Any] = {}
        try:
            candidates = run_llm_extraction(
                llm=llm,
                prompt=prompt,
                output_model=MetricExtractionOutput,
                human_payload=extraction_payload,
                map_result=lambda parsed: _flatten_candidate_sets(
                    parsed,
                    column_refs=column_refs,
                    columns_by_table=columns_by_table,
                ),
                trace_capture=attempt_trace,
            )
        except (ValidationError, ValueError) as exc:
            construction_error = {
                "stage": "metric_extraction",
                "error": f"OUTPUT_SCHEMA_OR_CANDIDATE_SET_INVALID: {exc}",
            }
            generation_attempts.append(
                {
                    "attempt": generation_attempt,
                    "candidate_ids": [],
                    "retryable_errors": [construction_error],
                    "trace": attempt_trace,
                }
            )
            if generation_attempt == 2:
                _record_metric_extraction_trace(trace_capture, generation_attempts)
                raise
            extraction_payload = {
                **human_payload,
                "validation_feedback": {
                    "rejected_metrics": [construction_error],
                    "instruction": _metric_construction_retry_instruction(str(exc)),
                },
            }
            continue
        retryable_errors: list[dict[str, Any]] = []
        by_set: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            by_set.setdefault(str(candidate["_candidate_set_id"]), []).append(candidate)
        preserved_asset_owners = {
            str(candidate.get("id") or ""): candidate_set_id
            for candidate_set_id, accepted_candidates in preserved_accepted.items()
            for candidate in accepted_candidates
        }
        visible_metrics = [*current_metrics, *prior_case_metrics]
        for candidate_set_id, set_candidates in by_set.items():
            set_errors: list[dict[str, Any]] = []
            typed_assets: list[NodeAsset] = []
            member_ids = {str(candidate.get("id") or "") for candidate in set_candidates}
            root_ids = set(_raw_metric_set_root_ids(set_candidates))
            if not root_ids:
                set_errors.append(
                    {
                        "candidate_set_id": candidate_set_id,
                        "error": "Metric candidate set must contain at least one dependency root",
                    }
                )
            overlapping_ids = sorted(member_ids & set(preserved_asset_owners))
            if overlapping_ids:
                set_errors.append(
                    {
                        "candidate_set_id": candidate_set_id,
                        "error": (
                            "DUPLICATE_METRIC_ACROSS_RETRY_ATTEMPTS: a retry must not re-emit "
                            "logical Metric IDs already preserved from a valid independent set; "
                            f"overlapping_ids={overlapping_ids}, prior_sets="
                            f"{sorted({preserved_asset_owners[item] for item in overlapping_ids})}"
                        ),
                    }
                )
            for candidate in set_candidates:
                metric_id = str(candidate.get("id") or "")
                name = str(candidate.get("name") or "").strip()
                if metric_id in prior_case_metric_ids:
                    set_errors.append(
                        {
                            "candidate_set_id": candidate_set_id,
                            "metric_id": metric_id,
                            "error": (
                                "SECOND_PASS_REPEATED_METRIC: omission self-check must not repeat "
                                "or revise a first-pass Metric"
                            ),
                        }
                    )
                if _is_generic_metric_name(name):
                    set_errors.append(
                        {
                            "candidate_set_id": candidate_set_id,
                            "metric_id": metric_id,
                            "error": (
                                "GENERIC_METRIC_NAME: a Metric name must include the measured "
                                f"subject, not only a calculation-shape word; name={name!r}"
                            ),
                        }
                    )
                if metric_id in root_ids and name not in human_payload["question"]:
                    set_errors.append(
                        {
                            "candidate_set_id": candidate_set_id,
                            "metric_id": metric_id,
                            "error": (
                                "MISSING_ROOT_METRIC_NAME_EVIDENCE: a root Metric name must be an "
                                "exact Question span; a Gold alias cannot replace an explicit "
                                "Question business term"
                            ),
                        }
                    )
                if (
                    metric_id not in root_ids
                    and name not in human_payload["question"]
                    and name not in gold_output_aliases
                ):
                    set_errors.append(
                        {
                            "candidate_set_id": candidate_set_id,
                            "metric_id": metric_id,
                            "error": (
                                "MISSING_DEPENDENCY_METRIC_NAME_EVIDENCE: a dependency Metric "
                                "name must be an exact Question span or an exact trustworthy final "
                                "Gold SQL alias; keep an unnamed intermediate inside its "
                                "containing Metric"
                            ),
                        }
                    )
            for candidate in set_candidates:
                metric_id = str(candidate.get("id") or "")
                try:
                    _validate_raw_metric_contract(
                        candidate,
                        columns_by_table=columns_by_table,
                    )
                    typed_assets.append(_raw_metric_asset(candidate))
                except ValueError as exc:
                    set_errors.append({"metric_id": metric_id, "error": str(exc)})
            diagnostics = metric_dependency_diagnostics(visible_metrics, typed_assets)
            set_errors.extend(diagnostic.as_feedback() for diagnostic in diagnostics)
            if set_errors:
                retryable_errors.extend(set_errors)
            else:
                preserved_accepted[candidate_set_id] = set_candidates
        generation_attempts.append(
            {
                "attempt": generation_attempt,
                "candidate_ids": [candidate.get("id") for candidate in candidates],
                "retryable_errors": retryable_errors,
                "trace": attempt_trace,
            }
        )
        if not retryable_errors:
            final_accepted = dict(preserved_accepted)
            break
        if generation_attempt == 2:
            final_accepted = dict(preserved_accepted)
            if retryable_errors and not final_accepted:
                terminal_contract_errors = list(retryable_errors)
            break
        extraction_payload = {
            **human_payload,
            "validation_feedback": {
                "rejected_metrics": retryable_errors,
                "preserved_accepted_asset_ids": sorted(
                    {
                        str(candidate.get("id") or "")
                        for accepted_candidates in preserved_accepted.values()
                        for candidate in accepted_candidates
                    }
                ),
                "instruction": (
                    "Regenerate only the rejected Metric candidate sets. Put each independent "
                    "Question-facing root in its own set; combine roots only through a shared new "
                    "Metric dependency. Reconsider every independently named quantity in the "
                    "Question and do not omit an unaffected root. Correct every rejected "
                    "Pattern/Plan issue, use fully qualified <table.column> field tokens, and "
                    "keep result selection, ranking, ordering, limits, and display behavior out "
                    "of every Metric plan. "
                    "when dependency feedback reports references unused by the Metric's own "
                    "expression, remove those exact references. A parent plan naming a "
                    "child Metric's source table does not directly use, and therefore must not "
                    "inherit, that child Metric's physical columns. "
                    "when scope review identifies query-instance time, entity, or report "
                    "qualifiers, remove those qualifiers from the Metric description, Pattern, "
                    "application, and steps while preserving the scope-free calculation. "
                    "reconsider every rejected business measure named by the Question or Gold "
                    "SQL. A root Metric name must be an exact Question span. A dependency Metric "
                    "name may instead use an exact trustworthy final Gold alias. Omit any Metric "
                    "without the required naming evidence; never retry an invented contraction. "
                    "Do not re-emit "
                    "any logical Metric ID listed in "
                    "preserved_accepted_asset_ids; those independent candidate sets are already "
                    "valid and will be retained by the Extractor."
                ),
            },
        }
    if trace_capture is not None:
        _record_metric_extraction_trace(trace_capture, generation_attempts)
    if terminal_contract_errors:
        if trace_capture is not None:
            trace_capture["discarded_metric_candidate_errors"] = terminal_contract_errors
    final_candidates = [
        candidate for candidates in final_accepted.values() for candidate in candidates
    ]
    return final_candidates


def _record_metric_extraction_trace(
    trace_capture: dict[str, Any] | None,
    generation_attempts: list[dict[str, Any]],
) -> None:
    if trace_capture is None:
        return
    trace_capture["metric_generation_attempts"] = generation_attempts
    if not generation_attempts:
        return
    latest_trace = generation_attempts[-1].get("trace")
    if not isinstance(latest_trace, dict):
        return
    for key in ("prompt", "response"):
        if key in latest_trace:
            trace_capture[key] = latest_trace[key]


def _gold_output_aliases(sql: str) -> set[str]:
    try:
        tree = sqlglot.parse_one(sql)
    except Exception:  # noqa: BLE001 - invalid SQL yields no naming evidence
        return set()
    select = tree if isinstance(tree, expressions.Select) else tree.find(expressions.Select)
    if not isinstance(select, expressions.Select):
        return set()
    return {
        alias
        for projection in select.expressions
        if (alias := str(projection.alias or "").strip())
    }


def _raw_metric_asset(candidate: dict[str, Any]) -> NodeAsset:
    payload = {
        key: value for key, value in candidate.items() if not key.startswith("_")
    }
    normalized, _ = normalize_properties_payload(payload, property_keys=_METRIC_BIZ_KEYS)
    return NodeAsset.model_validate(normalized)


def _metric_prompt_projection(asset: NodeAsset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "name": asset.name,
        "description": asset.description,
        "references": list(asset.properties.references),
        "expression": asset.properties.expression.model_dump(mode="json"),
        "pattern": asset.properties.pattern,
    }


def _validate_raw_metric_contract(
    candidate: dict[str, Any],
    *,
    columns_by_table: dict[str, set[str]] | None = None,
) -> None:
    if candidate.get("aliases"):
        raise ValueError("Initial Metric CREATE aliases must be empty")
    asset = _raw_metric_asset(candidate)
    validate_metric_contract(
        asset,
        require_generation_plan=True,
        require_pattern=True,
    )
    if columns_by_table:
        _validate_known_schema_tokens(asset, columns_by_table=columns_by_table)


def _metric_construction_retry_instruction(error: str) -> str:
    instruction = (
        "The previous result is structurally invalid. Regenerate the complete Metric candidate "
        "set. Preserve every valid root Metric from the previous result; do not fix a "
        "candidate-set structure error by omitting a Metric. Every asset must contain exactly "
        "name, description, pattern, expression, and sql_generation_plan. Code derives asset IDs, "
        "aliases, and references. Do not emit those derived fields or any lifecycle field."
    )
    if "duplicate Metric asset ID across candidate sets" in error:
        instruction += (
            " The same Metric cannot be copied into multiple candidate sets. When independently "
            "requested Metrics are dependency-linked and therefore share that Metric, consolidate "
            "the connected roots and their dependency closure into one atomic candidate set."
        )
    if "instead of ontology references" in error:
        instruction += (
            " In Pattern and plan prose, replace every ontology spelling column.T.C with the "
            "physical token <T.C>, and table.T with <T>. Ontology IDs belong only in "
            "expression.refs."
        )
    if "Unknown NODE ID type" in error:
        instruction += (
            " Copy every expression reference exactly from physical_reference_catalog in "
            "column.<table>.<column> order. Never reverse it to <column>.<table>."
        )
    if "unresolved bare schema tokens" in error:
        instruction += (
            " Apply every reported field_path violation. Replace every <column> occurrence with "
            "one evidence-backed <table.column>; a nearby <table> does not qualify it. When a "
            "step uses Columns from different Tables, name each side separately."
        )
    if "complete SQL" in error:
        instruction += (
            " Rewrite plan steps as short natural-language instructions. Use verbs such as read, "
            "aggregate, divide, and combine; do not write a SELECT ... FROM statement."
        )
    if "GENERIC_METRIC_NAME" in error:
        instruction += (
            " Use the complete exact Question span that includes the measured subject, or omit "
            "the candidate when no such self-contained span exists."
        )
    return instruction


def _is_generic_metric_name(name: str) -> bool:
    compact = re.sub(r"[\W_]+", "", name, flags=re.UNICODE).casefold()
    return compact in {
        "average",
        "avg",
        "growth",
        "growthrate",
        "percentage",
        "rate",
        "ratio",
        "share",
        "同比增速",
        "同比增长率",
        "占比",
        "平均值",
    }


def _payload_candidate_sets(payload: Any) -> list[MetricCandidateSetPayload]:
    if isinstance(payload, MetricExtractionOutput):
        return list(payload.candidate_sets)
    raw_sets = payload.get("candidate_sets", []) if isinstance(payload, dict) else []
    if not isinstance(raw_sets, list):
        raw_sets = [raw_sets]
    validated: list[MetricCandidateSetPayload] = []
    for item in raw_sets:
        if not isinstance(item, dict):
            continue
        for asset in item.get("assets", []) if isinstance(item.get("assets"), list) else []:
            if isinstance(asset, dict):
                _normalize_ref_keys(asset)
        validated.append(MetricCandidateSetPayload.model_validate(item))
    return validated


def _flatten_candidate_sets(
    payload: Any,
    *,
    column_refs: dict[str, str],
    columns_by_table: dict[str, set[str]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_set_ids: set[str] = set()
    asset_owner: dict[str, str] = {}
    for candidate_set in _payload_candidate_sets(payload):
        if candidate_set.candidate_id in seen_set_ids:
            raise ValueError(f"duplicate Metric candidate set ID: {candidate_set.candidate_id}")
        seen_set_ids.add(candidate_set.candidate_id)
        built_candidates = [
            _metric_candidate_to_raw_candidate(
                item,
                column_refs=column_refs,
                columns_by_table=columns_by_table,
            )
            for item in candidate_set.assets
        ]
        _canonicalize_local_metric_references(built_candidates)
        set_asset_ids: set[str] = set()
        for candidate in built_candidates:
            asset_id = str(candidate["id"])
            if asset_id in set_asset_ids:
                raise ValueError(
                    f"duplicate Metric asset ID in candidate set: {asset_id}"
                )
            set_asset_ids.add(asset_id)
            previous_owner = asset_owner.setdefault(asset_id, candidate_set.candidate_id)
            if previous_owner != candidate_set.candidate_id:
                raise ValueError(
                    "duplicate Metric asset ID across candidate sets: "
                    f"{asset_id} ({previous_owner}, {candidate_set.candidate_id})"
                )
        components = _raw_metric_connected_components(built_candidates)
        for component_index, component in enumerate(components, start=1):
            component_id = candidate_set.candidate_id
            if len(components) > 1:
                first_id = str(component[0].get("id") or component_index)
                component_id = f"{candidate_set.candidate_id}::component::{first_id}"
            for candidate in component:
                candidate["_candidate_set_id"] = component_id
                result.append(candidate)
    return result


def _raw_metric_connected_components(
    candidates: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split an LLM set into its exact formula-dependency components."""

    by_id = {str(candidate.get("id") or ""): candidate for candidate in candidates}
    adjacency = {asset_id: set() for asset_id in by_id}
    for asset_id, candidate in by_id.items():
        for dependency_id in _raw_metric_reference_ids(candidate):
            if dependency_id not in by_id:
                continue
            adjacency[asset_id].add(dependency_id)
            adjacency[dependency_id].add(asset_id)

    result: list[list[dict[str, Any]]] = []
    remaining = set(by_id)
    for candidate in candidates:
        start = str(candidate.get("id") or "")
        if start not in remaining:
            continue
        stack = [start]
        component_ids: set[str] = set()
        while stack:
            current = stack.pop()
            if current not in remaining:
                continue
            remaining.remove(current)
            component_ids.add(current)
            stack.extend(adjacency[current] & remaining)
        result.append(
            [
                member
                for member in candidates
                if str(member.get("id") or "") in component_ids
            ]
        )
    return result


def _normalize_ref_keys(candidate: dict[str, Any]) -> None:
    expression = candidate.get("expression")
    if not isinstance(expression, dict) or not isinstance(expression.get("refs"), dict):
        return
    refs = expression["refs"]
    normalized: dict[str, Any] = {}
    for key, value in refs.items():
        name = str(key).strip()
        if name.startswith("${") and name.endswith("}"):
            name = name[2:-1].strip()
        if name in normalized:
            raise ValueError(f"duplicate normalized Metric refs key: {name}")
        normalized[name] = value
    expression["refs"] = normalized


def _raw_metric_set_root_ids(candidates: list[dict[str, Any]]) -> tuple[str, ...]:
    member_ids = {str(candidate.get("id") or "") for candidate in candidates}
    referenced_members = {
        reference
        for candidate in candidates
        for reference in _raw_metric_reference_ids(candidate)
        if reference in member_ids
    }
    return tuple(
        str(candidate.get("id") or "")
        for candidate in candidates
        if str(candidate.get("id") or "") not in referenced_members
    )


def _raw_metric_reference_ids(candidate: dict[str, Any]) -> set[str]:
    source = candidate.get("properties")
    if not isinstance(source, dict):
        source = candidate
    expression = source.get("expression")
    expression_refs = expression.get("refs") if isinstance(expression, dict) else {}
    values = [
        *(source.get("references") or []),
        *(expression_refs.values() if isinstance(expression_refs, dict) else []),
    ]
    return {
        str(value).strip()
        for value in values
        if str(value).strip().startswith("metric.")
    }


def _metric_candidate_to_raw_candidate(
    item: MetricCandidatePayload,
    *,
    column_refs: dict[str, str] | None = None,
    columns_by_table: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    name = item.name.strip()
    expression = item.expression.model_dump(mode="json")
    pattern = item.pattern
    if column_refs or columns_by_table:
        expression["refs"] = {
            key: _normalize_column_ref(
                value,
                column_refs,
                columns_by_table=columns_by_table,
            )
            for key, value in expression.get("refs", {}).items()
        }
        pattern = _normalize_angle_column_tokens(
            pattern,
            column_refs,
            columns_by_table=columns_by_table,
        )
    sql_generation_plan = item.sql_generation_plan.model_dump(
        mode="json",
        exclude_none=True,
    )
    if column_refs or columns_by_table:
        plan_column_refs = {
            **column_refs,
            **_expression_column_ref_index(expression),
        }
        sql_generation_plan = _normalize_plan_schema_tokens(
            sql_generation_plan,
            plan_column_refs,
            columns_by_table=columns_by_table,
        )
    references = [*expression.get("refs", {}).values()]
    references = list(dict.fromkeys(str(reference) for reference in references))
    return {
        "node_type": NodeType.METRIC.value,
        "id": make_node_id(NodeType.METRIC, name),
        "name": name,
        "aliases": [],
        "description": item.description,
        "tags": [],
        "references": references,
        "pattern": pattern,
        "expression": expression,
        "sql_generation_plans": [sql_generation_plan],
    }


def _canonicalize_local_metric_references(candidates: list[dict[str, Any]]) -> None:
    """Resolve an emitted Metric's local plan ID to its deterministic asset ID."""

    aliases: dict[str, list[str]] = {}
    for candidate in candidates:
        plans = candidate.get("sql_generation_plans") or []
        if not plans or not isinstance(plans[0], dict):
            continue
        plan_id = str(plans[0].get("id") or "").strip()
        if plan_id:
            aliases.setdefault(f"metric.{plan_id}", []).append(str(candidate["id"]))
    replacements = {
        alias: asset_ids[0]
        for alias, asset_ids in aliases.items()
        if len(set(asset_ids)) == 1
    }
    plan_metric_names = {
        **{
            str(candidate["id"]): str(candidate["name"])
            for candidate in candidates
        },
        **{
            alias: asset_id.removeprefix("metric.")
            for alias, asset_id in replacements.items()
        },
    }
    for candidate in candidates:
        expression = candidate.get("expression")
        refs = expression.get("refs") if isinstance(expression, dict) else None
        if not isinstance(refs, dict):
            continue
        expression["refs"] = {
            key: replacements.get(str(value), value)
            for key, value in refs.items()
        }
        candidate["references"] = list(dict.fromkeys(expression["refs"].values()))
        plans = candidate.get("sql_generation_plans") or []
        for plan in plans:
            _replace_plan_metric_ids_with_names(plan, plan_metric_names)
        candidate["references"] = list(dict.fromkeys(expression["refs"].values()))


def _replace_plan_metric_ids_with_names(
    plan: dict[str, Any],
    metric_names: dict[str, str],
) -> None:
    def replace(text: str) -> str:
        for reference, name in sorted(
            metric_names.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            text = text.replace(reference, name)
        return text

    plan["application"] = replace(str(plan.get("application") or ""))
    plan["steps"] = [replace(str(step)) for step in plan.get("steps", [])]


def _normalize_column_ref(
    value: Any,
    column_refs: dict[str, str],
    *,
    columns_by_table: dict[str, set[str]] | None = None,
) -> str:
    text = str(value).strip()
    if text.startswith(("column.", "metric.")):
        return text
    if "." not in text:
        return column_refs.get(text, text)
    left, right = text.split(".", 1)
    known = columns_by_table or {}
    if left in known and right in known[left]:
        return f"column.{left}.{right}"
    if right in known and left in known[right]:
        return f"column.{right}.{left}"
    return text


def _expression_column_ref_index(expression: dict[str, Any]) -> dict[str, str]:
    candidates: dict[str, list[str]] = {}
    refs = expression.get("refs")
    for value in refs.values() if isinstance(refs, dict) else []:
        reference = str(value)
        if not reference.startswith("column."):
            continue
        column = reference.split(".", 2)[-1]
        candidates.setdefault(column, []).append(reference)
    return {
        column: references[0]
        for column, references in candidates.items()
        if len(set(references)) == 1
    }


def _column_ref_index(
    *,
    sql: str,
    database_context: Any,
) -> dict[str, str]:
    if not isinstance(database_context, dict):
        return {}
    columns_by_table = _columns_by_table(sql=sql, database_context=database_context)

    refs: dict[str, list[str]] = {}
    for table in columns_by_table:
        for column in columns_by_table.get(table, set()):
            refs.setdefault(column, []).append(make_node_id(NodeType.COLUMN, table, column))
    return {column: candidates[0] for column, candidates in refs.items() if len(candidates) == 1}


def _normalize_plan_schema_tokens(
    plan: dict[str, Any],
    column_refs: dict[str, str],
    *,
    columns_by_table: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    normalized = dict(plan)
    if isinstance(normalized.get("application"), str):
        normalized["application"] = _normalize_angle_column_tokens(
            normalized["application"],
            column_refs,
            columns_by_table=columns_by_table,
        )
    normalized["steps"] = [
        _normalize_angle_column_tokens(
            str(step),
            column_refs,
            columns_by_table=columns_by_table,
        )
        for step in ensure_list(normalized.get("steps"))
    ]
    return normalized


def _normalize_angle_column_tokens(
    text: str,
    column_refs: dict[str, str],
    *,
    columns_by_table: dict[str, set[str]] | None = None,
) -> str:
    known_columns = columns_by_table or {}
    text = re.sub(r"<column\.([^.<>]+)\.([^<>]+)>", r"<\1.\2>", text)
    text = re.sub(r"<table\.([^<>]+)>", r"<\1>", text)
    text = re.sub(
        r"(?<![<A-Za-z0-9_])column\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)",
        r"<\1.\2>",
        text,
    )
    text = re.sub(
        r"(?<![<A-Za-z0-9_])table\.([A-Za-z0-9_]+)",
        r"<\1>",
        text,
    )
    text = _wrap_qualified_column_mentions(text, known_columns)
    text = _wrap_table_mentions(text, known_columns)
    mentioned_tables = {part.split(".", 1)[0] for part in _angle_tokens(text) if "." in part}
    mentioned_tables.update(token for token in _angle_tokens(text) if token in known_columns)
    if len(mentioned_tables) == 1:
        text = _wrap_contextual_column_mentions(
            text,
            table=next(iter(mentioned_tables)),
            columns_by_table=known_columns,
        )
    text = _wrap_referenced_column_mentions(text, column_refs)

    def replace(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        if "." in token:
            return match.group(0)
        ref = column_refs.get(token)
        if ref and ref.startswith("column."):
            _, table, column = ref.split(".", 2)
            return f"<{table}.{column}>"
        all_candidate_tables = [
            table for table, columns in known_columns.items() if token in columns
        ]
        if len(all_candidate_tables) != 1:
            return match.group(0)
        contextual_tables = [
            table for table in mentioned_tables if token in known_columns.get(table, set())
        ]
        if len(contextual_tables) == 1:
            return f"<{contextual_tables[0]}.{token}>"
        return match.group(0)

    return re.sub(r"<([^<>]+)>", replace, text)


def _wrap_referenced_column_mentions(text: str, column_refs: dict[str, str]) -> str:
    """Qualify bare physical operands when one candidate reference resolves them exactly."""

    ordered_refs = sorted(
        column_refs.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for column, reference in ordered_refs:
        if not reference.startswith("column."):
            continue
        _, table, physical_column = reference.split(".", 2)
        if column != physical_column:
            continue
        text = re.sub(
            rf"(?<![<.A-Za-z0-9_]){re.escape(column)}(?![>.A-Za-z0-9_])",
            f"<{table}.{physical_column}>",
            text,
        )
    return text


def _wrap_qualified_column_mentions(
    text: str,
    columns_by_table: dict[str, set[str]],
) -> str:
    """Canonicalize unambiguous physical table.column prose into plan tokens."""

    qualified = sorted(
        (
            f"{table}.{column}"
            for table, columns in columns_by_table.items()
            for column in columns
        ),
        key=len,
        reverse=True,
    )
    for token in qualified:
        text = re.sub(
            rf"(?<![<A-Za-z0-9_]){re.escape(token)}(?![>A-Za-z0-9_])",
            f"<{token}>",
            text,
        )
    return text


def _wrap_table_mentions(
    text: str,
    columns_by_table: dict[str, set[str]],
) -> str:
    for table in sorted(columns_by_table, key=len, reverse=True):
        text = re.sub(
            rf"(?<![<.A-Za-z0-9_]){re.escape(table)}(?![>.A-Za-z0-9_])",
            f"<{table}>",
            text,
        )
    return text


def _wrap_contextual_column_mentions(
    text: str,
    *,
    table: str,
    columns_by_table: dict[str, set[str]],
) -> str:
    for column in sorted(columns_by_table.get(table, set()), key=len, reverse=True):
        matching_tables = [
            candidate_table
            for candidate_table, columns in columns_by_table.items()
            if column in columns
        ]
        if len(matching_tables) != 1:
            continue
        text = re.sub(
            rf"(?<![<.A-Za-z0-9_]){re.escape(column)}(?![>.A-Za-z0-9_])",
            f"<{table}.{column}>",
            text,
        )
    return text


def _angle_tokens(text: str) -> list[str]:
    return [token.strip() for token in re.findall(r"<([^<>]+)>", text)]


def _columns_by_table(
    *,
    sql: str,
    database_context: Any,
) -> dict[str, set[str]]:
    if not isinstance(database_context, dict):
        return {}
    all_columns = {
        str(table): _schema_column_names(str(schema)) for table, schema in database_context.items()
    }
    sql_tables = _sql_table_names(sql)
    preferred = {table: columns for table, columns in all_columns.items() if table in sql_tables}
    return preferred or all_columns


def _validate_known_schema_tokens(
    asset: NodeAsset,
    *,
    columns_by_table: dict[str, set[str]],
) -> None:
    schema_tables = set(columns_by_table)
    located_texts = [("pattern", asset.properties.pattern)]
    for plan_index, plan in enumerate(asset.properties.sql_generation_plans):
        prefix = f"sql_generation_plans[{plan_index}]"
        located_texts.append((f"{prefix}.application", plan.application))
        located_texts.extend(
            (f"{prefix}.steps[{step_index}]", step)
            for step_index, step in enumerate(plan.steps)
        )
    violations: list[dict[str, Any]] = []
    for field_path, value in located_texts:
        for token in _angle_tokens(value):
            if "." in token or token in schema_tables:
                continue
            qualified_candidates = [
                f"<{table}.{token}>"
                for table, columns in sorted(columns_by_table.items())
                if token in columns
            ]
            violations.append(
                {
                    "field_path": field_path,
                    "invalid_token": f"<{token}>",
                    "qualified_candidates": qualified_candidates,
                }
            )
    if violations:
        raise ValueError(
            "Metric Pattern/Generation Plan contains unresolved bare schema tokens; "
            "every physical Column occurrence must be self-contained as <table.column>; "
            "qualified candidates are evidence-backed format options, not an automatic source "
            "selection; violations="
            f"{json.dumps(violations, ensure_ascii=False, sort_keys=True)}"
        )


def _schema_column_names(schema: str) -> set[str]:
    columns: set[str] = set()
    for line in schema.splitlines():
        match = re.match(r"\s*-\s*([^\n:]+?)\s*:", line)
        if match:
            columns.add(match.group(1))
    return columns


def _sql_table_names(sql: str) -> set[str]:
    return {
        match.group(1)
        for match in re.finditer(
            r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
            sql,
            flags=re.IGNORECASE,
        )
    }


class MetricExtractor(TypedCandidateExtractor):
    asset_type = NodeType.METRIC

    def extract(
        self,
        raw_assets: list[dict[str, Any]],
        *,
        source: CandidateSource,
        skipped: list[dict[str, Any]],
    ) -> list[NodeAsset]:
        assets = super().extract(raw_assets, source=source, skipped=skipped)
        valid: list[NodeAsset] = []
        for asset in assets:
            try:
                validate_metric_contract(
                    asset,
                    require_generation_plan=True,
                    require_pattern=True,
                )
            except ValueError as exc:
                skipped.append(
                    {
                        "code": f"INVALID_{source.error_prefix}_METRIC_CONTRACT",
                        "record": source.source_id,
                        "asset_id": asset.id,
                        "asset_type": asset.node_type.value,
                        "error": str(exc),
                    }
                )
                continue
            valid.append(asset)
        return valid

    def _normalize(self, raw_asset: dict[str, Any], source: CandidateSource) -> dict[str, Any]:
        normalized, properties = normalize_properties_payload(
            raw_asset, property_keys=_METRIC_BIZ_KEYS
        )
        normalized["node_type"] = NodeType.METRIC

        name = str(normalized.get("name") or "").strip()
        if name and not normalized.get("id"):
            normalized["id"] = make_node_id(NodeType.METRIC, name)

        normalized["aliases"] = [str(item) for item in ensure_list(normalized.get("aliases"))]
        normalized["tags"] = []

        expression = properties.get("expression")
        if isinstance(expression, str):
            properties["expression"] = {"expr": expression, "refs": {}}
        elif isinstance(expression, dict):
            expression.setdefault("refs", {})
            properties["expression"] = expression
        expression_refs = properties.get("expression", {}).get("refs", {})
        properties["references"] = list(
            dict.fromkeys(
                str(reference)
                for reference in (
                    expression_refs.values() if isinstance(expression_refs, dict) else []
                )
            )
        )

        normalized["properties"] = properties
        return super()._normalize(normalized, source)
