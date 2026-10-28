from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from cortex.capabilities.sql_training.error_analysis.models import (
    ExecutionFailureAnalysis,
)
from cortex.capabilities.sql_training.llm.parsing import run_llm_extraction
from cortex.capabilities.sql_training.llm.prompts import load_prompt


class ExecutionFailureAnalysisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: ExecutionFailureAnalysis


def _dump_execution_failure_result(parsed: Any) -> dict[str, Any]:
    if isinstance(parsed, ExecutionFailureAnalysisOutput):
        return parsed.result.model_dump(mode="json")
    if isinstance(parsed, dict):
        if "result" in parsed:
            parsed = ExecutionFailureAnalysisOutput.model_validate(parsed).result
        else:
            parsed = ExecutionFailureAnalysis.model_validate(parsed)
        return parsed.model_dump(mode="json")
    return ExecutionFailureAnalysisOutput.model_validate(parsed).result.model_dump(
        mode="json"
    )


def extract_execution_failure_analysis(
    llm: object,
    record: dict[str, Any],
    prompt: str,
    *,
    trace_capture: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return run_llm_extraction(
        llm=llm,
        prompt=prompt,
        output_model=ExecutionFailureAnalysisOutput,
        human_payload=record,
        human_content=format_execution_failure_human_payload(record),
        map_result=lambda parsed: [_dump_execution_failure_result(parsed)],
        trace_capture=trace_capture,
    )


def analyze_execution_failure(
    provider: Any,
    *,
    payload: dict[str, Any],
    trace_capture: dict[str, Any] | None = None,
    max_attempts: int = 2,
) -> ExecutionFailureAnalysis:
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        request = dict(payload)
        if errors:
            request["validation_feedback"] = {
                "error": errors[-1],
                "instruction": (
                    "Return at least one confirmed error and cover it with Repair Groups. "
                    "Use concise evidence from the supplied SQL and database error."
                ),
            }
        local_trace: dict[str, Any] = {}
        try:
            rows = provider.extract_assets(
                request,
                context={
                    "prompt_name": "error_analysis_execution_failure",
                    "prompt": load_prompt("error_analysis_execution_failure"),
                    "trace_capture": local_trace,
                },
            )
            row = rows[0] if rows and isinstance(rows[0], dict) else {}
            row = _normalize_singleton_repair_group_fields(row)
            result = ExecutionFailureAnalysis.model_validate(row)
            _validate_execution_failure_bases(result, request.get("accepted_working_assets", []))
            if trace_capture is not None:
                trace_capture.update(local_trace)
                trace_capture["attempt"] = attempt
            return result
        except ValueError as exc:
            errors.append(str(exc))
            if trace_capture is not None:
                trace_capture.update(local_trace)
    raise ValueError(
        f"execution failure analysis failed after {max_attempts} attempts: {errors[-1]}"
    )


def _normalize_singleton_repair_group_fields(row: dict[str, Any]) -> dict[str, Any]:
    groups = row.get("repair_groups")
    if not isinstance(groups, list):
        return row
    normalized = dict(row)
    normalized_groups: list[Any] = []
    changed = False
    for group in groups:
        if not isinstance(group, dict):
            normalized_groups.append(group)
            continue
        error_ids = group.get("error_ids")
        if not isinstance(error_ids, list) or len(error_ids) != 1:
            normalized_groups.append(group)
            continue
        clean = dict(group)
        if clean.pop("grouping_evidence", None) is not None:
            changed = True
        if clean.get("grouping_reason") not in (None, ""):
            changed = True
        clean["grouping_reason"] = ""
        normalized_groups.append(clean)
    if not changed:
        return row
    normalized["repair_groups"] = normalized_groups
    return normalized


def format_execution_failure_human_payload(payload: dict[str, Any]) -> str:
    """Keep all case-specific evidence in the trailing Human message."""

    context = {
        "execution_error": payload.get("execution_error", ""),
        "database_dialect": payload.get("database_dialect", ""),
        "table_schema": _sorted_mapping(payload.get("table_schema", {})),
        "matched_ontology": payload.get("matched_ontology", {}),
        "draft_metrics": payload.get("draft_metrics", []),
        "evaluation_contract": payload.get("evaluation_contract", {}),
        "accepted_working_assets": _sorted_assets(payload.get("accepted_working_assets", [])),
    }
    sections = [
        f"# Question\n{payload.get('question', '')}",
        f"# Gold SQL\n```sql\n{payload.get('gold_sql', '')}\n```",
        f"# Predicted SQL\n```sql\n{payload.get('predicted_sql', '')}\n```",
        "# Execution Context\n```json\n"
        + json.dumps(context, ensure_ascii=False, indent=2, default=str)
        + "\n```",
    ]
    if payload.get("validation_feedback"):
        sections.append(
            "# Validation Feedback\n```json\n"
            + json.dumps(payload["validation_feedback"], ensure_ascii=False, indent=2, default=str)
            + "\n```"
        )
    return "\n\n".join(sections)


def _sorted_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): value[key] for key in sorted(value)}


def _sorted_assets(value: Any) -> list[dict[str, Any]]:
    assets = (
        [dict(item) for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )
    return sorted(
        assets,
        key=lambda item: (str(item.get("node_type") or ""), str(item.get("id") or "")),
    )


def _validate_execution_failure_bases(
    result: ExecutionFailureAnalysis,
    working_assets: Any,
) -> None:
    assets_by_id = {
        str(asset.get("id")): asset
        for asset in _sorted_assets(working_assets)
        if str(asset.get("id") or "").strip()
    }
    for group in result.repair_groups:
        if group.base_asset_id is None:
            continue
        asset = assets_by_id.get(group.base_asset_id)
        if asset is None:
            raise ValueError(
                f"repair group {group.id} base_asset_id is not an accepted Working asset"
            )
        expected_type = "concept" if group.owner.value == "semantic_mapping" else group.owner.value
        if str(asset.get("node_type") or "") != expected_type:
            raise ValueError(f"repair group {group.id} base_asset_id type does not match owner")
