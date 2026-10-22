"""Synchronous and asynchronous structured-output helpers for knowledge capabilities."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ValidationError

from cortex.infrastructure.llm.providers import (
    LLMInvocationError,
    LLMStructuredOutputError,
)

OutputModelT = TypeVar("OutputModelT", bound=BaseModel)


class ReasoningParser:
    """Extract the structured payload emitted inside optional result tags."""

    def parse(self, text: str) -> str:
        match = re.search(
            r"<result>(.*?)</result>", text, flags=re.DOTALL | re.IGNORECASE
        )
        return match.group(1).strip() if match else text.strip()


def response_text(response: Any) -> str:
    """Normalize a LangChain chat response while omitting thinking blocks."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                if item.get("type") == "thinking":
                    continue
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    return str(content)


def build_prompt_messages(
    prompt: str,
    output_model: type[BaseModel],
    human_payload: Mapping[str, Any],
    *,
    human_content: str | None = None,
) -> list[dict[str, str]]:
    """Build the historical system/user prompt pair with a Pydantic JSON Schema."""
    schema = json.dumps(output_model.model_json_schema(), ensure_ascii=False, indent=2)
    if "{template_output}" in prompt:
        system_content = prompt.replace("{template_output}", schema)
    else:
        system_content = (
            f"{prompt.rstrip()}\n\n"
            "Return JSON matching this schema inside <result>...</result>:\n"
            f"{schema}"
        )
    rendered_human = (
        human_content
        if human_content is not None
        else json.dumps(human_payload, ensure_ascii=False, default=str)
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": rendered_human},
    ]


async def run_llm_extraction(
    *,
    llm: BaseChatModel,
    prompt: str,
    output_model: type[OutputModelT],
    human_payload: Mapping[str, Any],
    human_content: str | None = None,
    trace_capture: dict[str, Any] | None = None,
    allow_repair: bool = True,
    repair_with_context: bool = False,
) -> OutputModelT:
    """Invoke the model and validate a structured response, with one repair attempt."""
    messages = build_prompt_messages(
        prompt,
        output_model,
        human_payload,
        human_content=human_content,
    )
    if trace_capture is not None:
        trace_capture["prompt"] = messages
    raw_response = await _invoke(llm, messages)
    if trace_capture is not None:
        trace_capture["response"] = raw_response
    first_error: json.JSONDecodeError | ValidationError | ValueError | None = None
    try:
        return _parse_output(raw_response, output_model)
    except (json.JSONDecodeError, ValidationError, ValueError) as error:
        first_error = error
        if not allow_repair:
            raise LLMStructuredOutputError(
                f"LLM output did not match {output_model.__name__}"
            ) from error

    assert first_error is not None
    repair_messages = _build_repair_messages(
        raw_response,
        output_model,
        validation_error=first_error,
    )
    if repair_with_context:
        repair_messages = [
            *messages,
            {"role": "assistant", "content": raw_response},
            {"role": "user", "content": repair_messages[0]["content"]},
        ]
    repaired_response = await _invoke(llm, repair_messages)
    if trace_capture is not None:
        trace_capture["repair_response"] = repaired_response
    try:
        return _parse_output(repaired_response, output_model)
    except (json.JSONDecodeError, ValidationError, ValueError) as repair_error:
        raise LLMStructuredOutputError(
            f"LLM output did not match {output_model.__name__} after one repair attempt"
        ) from repair_error


async def _invoke(llm: BaseChatModel, messages: list[dict[str, str]]) -> str:
    try:
        response = await llm.ainvoke(messages)
    except Exception as exc:
        raise LLMInvocationError("LLM invocation failed") from exc
    return response_text(response)


def run_llm_extraction_sync(
    *,
    llm: BaseChatModel,
    prompt: str,
    output_model: type[OutputModelT],
    human_payload: Mapping[str, Any],
    human_content: str | None = None,
    trace_capture: dict[str, Any] | None = None,
    allow_repair: bool = True,
    repair_with_context: bool = False,
) -> OutputModelT:
    """Invoke the model and validate a structured response, with one repair attempt."""
    messages = build_prompt_messages(
        prompt,
        output_model,
        human_payload,
        human_content=human_content,
    )
    if trace_capture is not None:
        trace_capture["prompt"] = messages
    raw_response = _invoke_sync(llm, messages)
    if trace_capture is not None:
        trace_capture["response"] = raw_response
    first_error: json.JSONDecodeError | ValidationError | ValueError | None = None
    try:
        return _parse_output(raw_response, output_model)
    except (json.JSONDecodeError, ValidationError, ValueError) as error:
        first_error = error
        if not allow_repair:
            raise LLMStructuredOutputError(
                f"LLM output did not match {output_model.__name__}"
            ) from error

    assert first_error is not None
    repair_messages = _build_repair_messages(
        raw_response,
        output_model,
        validation_error=first_error,
    )
    if repair_with_context:
        repair_messages = [
            *messages,
            {"role": "assistant", "content": raw_response},
            {"role": "user", "content": repair_messages[0]["content"]},
        ]
    repaired_response = _invoke_sync(llm, repair_messages)
    if trace_capture is not None:
        trace_capture["repair_response"] = repaired_response
    try:
        return _parse_output(repaired_response, output_model)
    except (json.JSONDecodeError, ValidationError, ValueError) as repair_error:
        raise LLMStructuredOutputError(
            f"LLM output did not match {output_model.__name__} after one repair attempt"
        ) from repair_error


def _invoke_sync(llm: BaseChatModel, messages: list[dict[str, str]]) -> str:
    try:
        response = llm.invoke(messages)
    except Exception as exc:
        raise LLMInvocationError("LLM invocation failed") from exc
    return response_text(response)


def _parse_output(text: str, output_model: type[OutputModelT]) -> OutputModelT:
    candidate = _strip_json_fence(ReasoningParser().parse(text))
    return output_model.model_validate(json.loads(candidate))


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE
    )
    return match.group(1).strip() if match else stripped


def _build_repair_messages(
    invalid_response: str,
    output_model: type[BaseModel],
    *,
    validation_error: json.JSONDecodeError | ValidationError | ValueError,
) -> list[dict[str, str]]:
    schema = json.dumps(output_model.model_json_schema(), ensure_ascii=False, indent=2)
    return [
        {
            "role": "system",
            "content": (
                "Repair the invalid response into JSON matching the supplied schema. "
                "Correct the reported validation errors; do not preserve values that the "
                "schema rejects. "
                "Return only <result>...</result>.\n\n"
                f"Validation errors:\n{validation_error}\n\n"
                f"Schema:\n{schema}"
            ),
        },
        {"role": "user", "content": invalid_response},
    ]


__all__ = [
    "ReasoningParser",
    "build_prompt_messages",
    "response_text",
    "run_llm_extraction",
    "run_llm_extraction_sync",
]
