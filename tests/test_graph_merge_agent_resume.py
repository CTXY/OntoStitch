"""Nested agents retain completed tool reads when the outer node is interrupted."""

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_agent_runtime import ScriptModel, call, message
from test_graph_merge_workflow import build, inputs, response

from cortex.capabilities.graph_merge.llm import agents


def test_nested_agent_resumes_tool_memory_without_repeating_completed_read(monkeypatch):
    context, proposal, review = inputs()
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "resume-after-evidence-read"}}
    reads = []
    build_read_tools = agents.build_read_tools

    def counted_tools(context):
        tools = build_read_tools(context)
        reader = next(tool for tool in tools if tool.name == "read_evidence")
        read = reader.func

        def counted_read(ids):
            reads.append(ids)
            return read(ids)

        reader.func = counted_read
        return tools

    monkeypatch.setattr(agents, "build_read_tools", counted_tools)

    def power_loss(messages):
        assert any(isinstance(item, ToolMessage) for item in messages)
        raise KeyboardInterrupt("process lost after completed evidence read")

    first = ScriptModel(
        [message(call("read_evidence", {"ids": ["a"]}, "read-source")), power_loss]
    )
    with pytest.raises(KeyboardInterrupt, match="completed evidence read"):
        build(first, saver).invoke(
            {"context": context.model_dump(mode="json")},
            config=config,
            durability="sync",
        )
    assert reads == [["a"]]
    assert build(first, saver).get_state(config).next == ("consolidate",)

    resumed = ScriptModel([response(proposal), response(review)])
    output = build(resumed, saver).invoke(None, config=config, durability="sync")

    assert output["accepted"]
    assert len(resumed._calls) == 2
    assert reads == [["a"]]
    messages = resumed._calls[0]
    tool_result = next(item for item in messages if isinstance(item, ToolMessage))
    assert tool_result.tool_call_id == "read-source"
    assert json.loads(tool_result.content)["items"][0]["id"] == context.evidence[0].id
    assert any(
        isinstance(item, AIMessage)
        and any(tool["id"] == "read-source" for tool in item.tool_calls)
        for item in messages
    )
