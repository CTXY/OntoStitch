from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import JsonOutputParser


class OutputFixingParser:
    """Async-model-aware fixing parser facade used by the migrated runtime."""

    @staticmethod
    def from_llm(*, llm: Any, parser: JsonOutputParser) -> _OutputFixingParser:
        return _OutputFixingParser(llm, parser)


class _OutputFixingParser:
    def __init__(self, llm: Any, parser: JsonOutputParser):
        self.llm = llm
        self.parser = parser

    def parse(self, text: str) -> Any:
        schema = json.dumps(
            self.parser.pydantic_object.model_json_schema(),
            ensure_ascii=False,
            indent=2,
        )
        repaired = _invoke_llm(
            self.llm,
            [
                SystemMessage(
                    content=(
                        "Repair the invalid response into JSON matching the schema. "
                        "Return only <result>...</result>.\n\n"
                        f"Schema:\n{schema}"
                    )
                ),
                HumanMessage(content=text),
            ],
        )
        repaired_text = _strip_json_fence(
            ReasoningParser().parse(response_text(repaired))
        )
        _reject_duplicate_json_keys(repaired_text)
        return _validate_parsed_output(self.parser, self.parser.parse(repaired_text))


class ReasoningParser:
    def parse(self, text: str) -> str:
        match = re.search(r"<result>(.*?)</result>", text, flags=re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else text.strip()


def response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") != "thinking":
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    return str(content)


def build_prompt_messages(
    prompt: str,
    parser: JsonOutputParser,
    human_payload: dict[str, Any],
    *,
    human_content: str | None = None,
) -> list[Any]:
    output_model = parser.pydantic_object
    schema = json.dumps(output_model.model_json_schema(), ensure_ascii=False, indent=2)
    system_content = prompt.replace("{template_output}", schema)
    rendered_human = (
        human_content
        if human_content is not None
        else json.dumps(human_payload, ensure_ascii=False, default=str)
    )
    return [SystemMessage(content=system_content), HumanMessage(content=rendered_human)]


def parse_envelope(
    llm: Any,
    parser: JsonOutputParser,
    response_text_value: str,
    *,
    allow_output_fixing: bool = True,
) -> Any:
    candidate = _strip_json_fence(ReasoningParser().parse(response_text_value))
    try:
        _reject_duplicate_json_keys(candidate)
        return _validate_parsed_output(parser, parser.parse(candidate))
    except Exception:
        if not allow_output_fixing:
            raise
    fixing_parser = OutputFixingParser.from_llm(llm=llm, parser=parser)
    return _validate_parsed_output(parser, fixing_parser.parse(response_text_value))


def _validate_parsed_output(parser: JsonOutputParser, payload: Any) -> dict[str, Any]:
    """Normalize one common envelope and require a recognizable top-level Schema."""
    output_model = parser.pydantic_object
    if isinstance(payload, dict):
        model_fields = set(output_model.model_fields)
        if not model_fields.intersection(payload):
            result = payload.get("result")
            if isinstance(result, dict) and set(payload).issubset({"reasoning", "result"}):
                payload = result
            elif model_fields == {"result"}:
                result_field = output_model.model_fields.get("result")
                result_model = getattr(result_field.annotation, "model_fields", None)
                if isinstance(result_model, dict) and set(result_model).intersection(payload):
                    payload = {"result": payload}
    if not isinstance(payload, dict) or not set(output_model.model_fields).intersection(payload):
        raise ValueError(f"output does not contain fields for {output_model.__name__}")
    return payload


def _reject_duplicate_json_keys(value: str) -> None:
    """Reject conflicting JSON keys while keeping identical legacy duplicates compatible."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result and result[key] != item:
                raise ValueError(f"conflicting duplicate JSON field: {key}")
            result[key] = item
        return result

    try:
        json.loads(value, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError:
        return


def run_llm_extraction(
    *,
    llm: Any,
    prompt: str,
    output_model: type,
    human_payload: dict[str, Any],
    map_result: Callable[[Any], Any],
    human_content: str | None = None,
    trace_capture: dict[str, Any] | None = None,
    allow_output_fixing: bool = True,
) -> Any:
    parser = JsonOutputParser(pydantic_object=output_model)
    messages = build_prompt_messages(
        prompt,
        parser,
        human_payload,
        human_content=human_content,
    )
    if trace_capture is not None:
        trace_capture["prompt"] = [
            {"role": getattr(message, "type", "message"), "content": response_text(message)}
            for message in messages
        ]
    response = _invoke_llm(llm, messages)
    raw = response_text(response)
    if trace_capture is not None:
        trace_capture["response"] = raw
    parsed = parse_envelope(
        llm,
        parser,
        raw,
        allow_output_fixing=allow_output_fixing,
    )
    return map_result(parsed)


def _strip_json_fence(value: str) -> str:
    match = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        value.strip(),
        flags=re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else value.strip()


def _invoke_llm(llm: Any, messages: list[Any]) -> Any:
    """Invoke the model synchronously inside the synchronous SQL runtime."""
    return llm.invoke(messages)


__all__ = [
    "OutputFixingParser",
    "ReasoningParser",
    "build_prompt_messages",
    "parse_envelope",
    "response_text",
    "run_llm_extraction",
]
