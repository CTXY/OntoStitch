"""Prompt reductions must preserve contracts, paged facts and provider evidence."""
import json
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_agent_runtime import session_for

from cortex.capabilities.sql_training.learning.runtime import compact_repeated_guides, model_history_view
from cortex.capabilities.sql_training.learning.tools import build_tools
from cortex.capabilities.sql_training.llm.prompts import load_prompt
from cortex.infrastructure.llm.deepseek import DeepSeekChatOpenAI


def test_identical_shared_contract_is_present_once_in_both_agents():
    shared = (Path(__file__).parents[1] / "src/cortex/capabilities/sql_training/llm/prompts/learning/shared_contract.md").read_text().strip()
    for name in ("learning_system", "learning_assessment"):
        prompt = load_prompt(name)
        assert prompt.count(shared) == 1
        assert "{shared_extraction_contract}" not in prompt
    assert "Illustration: School" not in load_prompt("learning_assessment")


def test_relation_index_not_starved_by_write_guide(case_graph):
    session = session_for(case_graph)
    inspect = next(t for t in build_tools(session) if t.name == "inspect_graph")
    index = inspect.invoke({"mode": "relations", "limit": 50})
    assert len(index["relations"]) == index["total"] > 1
    assert index["next_offset"] is None
    assert "guidance" not in index
    exact = inspect.invoke({"mode": "relations", "edge_type": "grounded_on"})
    assert exact["guidance"] and exact["edge_operation_schema"]
    assert exact["relations"][0]["type"] == "grounded_on"


def test_relation_compaction_keeps_distinct_facts_and_changed_schemas():
    messages = []
    for ident, predicate, schema in (("a", "grounded_on", {"v": 1}), ("b", "is_a", {"v": 1}), ("c", "applies_to", {"v": 2})):
        messages += [AIMessage(content="", tool_calls=[{"id": ident, "name": "inspect_graph", "args": {"mode": "relations", "edge_type": predicate}}]),
                     ToolMessage(tool_call_id=ident, content=json.dumps({"guidance": "same common guide", "edge_operation_schema": schema, "relations": [{"type": predicate}], "next_offset": 1}))]
    view = compact_repeated_guides(messages)
    first, second, last = (json.loads(view[i].content) for i in (1, 3, 5))
    assert "guidance" not in first and last["guidance"] == "same common guide"
    assert first["relations"] == [{"type": "grounded_on"}]
    assert first["next_offset"] == 1
    assert "edge_operation_schema" not in first
    assert second["edge_operation_schema"] == {"v": 1}
    assert last["edge_operation_schema"] == {"v": 2}
    assert "guidance" in json.loads(messages[1].content)
    assert compact_repeated_guides(messages[-2:])[-1].content == messages[-1].content


def test_disabled_thinking_still_records_returned_reasoning_without_resending():
    model = DeepSeekChatOpenAI(model="deepseek-v4-pro", api_key="offline-test", base_url="https://offline.invalid/v1", extra_body={"thinking": {"type": "disabled"}}, reasoning_effort="none")
    raw = {"id": "offline", "model": "deepseek-v4-pro", "object": "chat.completion", "created": 0,
           "choices": [{"index": 0, "message": {"role": "assistant", "content": "{}", "reasoning_content": "synthetic marker"}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4}}
    response = model._create_chat_result(raw).generations[0].message
    assert response.additional_kwargs["reasoning_content"] == "synthetic marker"
    payload = model._get_request_payload([HumanMessage(content="offline"), response])
    assert all("reasoning_content" not in m for m in payload["messages"])


def test_history_budget_excludes_unsent_reasoning_without_changing_wire_or_raw_history():
    model = DeepSeekChatOpenAI(model="deepseek-v4-pro", api_key="offline-test", extra_body={"thinking": {"type": "disabled"}})
    original = AIMessage(content="Review this edit", additional_kwargs={"reasoning_content": "r" * 150000},
        response_metadata={"provider_note": "audit only"}, tool_calls=[{"id": "write", "name": "edit_graph", "args": {"edit_id": "a"}}])
    messages = [HumanMessage(content="Case evidence"), original, ToolMessage(content="Saved", tool_call_id="write")]
    view = model_history_view(messages, model)
    assert model._get_request_payload(messages) == model._get_request_payload(view)
    assert len(view[1].additional_kwargs) == 0
    assert len(original.additional_kwargs["reasoning_content"]) == 150000
    assert original.response_metadata == {"provider_note": "audit only"}
    assert view[1].tool_calls == original.tool_calls
    assert view[2] is messages[2]


def test_history_view_preserves_reasoning_when_protocol_needs_it():
    model = DeepSeekChatOpenAI(model="deepseek-v4-pro", api_key="offline-test", extra_body={"thinking": {"type": "enabled"}})
    original = AIMessage(content="", additional_kwargs={"reasoning_content": "Keep for tool protocol"})
    assert model_history_view([original], model)[0].additional_kwargs == original.additional_kwargs


def test_feedback_diagnostic_identifies_bad_target_location():
    import pytest

    from cortex.capabilities.sql_training.training.assessment_models import (
        AssessmentFeedback,
    )
    payload = {"units": [], "attempts": [{"attempt": 1}],
               "fixed_targets": [{"target_id": "T1"}], "physical_context": [{"id": "column.orders.id"}],
               "related_knowledge": {"nodes": [], "edges": [], "prior_edges": []},
               "round_baseline": None, "prior_evidence": []}
    feedback = AssessmentFeedback.model_validate({"unit_results": [], "target_results": [{"target_id": "T1", "status": "unresolved", "feedback": [{"kind": "target", "location": "T1", "gap": "wrong projection", "required_change": "preserve requested content", "preserve": "population"}]}]})
    with pytest.raises(ValueError, match=r"target_results\[T1\].feedback\[0\].location: 'T1'") as error:
        feedback.validate_evidence(payload)
    assert "not a node/relation ID" in str(error.value)
    feedback.target_results[0].feedback[0].location = "column.orders.id"
    feedback.validate_evidence(payload)
