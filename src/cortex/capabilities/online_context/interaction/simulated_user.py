from __future__ import annotations

import sqlglot
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict
from sqlglot import expressions

from cortex.application.online_context import InteractionAnswer, InteractionQuestion
from cortex.capabilities.online_context.llm.prompts import load_prompt
from cortex.infrastructure.llm import run_llm_extraction


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SimulatorAnswerReview(_Model):
    answers: tuple[InteractionAnswer, ...]
    invalid_question_ids: tuple[str, ...] = ()


class SimulatedUserAgent:
    """Evaluation-only Agent that answers an existing interaction from Gold SQL."""

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    async def answer(
        self,
        *,
        question: str,
        gold_sql: str,
        interaction_question: InteractionQuestion,
        dialect: str = "sqlite",
    ) -> InteractionAnswer:
        gold_tree = _parse_sql(gold_sql, dialect)
        payload = {
            "question": question,
            "dialect": dialect,
            "gold_sql": gold_sql,
            "gold_aliases": _aliases(gold_tree) if gold_tree is not None else {},
            "interaction_question": interaction_question.model_dump(mode="json"),
        }
        result = await self._invoke_answer(payload)
        _validate_answer(result, interaction_question)
        return result

    async def review_answers(
        self,
        *,
        question: str,
        gold_sql: str,
        interaction_questions: tuple[InteractionQuestion, ...],
        tentative_answers: tuple[InteractionAnswer, ...],
        prior_questions: tuple[InteractionQuestion, ...] = (),
        prior_answers: tuple[InteractionAnswer, ...] = (),
        dialect: str = "sqlite",
    ) -> tuple[InteractionAnswer, ...]:
        gold_tree = _parse_sql(gold_sql, dialect)
        result = await run_llm_extraction(
            llm=self._llm,
            prompt=load_prompt("simulated_user_review"),
            output_model=SimulatorAnswerReview,
            human_payload={
                "question": question,
                "dialect": dialect,
                "gold_sql": gold_sql,
                "gold_aliases": _aliases(gold_tree) if gold_tree is not None else {},
                "interaction_questions": [
                    item.model_dump(mode="json") for item in interaction_questions
                ],
                "tentative_answers": [item.model_dump(mode="json") for item in tentative_answers],
                "prior_questions": [item.model_dump(mode="json") for item in prior_questions],
                "prior_answers": [item.model_dump(mode="json") for item in prior_answers],
            },
            repair_with_context=True,
        )
        expected_ids = tuple(item.question_id for item in interaction_questions)
        invalid_ids = result.invalid_question_ids
        if len(invalid_ids) != len(set(invalid_ids)) or not set(invalid_ids) <= set(expected_ids):
            raise ValueError("simulator review returned invalid question IDs")
        if invalid_ids:
            raise ValueError("simulator rejected non-local Slots: " + ", ".join(invalid_ids))
        actual_ids = tuple(item.question_id for item in result.answers)
        if actual_ids != expected_ids:
            raise ValueError("simulator review must return every current answer once and in order")
        for answer, interaction_question in zip(result.answers, interaction_questions, strict=True):
            _validate_answer(answer, interaction_question)
        return result.answers

    async def answer_round(
        self,
        *,
        question: str,
        gold_sql: str,
        interaction_questions: tuple[InteractionQuestion, ...],
        prior_questions: tuple[InteractionQuestion, ...] = (),
        prior_answers: tuple[InteractionAnswer, ...] = (),
        dialect: str = "sqlite",
    ) -> tuple[InteractionAnswer, ...]:
        tentative = tuple(
            [
                await self.answer(
                    question=question,
                    gold_sql=gold_sql,
                    interaction_question=interaction_question,
                    dialect=dialect,
                )
                for interaction_question in interaction_questions
            ]
        )
        return await self.review_answers(
            question=question,
            gold_sql=gold_sql,
            interaction_questions=interaction_questions,
            tentative_answers=tentative,
            prior_questions=prior_questions,
            prior_answers=prior_answers,
            dialect=dialect,
        )

    async def _invoke_answer(self, payload: dict) -> InteractionAnswer:
        return await run_llm_extraction(
            llm=self._llm,
            prompt=load_prompt("simulated_user"),
            output_model=InteractionAnswer,
            human_payload=payload,
            repair_with_context=True,
        )


def _validate_answer(result: InteractionAnswer, question: InteractionQuestion) -> None:
    if result.question_id != question.question_id:
        raise ValueError("simulated user changed the question ID")
    allowed = {item.option_id for item in question.options}
    if not set(result.selected_option_ids) <= allowed:
        raise ValueError("simulated user selected an unknown option")


def _parse_sql(sql: str, dialect: str):
    for candidate_dialect in dict.fromkeys((dialect, "sqlite", "mysql", "")):
        try:
            return sqlglot.parse_one(sql, dialect=candidate_dialect or None)
        except (sqlglot.errors.ParseError, ValueError):
            continue
    return None


def _aliases(tree) -> dict[str, str]:
    return {
        table.alias_or_name.casefold(): table.name for table in tree.find_all(expressions.Table)
    }


__all__ = ["SimulatedUserAgent", "SimulatorAnswerReview"]
