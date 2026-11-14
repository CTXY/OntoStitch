import pytest
from langchain_core.messages import AIMessage

from cortex.infrastructure.llm.deepseek import DeepSeekChatOpenAI


@pytest.mark.parametrize("role", ["", None, " "])
def test_missing_completion_role_preserves_validated_tool_calls(role):
    model = DeepSeekChatOpenAI(model="deepseek-v4-pro", api_key="offline-test")
    raw = {
        "id": "test", "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": role, "content": "Inspect the relevant object.",
            "tool_calls": [{"id": "call1", "type": "function", "function": {
                "name": "inspect_graph", "arguments": '{"ids":["N"]}'}}],
        }}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    message = model._create_chat_result(raw).generations[0].message
    assert isinstance(message, AIMessage)
    assert message.tool_calls[0]["name"] == "inspect_graph"
    assert message.tool_calls[0]["args"] == {"ids": ["N"]}
    assert message.usage_metadata["total_tokens"] == 15
    assert raw["choices"][0]["message"]["role"] == role


def test_missing_role_does_not_hide_invalid_tool_json():
    model = DeepSeekChatOpenAI(model="deepseek-v4-pro", api_key="offline-test")
    raw = {"choices": [{"index": 0, "message": {"role": "", "content": "",
        "tool_calls": [{"id": "bad", "type": "function", "function": {
            "name": "inspect_graph", "arguments": "not-json"}}]}, "finish_reason": "tool_calls"}]}
    message = model._create_chat_result(raw).generations[0].message
    assert message.tool_calls == []
    assert message.invalid_tool_calls[0]["id"] == "bad"
