"""Malformed tool recovery must preserve provider protocol and avoid partial commits."""
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_light_reconciliation import setup
from test_incremental_agent import Chat

from cortex.capabilities.graph_merge.integration.relation_context import relation_context
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent


def invalid(call_id='broken'):
    return {'name': 'submit_changes', 'args': '{"patch":', 'id': call_id, 'error': 'incomplete JSON', 'type': 'invalid_tool_call'}


class RecordingChat(Chat):
    def __init__(self, responses):
        super().__init__(responses)
        self.bindings = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))
        return super().bind_tools(tools, **kwargs)


def packet(ctx, base, a, b, phase='reconcile'):
    return relation_context(ctx, base, LocalTask('malformed-recovery', phase, {a, b}))


def test_malformed_only_receives_tool_reply_then_plain_json_finishes():
    ctx, base, a, b, _ = setup()
    model = RecordingChat([AIMessage(content='', invalid_tool_calls=[invalid()]),
                           AIMessage(content='{"explanation":"No change required."}')])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    assert len(model.requests) == 2
    replies = [m for m in model.requests[1] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in replies] == ['broken']
    assert model.bindings[-1][1]['tool_choice'] == 'auto'
    assert 'submit_changes' not in {getattr(t, 'name', '') for t in model.bindings[-1][0]}
    assert ctx.active_batch.preview == base


def test_mixed_malformed_and_valid_read_replies_to_every_id():
    ctx, base, a, b, _ = setup()
    model = RecordingChat([AIMessage(content='', invalid_tool_calls=[invalid()], tool_calls=[{
        'name': 'inspect_graph', 'id': 'read', 'args': {'mode': 'items', 'ids': [a], 'fields': ['name']}}]),
        AIMessage(content='{"explanation":"No change required."}')])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    replies = [m for m in model.requests[1] if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in replies} == {'broken', 'read'}
    assert json.loads(next(m.content for m in replies if m.tool_call_id == 'read'))['items'][0]['name']


def test_mixed_malformed_and_final_patch_does_not_apply_final_patch():
    ctx, base, a, b, e = setup()
    model = RecordingChat([AIMessage(content='', invalid_tool_calls=[invalid()], tool_calls=[{
        'name': 'submit_changes', 'id': 'final', 'args': {'patch': {
            'edge_updates': [{'id': e, 'properties': {'description': 'Should not be saved.'}}],
            'explanation': 'Bundled with an invalid call.'}}}]),
        AIMessage(content='{"explanation":"No change required."}')])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    assert {m.tool_call_id for m in model.requests[1] if isinstance(m, ToolMessage)} == {'broken', 'final'}
    assert ctx.active_batch.preview == base


def test_repeated_malformed_outputs_do_not_repeat_full_schema_feedback():
    ctx, base, a, b, _ = setup()
    model = RecordingChat([AIMessage(content='', invalid_tool_calls=[invalid('one')]),
        AIMessage(content='', invalid_tool_calls=[invalid('two')]),
        AIMessage(content='{"explanation":"No change required."}')])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    assert sum('Result schema:' in m.content for m in model.requests[-1] if isinstance(m, HumanMessage)) == 1
    assert {m.tool_call_id for m in model.requests[-1] if isinstance(m, ToolMessage)} == {'one', 'two'}


def test_assessment_malformed_only_recovers_to_review_not_patch():
    ctx, base, a, b, _ = setup()
    bad = {**invalid(), 'name': 'RelationReview'}
    model = RecordingChat([AIMessage(content='', invalid_tool_calls=[bad]),
                           AIMessage(content='{"verdict":"accept","issues":[],"issue_refs":[]}')])
    result = IncrementalIntegrationAgent(model, max_calls=None).invoke('graph_assess', ctx, packet(ctx, base, a, b, 'graph_assess'))
    assert result.verdict == 'accept' and len(model.requests) == 2
    notice = next(m.content for m in model.requests[1] if isinstance(m, HumanMessage) and 'Result schema:' in m.content)
    assert 'RelationReview' in notice and 'node_updates' not in notice


def test_fields_schema_gives_real_paths_without_silent_aliases():
    from langchain_core.utils.function_calling import convert_to_openai_tool
    ctx, base, a, b, _ = setup()
    model = RecordingChat([AIMessage(content='{"explanation":"No change required."}')])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    tool = next(t for t in model.bindings[0][0] if getattr(t, 'name', '') == 'inspect_graph')
    description = convert_to_openai_tool(tool)['function']['parameters']['properties']['fields']['description']
    assert 'properties.targets' in description and 'properties.definition' in description
    assert "whole 'properties' are not selectors" in description
    assert "['alternatives']" in description
    # An incorrect caller is still corrected explicitly, never silently remapped.
    result = tool.invoke({'mode': 'items', 'ids': [a], 'fields': ['targets']})
    assert result['read_errors'][0]['field'] == 'targets'
    assert 'properties.targets' in result['read_errors'][0]['available_fields']
