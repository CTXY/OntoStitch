from __future__ import annotations

from typing import Protocol
from collections import Counter

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field

from cortex.application.online_context import (
    ChoicePoint,
    InteractionAnswer,
    InteractionGuidance,
)
from cortex.capabilities.online_context.llm.prompts import load_prompt
from cortex.infrastructure.llm import run_llm_extraction


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SlotGuidance(_Model):
    choice_point_id: str = Field(
        min_length=1,
        description="The unchanged ID of the one Slot this guidance answers.",
    )
    guidance_content: str = Field(
        min_length=1,
        description="Final downstream guidance for this Slot only.",
    )


class RuleGuidance(_Model):
    rule_id: str
    include: bool
    reason: str = Field(min_length=1)
    instruction: str = ""


class GuidanceAgentOutput(_Model):
    rule_guidance: tuple[RuleGuidance, ...] = ()
    guidance_by_slot: tuple[SlotGuidance, ...] = Field(
        description="One independent guidance item per supplied Slot, in Slot order."
    )


class GuidanceSynthesis(_Model):
    rule_guidance: tuple[RuleGuidance, ...] = ()
    guidance_content: str
    interaction_guidance: tuple[InteractionGuidance, ...]


class GuidanceSynthesizer(Protocol):
    async def synthesize(
        self,
        *,
        question: str,
        dialect: str,
        choice_points: tuple[ChoicePoint, ...],
        answers: tuple[InteractionAnswer, ...],
        rule_candidates: tuple[dict, ...] = (),
    ) -> GuidanceSynthesis: ...


class GuidanceAgent:
    """Render confirmed choices and assess task-local Rule applicability in one call."""

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    async def synthesize(
        self,
        *,
        question: str,
        dialect: str = "mysql",
        choice_points: tuple[ChoicePoint, ...],
        answers: tuple[InteractionAnswer, ...],
        rule_candidates: tuple[dict, ...] = (),
    ) -> GuidanceSynthesis:
        if not choice_points and not rule_candidates:
            return GuidanceSynthesis(guidance_content="", interaction_guidance=())
        expected_answer_ids = {
            f"question:{point.choice_point_id}" for point in choice_points
        }
        if (
            len(answers) != len(expected_answer_ids)
            or {item.question_id for item in answers} != expected_answer_ids
        ):
            raise ValueError(
                "guidance answers must cover every choice point exactly once"
            )
        payload = {
            "question": question,
            "dialect": dialect,
            "slots": [item.model_dump(mode="json") for item in choice_points],
            "user_answers": [item.model_dump(mode="json") for item in answers],
            "rule_candidates": rule_candidates,
        }
        expected_rule_ids = {item["rule_id"] for item in rule_candidates}
        if len(expected_rule_ids) != len(rule_candidates):
            raise ValueError("rule_candidates must have unique rule_id values")
        expected_slot_ids = tuple(point.choice_point_id for point in choice_points)

        def validate_output(result):
            actual = [item.rule_id for item in result.rule_guidance]
            missing = sorted(expected_rule_ids - set(actual))
            unknown = sorted(set(actual) - expected_rule_ids)
            duplicate = sorted(key for key, count in Counter(actual).items() if count > 1)
            if missing or unknown or duplicate:
                raise ValueError(f"guidance must assess each candidate Rule exactly once; missing_rule_ids={missing}; unknown_rule_ids={unknown}; duplicate_rule_ids={duplicate}")
            empty = [item.rule_id for item in result.rule_guidance if item.include and not item.instruction.strip()]
            if empty:
                raise ValueError(f"applicable Rules require instructions: {empty}")
            actual_slots = tuple(item.choice_point_id for item in result.guidance_by_slot)
            if actual_slots != expected_slot_ids:
                raise ValueError(f"guidance must cover every Slot exactly once and in order; expected={expected_slot_ids}; actual={actual_slots}")
            for point, item in zip(choice_points, result.guidance_by_slot):
                answer = next(a for a in answers if a.question_id == f"question:{point.choice_point_id}")
                _preserve_confirmed_behavior(item.guidance_content, point, answer)

        result = await run_llm_extraction(
            llm=self._llm,
            prompt=load_prompt("guidance"),
            output_model=GuidanceAgentOutput,
            human_payload=payload,
            output_validator=validate_output,
            repair_with_context=True,
        )
        point_by_id = {item.choice_point_id: item for item in choice_points}
        guidance_by_id = {
            item.choice_point_id: item
            for item in result.guidance_by_slot
            if item.choice_point_id in point_by_id
        }
        answer_by_id = {item.question_id: item for item in answers}
        guidance = []
        for point in choice_points:
            answer = answer_by_id[f"question:{point.choice_point_id}"]
            item = guidance_by_id.get(point.choice_point_id)
            content = (
                item.guidance_content.strip()
                if item is not None and item.guidance_content.strip()
                else _fallback_guidance(point, answer)
            )
            content = _preserve_confirmed_behavior(content, point, answer)
            guidance.append(
                InteractionGuidance(
                    guidance_id=f"guidance:{point.choice_point_id}",
                    choice_point_id=point.choice_point_id,
                    evidence_priority=point.evidence_priority,
                    source_category=point.source_category,
                    applicable_candidate_count=point.applicable_candidate_count,
                    candidate_relevance_sum=point.candidate_relevance_sum,
                    priority_score=point.priority_score,
                    priority_reason=point.priority_reason,
                    decision_question=point.decision_question,
                    should_be=content,
                    must_not_be=(),
                    source=(
                        "user_choice"
                        if answer.selected_option_ids
                        else "user_explanation"
                    ),
                )
            )
        return GuidanceSynthesis(
            guidance_content="\n".join(item.should_be for item in guidance),
            interaction_guidance=tuple(guidance),
            rule_guidance=result.rule_guidance,
        )


def _fallback_guidance(point: ChoicePoint, answer: InteractionAnswer) -> str:
    selected = set(answer.selected_option_ids)
    selected_behavior = "; ".join(
        option.statement for option in point.options if option.option_id in selected
    )
    return selected_behavior or answer.explanation.strip()


def _preserve_confirmed_behavior(
    content: str, point: ChoicePoint, answer: InteractionAnswer
) -> str:
    """Let the Agent describe purpose without rewriting confirmed SQL behavior."""
    behavior = _fallback_guidance(point, answer)
    marker = "MUST USE:"
    if not behavior:
        return content
    if marker not in content:
        raise ValueError("guidance must identify confirmed behavior with MUST USE:")
    purpose = content.split(marker, 1)[0].rstrip(" ,")
    return f"{purpose}, {marker} {behavior}"


__all__ = [
    "GuidanceAgent",
    "GuidanceAgentOutput",
    "GuidanceSynthesis",
    "GuidanceSynthesizer",
    "SlotGuidance",
]
