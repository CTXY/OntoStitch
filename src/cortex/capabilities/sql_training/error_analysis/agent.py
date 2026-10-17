from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from cortex.capabilities.sql_training.error_analysis.models import (
    RepairAblationProposal,
    RepairAction,
    RepairReview,
)
from cortex.capabilities.sql_training.llm.parsing import run_llm_extraction
from cortex.capabilities.sql_training.llm.prompts import load_prompt


class ErrorAnalysisMinimalRepairActionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: RepairAction = Field(
        description=(
            "Exactly one repair-or-finish action. Do not wrap this field in another "
            "object named result."
        )
    )


class ErrorAnalysisMinimalRepairAblationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: RepairAblationProposal


class ErrorAnalysisMinimalRepairReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: RepairReview


class ErrorAnalysisProvider(Protocol):
    def extract_assets(
        self,
        record: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[dict]:
        """Return raw LLM extraction dictionaries for error analysis."""


def extract_error_analysis_minimal_repair_action(
    provider: ErrorAnalysisProvider,
    payload: dict[str, Any],
) -> RepairAction:
    result = provider.extract_assets(
        payload,
        context={
            "prompt_name": "error_analysis_minimal_repair_action",
            "prompt": load_prompt("error_analysis_minimal_repair_action"),
        },
    )
    if result and isinstance(result[0], dict):
        return RepairAction.model_validate(result[0])
    raise ValueError("error analysis minimal repair action returned no action")


def extract_error_analysis_minimal_repair_ablation(
    provider: ErrorAnalysisProvider,
    payload: dict[str, Any],
) -> RepairAblationProposal:
    result = provider.extract_assets(
        payload,
        context={
            "prompt_name": "error_analysis_minimal_repair_ablation",
            "prompt": load_prompt("error_analysis_minimal_repair_ablation"),
        },
    )
    if result and isinstance(result[0], dict):
        return RepairAblationProposal.model_validate(result[0])
    raise ValueError("error analysis minimal repair ablation returned no proposal")


def extract_error_analysis_minimal_repair_joint_ablation(
    provider: ErrorAnalysisProvider,
    payload: dict[str, Any],
) -> RepairAblationProposal:
    result = provider.extract_assets(
        payload,
        context={
            "prompt_name": "error_analysis_minimal_repair_joint_ablation",
            "prompt": load_prompt("error_analysis_minimal_repair_joint_ablation"),
        },
    )
    if result and isinstance(result[0], dict):
        return RepairAblationProposal.model_validate(result[0])
    raise ValueError("error analysis minimal repair joint ablation returned no proposal")


def extract_error_analysis_minimal_repair_review(
    provider: ErrorAnalysisProvider,
    payload: dict[str, Any],
) -> RepairReview:
    prompt = _minimal_repair_review_prompt(payload)
    result = provider.extract_assets(
        payload,
        context={
            "prompt_name": "error_analysis_minimal_repair_review",
            "prompt": prompt,
        },
    )
    if result and isinstance(result[0], dict):
        return RepairReview.model_validate(result[0])
    raise ValueError("error analysis minimal repair review returned no review")


def _minimal_repair_review_prompt(payload: dict[str, Any]) -> str:
    del payload
    return load_prompt("error_analysis_minimal_repair_review")


def extract_error_analysis_llm_payloads(
    llm: Any,
    record: dict[str, Any],
    prompt: str,
    prompt_name: str,
    trace_capture: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    output_model: type[BaseModel]
    if prompt_name == "error_analysis_minimal_repair_action":
        output_model = ErrorAnalysisMinimalRepairActionOutput
    elif prompt_name == "error_analysis_minimal_repair_ablation":
        output_model = ErrorAnalysisMinimalRepairAblationOutput
    elif prompt_name == "error_analysis_minimal_repair_joint_ablation":
        output_model = ErrorAnalysisMinimalRepairAblationOutput
    elif prompt_name == "error_analysis_minimal_repair_review":
        output_model = ErrorAnalysisMinimalRepairReviewOutput
    else:
        raise ValueError(f"Unsupported error analysis prompt: {prompt_name}")
    return run_llm_extraction(
        llm=llm,
        prompt=prompt,
        output_model=output_model,
        human_payload=record,
        human_content=format_error_analysis_human_payload(prompt_name, record),
        map_result=lambda parsed: _payload_result(parsed, output_model),
        trace_capture=trace_capture,
    )


def format_error_analysis_human_payload(prompt_name: str, payload: dict[str, Any]) -> str:
    if prompt_name == "error_analysis_minimal_repair_action":
        return _format_minimal_repair_action_payload(payload)
    if prompt_name == "error_analysis_minimal_repair_ablation":
        return _format_minimal_repair_ablation_payload(payload)
    if prompt_name == "error_analysis_minimal_repair_joint_ablation":
        return _format_minimal_repair_joint_ablation_payload(payload)
    if prompt_name == "error_analysis_minimal_repair_review":
        return _format_minimal_repair_review_payload(payload)
    return json.dumps(payload, ensure_ascii=False)


def _format_minimal_repair_action_payload(payload: dict[str, Any]) -> str:
    structured = {
        "final_comparison": payload.get("final_comparison"),
        "gold_result": payload.get("gold_result"),
        "current_result": payload.get("current_result"),
        "current_error": payload.get("current_error", ""),
        "repair_rounds": payload.get("repair_rounds", []),
    }
    sections = [
        _text_section("Question", payload.get("question", "")),
        _text_section("Optional Evidence", payload.get("evidence", "")),
        _code_section("Gold SQL", "sql", payload.get("gold_sql", "")),
        _json_section("Gold Column Metadata", payload.get("gold_column_metadata", {})),
        _code_section(
            "Original Incorrect SQL",
            "sql",
            payload.get("original_incorrect_sql", ""),
        ),
        _code_section("Current SQL", "sql", payload.get("current_sql", "")),
        _json_section("Repair Context", structured),
    ]
    if payload.get("validation_feedback"):
        sections.append(_json_section("Validation Feedback", payload["validation_feedback"]))
    return "\n\n".join(sections)


def _format_minimal_repair_ablation_payload(payload: dict[str, Any]) -> str:
    sections = [
        _text_section("Question", payload.get("question", "")),
        _code_section("Gold SQL", "sql", payload.get("gold_sql", "")),
        _code_section("Final SQL", "sql", payload.get("final_sql", "")),
        _json_section("Target Repair Round", payload.get("target_round", {})),
    ]
    if payload.get("ablation_feedback"):
        sections.append(_json_section("Ablation Feedback", payload["ablation_feedback"]))
    return "\n\n".join(sections)


def _format_minimal_repair_joint_ablation_payload(payload: dict[str, Any]) -> str:
    return "\n\n".join(
        [
            _text_section("Question", payload.get("question", "")),
            _code_section("Gold SQL", "sql", payload.get("gold_sql", "")),
            _code_section("Final SQL", "sql", payload.get("final_sql", "")),
            _json_section("Target Repair Rounds", payload.get("target_rounds", [])),
        ]
    )


def _format_minimal_repair_review_payload(payload: dict[str, Any]) -> str:
    raw_evaluation_contract = payload.get("evaluation_contract", {})
    evaluation_contract = (
        dict(raw_evaluation_contract)
        if isinstance(raw_evaluation_contract, dict)
        else {}
    )
    # The optional exclusions are rendered once, in the system prompt immediately
    # before the decision process. Keep the remaining evaluation contract as
    # context without duplicating those exclusions in a second location.
    evaluation_contract.pop("non_learnable_obligations", None)
    structured = {
        "gold_result": payload.get("gold_result"),
        "final_result": payload.get("final_result"),
        "final_error": payload.get("final_error", ""),
        "repair_rounds": payload.get("repair_rounds", []),
        "ablations": payload.get("ablations", []),
        "joint_ablations": payload.get("joint_ablations", []),
        "matched": payload.get("matched", False),
        "terminal_status": payload.get("terminal_status", ""),
        "evaluation_contract": evaluation_contract,
        "schema_context": payload.get("schema_context", {}),
        "accepted_working_assets": _canonical_working_assets(
            payload.get("accepted_working_assets", [])
        ),
    }
    sections = [
        _text_section("Question", payload.get("question", "")),
        _text_section("Optional Evidence", payload.get("evidence", "")),
        _code_section("Gold SQL", "sql", payload.get("gold_sql", "")),
        _json_section("Gold Column Metadata", payload.get("gold_column_metadata", {})),
        _code_section(
            "Original Incorrect SQL",
            "sql",
            payload.get("original_incorrect_sql", ""),
        ),
        _code_section("Final SQL", "sql", payload.get("final_sql", "")),
    ]
    sections.append(_json_section("Repair Context", structured))
    if payload.get("validation_feedback"):
        sections.append(_json_section("Validation Feedback", payload["validation_feedback"]))
    return "\n\n".join(sections)


def _canonical_working_assets(value: Any) -> list[dict[str, Any]]:
    assets = (
        [dict(item) for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )
    summaries = [_working_asset_review_view(asset) for asset in assets]
    return sorted(
        summaries,
        key=lambda item: (str(item.get("node_type") or ""), str(item.get("id") or "")),
    )


def _working_asset_review_view(asset: dict[str, Any]) -> dict[str, Any]:
    """Expose only owner-relevant fields from accepted Working to Repair Review."""

    properties = asset.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    view: dict[str, Any] = {
        "id": asset.get("id"),
        "node_type": asset.get("node_type"),
        "name": asset.get("name"),
    }
    node_type = str(asset.get("node_type") or "")
    if node_type == "concept":
        view["targets"] = properties.get("targets", asset.get("targets", []))
    elif node_type == "metric":
        view["expression"] = properties.get("expression", asset.get("expression", {}))
        view["sql_generation_plans"] = properties.get(
            "sql_generation_plans",
            asset.get("sql_generation_plans", []),
        )
    elif node_type == "generation_rule":
        view["condition"] = properties.get("condition", asset.get("condition", ""))
        view["action"] = properties.get("action", asset.get("action", ""))
    return view


def _scalar_section(title: str, values: dict[str, Any]) -> str:
    body = "\n".join(f"{key}: {_stringify_scalar(value)}" for key, value in values.items())
    return f"# {title}\n{body}"


def _text_section(title: str, value: Any) -> str:
    return f"# {title}\n{_stringify_scalar(value)}"


def _code_section(title: str, language: str, value: Any) -> str:
    return f"# {title}\n```{language}\n{_stringify_scalar(value)}\n```"


def _json_section(title: str, value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return f"# {title}\n```json\n{body}\n```"


def _stringify_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _payload_result(payload: Any, output_model: type[BaseModel]) -> list[dict[str, Any]]:
    if isinstance(payload, ErrorAnalysisMinimalRepairActionOutput):
        return [payload.result.model_dump(mode="json")]
    if isinstance(payload, ErrorAnalysisMinimalRepairAblationOutput):
        return [payload.result.model_dump(mode="json")]
    if isinstance(payload, ErrorAnalysisMinimalRepairReviewOutput):
        return [payload.result.model_dump(mode="json")]
    if isinstance(payload, dict):
        if "result" not in payload:
            direct_model: type[BaseModel] | None = None
            if output_model is ErrorAnalysisMinimalRepairActionOutput:
                direct_model = RepairAction
            elif output_model is ErrorAnalysisMinimalRepairAblationOutput:
                direct_model = RepairAblationProposal
            elif output_model is ErrorAnalysisMinimalRepairReviewOutput:
                direct_model = RepairReview
            if direct_model is not None:
                return [direct_model.model_validate(payload).model_dump(mode="json")]
        validated = output_model.model_validate(payload)
        result = validated.result
        return [result.model_dump(mode="json")]
    return []
