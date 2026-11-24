"""Fail before transport when a graph request exceeds the conservative wire budget."""

import json

from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai.chat_models.base import _convert_message_to_dict


class RequestInputTooLarge(RuntimeError):
    retryable = False

    def __init__(self, size, budget, breakdown):
        self.details = {"characters": size, "budget": budget, "components": breakdown}
        super().__init__(
            f"Graph request has {size} serialized characters including messages/tools; budget {budget}. "
            f"Components (character estimates): {json.dumps(breakdown, ensure_ascii=False)}. "
            "Split the review scope; do not truncate evidence or retry unchanged input."
        )


def check_request(
    messages, tools, max_characters=210000, *, on_measure=None, message_sections=None
):
    body = {
        "messages": [_convert_message_to_dict(m) for m in messages],
        "tools": [convert_to_openai_tool(t) for t in tools],
    }
    size = len(json.dumps(body, ensure_ascii=False, default=str))

    def length(value):
        return len(json.dumps(value, ensure_ascii=False, default=str))

    task = {}
    if len(body["messages"]) > 1:
        try:
            task = json.loads(body["messages"][1].get("content") or "{}")
        except (ValueError, TypeError):
            pass
    if not isinstance(task, dict):
        task = {}
    draft = task.pop("draft", None)
    diagnostic_names = (
        "issues",
        "semantic_issues",
        "semantic_issue_basis",
        "latest_output_error",
        "required_context",
        "repair_facts",
    )
    diagnostics = {key: task.pop(key) for key in diagnostic_names if key in task}
    breakdown = {
        "system": length(body["messages"][:1]),
        "task_and_graph": length(task),
        "draft": length(draft) if draft is not None else 0,
        "diagnostics_and_basis": length(diagnostics),
        "read_exchanges": length(body["messages"][2:]),
        "tool_schemas": length(body["tools"]),
    }
    if message_sections is not None:
        if len(message_sections) != len(body["messages"]) - 1 or len(
            set(message_sections)
        ) != len(message_sections):
            raise ValueError(
                "Input budget sections must name each dynamic message exactly once"
            )
        breakdown = {
            "system": length(body["messages"][:1]),
            **{
                name: length(body["messages"][index + 1])
                for index, name in enumerate(message_sections)
            },
            "tool_schemas": length(body["tools"]),
        }
    if on_measure:
        on_measure(
            {"characters": size, "budget": max_characters, "components": breakdown}
        )
    if size > max_characters:
        raise RequestInputTooLarge(size, max_characters, breakdown)
    return size
