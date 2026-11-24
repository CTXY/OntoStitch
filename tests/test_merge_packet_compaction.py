from copy import deepcopy

from cortex.capabilities.graph_merge.llm.packets import (
    action_message,
    compact_node_packet,
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


def test_invalid_endpoint_feedback_names_actual_predicate_type_and_allowed_types():
    import pytest
    from graph_merge_fixtures import catalog, concept, edge, state

    from cortex.schema import GraphState, load_root_schema
    from cortex.schema.validation import validate_graph_state

    base = state([*catalog(), concept('left', 'a'), concept('right', 'b')])
    invalid = GraphState(graph=base.graph.model_copy(update={'edges': (*base.graph.edges, edge('wrong', 'left', 'right'))}))
    with pytest.raises(ValueError) as error:
        validate_graph_state(invalid, load_root_schema())
    message = str(error.value)
    assert '(grounded_on)' in message and 'right is concept' in message
    assert 'column' in message and 'dimension_value' in message
