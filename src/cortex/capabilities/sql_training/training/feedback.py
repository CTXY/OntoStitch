"""Independent structured assessment; the Learning Agent never writes verdicts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    messages_to_dict,
)

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.graph.snapshots import (
    GraphSnapshot,
)
from cortex.capabilities.sql_training.llm.generation_rule_taxonomy import (
    operation_registry,
)
from cortex.capabilities.sql_training.llm.parsing import ReasoningParser, response_text
from cortex.capabilities.sql_training.llm.prompts import load_prompt, prompt_fingerprint
from cortex.capabilities.sql_training.training.assessment_models import (
    AssessmentFeedback,
    application_options,
)
from cortex.schema import SchemaDefinition


def assessment_response_schema():
    """The Agent judges checks; code owns their redundant aggregate label."""
    schema = AssessmentFeedback.model_json_schema()
    unit = schema["$defs"]["UnitResult"]
    unit["properties"].pop("quality")
    unit["required"] = [key for key in unit["required"] if key != "quality"]
    if "quality_checks" not in unit["required"]:
        unit["required"].append("quality_checks")
    return schema


def assessment_messages(payload):
    system = load_prompt("learning_assessment") + "\n\nResponse schema:\n" + json.dumps(assessment_response_schema())
    view = {**payload, "units": [
        {**unit, "application_options": application_options(unit, payload["attempts"])}
        for unit in payload["units"]
    ]}
    return [SystemMessage(content=system), HumanMessage(content=json.dumps(view, ensure_ascii=False, default=str, separators=(",", ":")))]


class AssessmentOutputInvalid(ValueError):
    """A rejected assessment is a contract error, not a network outage."""
    retryable = False


def assess(
    model: BaseChatModel,
    *,
    record: dict[str, Any],
    schema: SchemaDefinition,
    baseline: GraphSnapshot,
    accepted: GraphSnapshot,
    trial: GraphSnapshot,
    groups: list[ChangeGroup],
    targets: list[dict[str, Any]],
    validation: dict[str, Any],
    context_limit: int,
    trace_capture: dict[str, Any] | None = None,
    prior_evidence: list[dict[str, Any]] | None = None,
) -> AssessmentFeedback:
    from cortex.capabilities.sql_training.training.assessment_inputs import (
        assessment_payload,
    )

    payload = assessment_payload(
        record=record, schema=schema, baseline=baseline, accepted=accepted,
        trial=trial, groups=groups, targets=targets, validation=validation, prior_evidence=prior_evidence,
    )
    return assess_payload(model, payload=payload, groups=groups,
                          context_limit=context_limit, trace_capture=trace_capture)


def assess_payload(model, *, payload, groups, context_limit, trace_capture=None):
    """Evaluate one fixed evidence payload; format recovery stays in this dialogue."""
    messages = assessment_messages(payload)
    # One bounded retry for malformed structured output, never a new graph trial.
    for attempt in range(2):
        if sum(len(m.content) for m in messages) > context_limit:
            raise ValueError("Assessment evidence exceeds configured context limit")
        captured = None
        if trace_capture is not None:
            captured = {
                "prompt": messages_to_dict(messages),
                "response": None,
                "prompt_fingerprint": prompt_fingerprint("learning_assessment"),
            }
            trace_capture.setdefault("attempts", []).append(captured)
        response = response_text(model.invoke(messages))
        if captured is not None:
            captured["response"] = response
        try:
            raw = ReasoningParser().parse(response).strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = AssessmentFeedback.model_validate_json(raw)
            result.validate_evidence(payload)
            result.accepted_members(groups)
            return result
        except (ValueError, TypeError) as exc:
            if attempt:
                raise AssessmentOutputInvalid(f"Assessment output invalid: {exc}") from exc
            messages.append(AIMessage(content=response))
            messages.append(
                HumanMessage(
                    content=f"Correct all reported contract violations in your previous assessment:\n{exc}\nPreserve its substantive judgments, defects and feedback. Correct an incompatible application label against that unit's supplied requirement and SQL/context evidence; do not blindly preserve an invalid label or change it to applied merely to pass validation. Add required feedback for unsuccessful states; do not erase a defect to satisfy the schema. Use exact supplied node or relation IDs for location, with field details in gap. Return the complete schema JSON."
                )
            )
    raise AssertionError("unreachable")


def assessment_policy_id() -> str:
    schema_id = hashlib.sha256(json.dumps(AssessmentFeedback.model_json_schema(), sort_keys=True).encode()).hexdigest()
    policy = hashlib.sha256()
    for name in ("assessment_inputs.py", "assessment_models.py", "assessment_views.py", "quality_contract.py", "feedback.py"):
        policy.update(name.encode())
        policy.update(Path(__file__).with_name(name).read_bytes())
    policy.update(json.dumps(operation_registry(), sort_keys=True).encode())
    return "sql-and-assessment-v6:" + prompt_fingerprint("learning_assessment") + ":" + schema_id + ":" + policy.hexdigest()
