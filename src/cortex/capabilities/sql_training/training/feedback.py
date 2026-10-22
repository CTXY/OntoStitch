"""Independent structured assessment; the Learning Agent never writes verdicts."""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, messages_to_dict
from pydantic import Field

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.graph.snapshots import (
    GraphSnapshot,
    semantic_diff,
)
from cortex.capabilities.sql_training.llm.parsing import ReasoningParser, response_text
from cortex.capabilities.sql_training.llm.prompts import load_prompt, prompt_fingerprint
from cortex.capabilities.sql_training.models.base import NodeModel


class GroupVerdict(NodeModel):
    group_id: str
    verdict: Literal["acceptable", "revise", "reject", "inconclusive"]
    evidence: str
    revision_suggestion: str = ""


class TargetVerdict(NodeModel):
    target_id: str
    status: Literal["resolved", "unresolved", "inconclusive"]
    evidence: str = ""


class AssessmentFeedback(NodeModel):
    group_results: list[GroupVerdict]
    target_results: list[TargetVerdict]
    merge_groups: list[list[str]] = Field(default_factory=list)
    deferred_observations: list[str] = Field(default_factory=list)

    def accepted_members(self, groups: list[ChangeGroup]) -> list[str]:
        expected = {g.group_id for g in groups}
        actual = [r.group_id for r in self.group_results]
        if set(actual) != expected or len(actual) != len(expected):
            raise ValueError("Assessment must cover every group exactly once")
        by_id = {g.group_id: set(g.member_ids) for g in groups}
        acceptable = {
            r.group_id for r in self.group_results if r.verdict == "acceptable"
        }
        # Merge declarations only reduce independent acceptance. No extra LLM or
        # combination search: a coupled set is acceptable iff every member is.
        coupled = [set(row) for row in self.merge_groups]
        if any(not row or not row <= expected for row in coupled):
            raise ValueError("Assessment merge_groups references unknown groups")
        changed = True
        while changed:
            changed = False
            for row in coupled:
                if not row <= acceptable and row & acceptable:
                    acceptable -= row
                    changed = True
        return sorted({key for group_id in acceptable for key in by_id[group_id]})


def assess(
    model: BaseChatModel,
    *,
    record: dict[str, Any],
    baseline: GraphSnapshot,
    accepted: GraphSnapshot,
    trial: GraphSnapshot,
    groups: list[ChangeGroup],
    targets: list[dict[str, Any]],
    validation: dict[str, Any],
    context_limit: int,
    trace_capture: dict[str, Any] | None = None,
) -> AssessmentFeedback:
    from cortex.schema.ontology_asset_definitions import render_ontology_asset_contract

    round_changed = semantic_diff(baseline, trial)
    tentative_changed = semantic_diff(accepted, trial)
    payload = {
        "snapshot_payload_policy": "current contains all semantic assets; baseline/accepted_before contain only changed prior definitions. Omitted unchanged nodes remain present.",
        "source": record,
        "baseline_revision": baseline.revision,
        "accepted_revision_before": accepted.revision,
        "trial_revision": trial.revision,
        "baseline": [
            a.model_dump(mode="json")
            for a in baseline.semantic_assets
            if a.id in round_changed
        ],
        "accepted_before": [
            a.model_dump(mode="json")
            for a in accepted.semantic_assets
            if a.id in tentative_changed
        ],
        "current": [a.model_dump(mode="json") for a in trial.semantic_assets],
        "round_changed_ids": round_changed,
        "tentative_changed_ids": tentative_changed,
        "groups": [g.model_dump(mode="json") for g in groups],
        "fixed_targets": targets,
        "validation": validation,
    }
    affected_types = sorted(
        {a.node_type.value for a in trial.semantic_assets if a.id in tentative_changed}
    )
    contract = render_ontology_asset_contract(("common", "owners", *affected_types))
    system = (
        load_prompt("learning_assessment")
        + "\n\n"
        + contract
        + "\n\nResponse schema:\n"
        + json.dumps(AssessmentFeedback.model_json_schema())
    )
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(system) + len(text) > context_limit:
        raise ValueError("Assessment evidence exceeds configured context limit")
    messages = [SystemMessage(content=system), HumanMessage(content=text)]
    # One bounded retry for malformed structured output, never a new graph trial.
    for attempt in range(2):
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
            result.accepted_members(groups)
            expected = {str(t["target_id"]) for t in targets}
            actual = [t.target_id for t in result.target_results]
            if set(actual) != expected or len(actual) != len(expected):
                raise ValueError(
                    "Assessment must cover every fixed target exactly once"
                )
            return result
        except (ValueError, TypeError) as exc:
            if attempt:
                raise ValueError(f"Assessment output invalid: {exc}") from exc
            messages.append(
                HumanMessage(
                    content=f"Previous output was invalid ({exc}). Return the complete schema JSON."
                )
            )
    raise AssertionError("unreachable")


def assessment_policy_id() -> str:
    return prompt_fingerprint("learning_assessment")
