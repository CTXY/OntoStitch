"""Fail before transport when a graph request exceeds the conservative wire budget."""
import json
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai.chat_models.base import _convert_message_to_dict


class RequestInputTooLarge(RuntimeError):
    pass


def check_request(messages, tools, max_characters=210000):
    body = {"messages": [_convert_message_to_dict(m) for m in messages],
            "tools": [convert_to_openai_tool(t) for t in tools]}
    size = len(json.dumps(body, ensure_ascii=False, default=str))
    if size > max_characters:
        raise RequestInputTooLarge(f"Graph request has {size} serialized characters including messages/tools; budget {max_characters}. Split the review scope; do not truncate evidence or retry unchanged input.")
    return size
