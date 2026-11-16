from copy import deepcopy

from cortex.capabilities.graph_merge.llm.packets import (
    action_message,
    compact_node_packet,
    reference_supplied_records,
    supplied_records,
)


def test_tool_dialogue_keeps_actions_but_does_not_echo_narration():
    from langchain_core.messages import AIMessage
    original = AIMessage(content="Long incidental analysis", tool_calls=[{
        "name": "inspect_graph", "args": {"ids": ["N"]}, "id": "read", "type": "tool_call",
    }])
    compact = action_message(original)
    assert compact.content == ""
    assert compact.tool_calls == original.tool_calls
    assert original.content == "Long incidental analysis"
    plain = AIMessage(content='{"groups":[],"distinctions":[]}')
    assert action_message(plain) is plain


def test_only_byte_equal_archived_records_share_current_representation():
    current = {"id": "N", "properties": {"targets": [["C"]]}, "sources": [{"source_id": "a"}]}
    different = {**current, "id": "M", "sources": [{"source_id": "b"}]}
    packet = {"nodes": [deepcopy(current), different], "candidate_nodes": [current],
              "relations": [], "candidate_relations": []}
    original = deepcopy(packet)
    compact = compact_node_packet(packet)
    assert compact["source_equals_candidate"]["nodes"] == ["N"]
    assert compact["nodes"] == [different]
    expanded = compact["nodes"] + [n for n in compact["candidate_nodes"] if n["id"] in compact["source_equals_candidate"]["nodes"]]
    assert sorted(expanded, key=lambda n: n["id"]) == sorted(packet["nodes"], key=lambda n: n["id"])
    assert packet == original


def test_different_versions_of_same_id_are_never_collapsed():
    source = {"id": "N", "properties": {"action": "one"}}
    current = {"id": "N", "properties": {"action": "two"}}
    compact = compact_node_packet({"nodes": [source], "candidate_nodes": [current]})
    assert compact["nodes"] == [source]
    assert compact["source_equals_candidate"]["nodes"] == []


def test_candidate_read_keeps_matching_signals_without_repeating_supplied_body():
    node = {"id": "N", "node_type": "concept", "properties": {"targets": [["C"]]}}
    result = {"pages": {"Q": {"items": [{"id": "N", "signals": ["same binding"], "node": node}]}}}
    compact = reference_supplied_records(result, supplied_records({"candidate_nodes": [node]}))
    item = compact["pages"]["Q"]["items"][0]
    assert item["signals"] == ["same binding"]
    assert item["node"] == {"id": "N", "already_supplied": True, "location": "candidate_nodes"}
    changed = {**node, "properties": {"targets": [["D"]]}}
    assert reference_supplied_records(changed, supplied_records({"candidate_nodes": [node]})) == changed


def test_invalid_endpoint_feedback_names_actual_predicate_type_and_allowed_types():
    import pytest
    from test_graph_integration import catalog, concept, edge, state

    from cortex.schema import GraphState, load_root_schema
    from cortex.schema.validation import validate_graph_state

    base = state([*catalog(), concept('left', 'a'), concept('right', 'b')])
    invalid = GraphState(graph=base.graph.model_copy(update={'edges': (*base.graph.edges, edge('wrong', 'left', 'right'))}))
    with pytest.raises(ValueError) as error:
        validate_graph_state(invalid, load_root_schema())
    message = str(error.value)
    assert '(grounded_on)' in message and 'right is concept' in message
    assert 'column' in message and 'dimension_value' in message


def test_relation_reads_share_prior_values_without_discarding_first_read():
    import json
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, ToolMessage
    from test_graph_integration import catalog, concept, state
    from test_incremental_agent import Chat

    from cortex.capabilities.graph_merge.integration.context import IntegrationContext
    from cortex.capabilities.graph_merge.llm.reconciliation import invoke_relation
    from cortex.schema import load_root_schema

    ctx = IntegrationContext(state(), state([*catalog(), concept('N', 'a')]), load_root_schema())
    ctx.candidate = ctx.union_state()
    ident = ctx.input_node_map['N']
    def read(call_id):
        return AIMessage(content='', tool_calls=[{'name': 'inspect_items', 'args': {'ids': [ident], 'fields': ['properties.targets']}, 'id': call_id, 'type': 'tool_call'}])
    model = Chat([read('first'), read('again'), AIMessage(content='{"verdict":"accept"}')])
    agent = SimpleNamespace(llm=model, trace_sink=None, call_budget=None, max_calls=None)
    invoke_relation(agent, 'graph_assess', ctx, {'nodes': [], 'edges': [], 'evidence': []})
    replies = [json.loads(m.content) for m in model.requests[-1] if isinstance(m, ToolMessage)]
    assert replies[0]['items'][0]['properties']['targets']
    assert replies[1]['already_supplied'] is True
    from cortex.capabilities.graph_merge.llm.reconciliation import RelationReview
    assert RelationReview in model.tools
    assert any(getattr(t, "name", None) == "inspect_graph" for t in model.tools)
