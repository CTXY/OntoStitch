"""Malformed output recovery preserves tool protocol and one draft vocabulary."""
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_light_reconciliation import setup
from test_incremental_agent import Chat

from cortex.capabilities.graph_merge.integration.relation_context import relation_context
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent


def invalid(call_id='broken'):
    return {'name': 'submit_patch', 'args': '{"explanation":', 'id': call_id,
            'error': 'incomplete JSON', 'type': 'invalid_tool_call'}


class RecordingChat(Chat):
    def __init__(self, responses):
        super().__init__(responses)
        self.bindings = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))
        return super().bind_tools(tools, **kwargs)


def packet(ctx, base, a, b, phase='reconcile'):
    return relation_context(ctx, base, LocalTask('malformed-recovery', phase, {a, b}))


def notices(request):
    return [m.content for m in request if isinstance(m, HumanMessage) and 'schema: ' in m.content]


def test_malformed_only_receives_tool_reply_then_plain_json_finishes():
    ctx, base, a, b, _ = setup()
    model = RecordingChat([AIMessage(content='', invalid_tool_calls=[invalid()]),
                           AIMessage(content='{"explanation":"No change required."}')])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    assert len(model.requests) == 2
    replies = [m for m in model.requests[1] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in replies] == ['broken']
    assert model.bindings[-1][1]['tool_choice'] == 'auto'
    assert 'submit_patch' not in {getattr(t, 'name', '') for t in model.bindings[-1][0]}
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
        'name': 'submit_patch', 'id': 'final', 'args': {
            'edge_updates': [{'id': e, 'properties': {'description': 'Should not be saved.'}}],
            'explanation': 'Bundled with an invalid call.'}}]),
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
    assert len(notices(model.requests[-1])) == 1
    assert {m.tool_call_id for m in model.requests[-1] if isinstance(m, ToolMessage)} == {'one', 'two'}


def test_assessment_malformed_only_recovers_to_review_not_patch():
    ctx, base, a, b, _ = setup()
    bad = {**invalid(), 'name': 'Review'}
    model = RecordingChat([AIMessage(content='', invalid_tool_calls=[bad]),
                           AIMessage(content='{"verdict":"accept","issues":[]}')])
    result = IncrementalIntegrationAgent(model, max_calls=None).invoke('graph_assess', ctx, packet(ctx, base, a, b, 'graph_assess'))
    assert result.verdict == 'accept' and len(model.requests) == 2
    notice = notices(model.requests[1])[0]
    assert 'Review' in notice and 'node_updates' not in notice


def test_fields_schema_gives_real_paths_without_silent_aliases():
    from langchain_core.utils.function_calling import convert_to_openai_tool
    ctx, base, a, b, _ = setup()
    model = RecordingChat([AIMessage(content='{"explanation":"No change required."}')])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    tool = next(t for t in model.bindings[0][0] if getattr(t, 'name', '') == 'inspect_graph')
    description = convert_to_openai_tool(tool)['function']['description']
    assert 'properties.targets' in description and 'properties.expression' in description
    assert 'whole properties are not selectors' in description and 'alternatives' in description
    result = tool.invoke({'mode': 'items', 'ids': [a], 'fields': ['targets']})
    assert result['read_errors'][0]['field'] == 'targets'
    assert 'properties.targets' in result['read_errors'][0]['available_fields']


def test_runtime_retains_invalid_raw_draft_for_inspection_and_full_replacement():
    ctx, base, a, b, e = setup()
    bad = {'edge_update': [{'id': e, 'properties': {'description': 'Clarified business feature.'}}],
           'explanation': 'Clarify its wording.'}
    fixed = {'edge_updates': bad['edge_update'], 'explanation': bad['explanation']}
    model = RecordingChat([
        AIMessage(content='', tool_calls=[{'name': 'submit_patch', 'id': 'bad', 'args': bad}]),
        AIMessage(content='', tool_calls=[{'name': 'inspect_draft', 'id': 'read', 'args': {}}]),
        AIMessage(content=json.dumps(fixed))])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    feedback = json.loads(next(m.content for m in model.requests[1] if isinstance(m, ToolMessage)))
    assert feedback['status'] == 'rejected_unapplied' and 'edge_update' in str(feedback)
    draft = json.loads(next(m.content for m in model.requests[2] if isinstance(m, ToolMessage) and m.tool_call_id == 'read'))
    assert draft['draft'] == bad
    assert next(x for x in ctx.active_batch.preview.graph.edges if x.id == e).properties['description'] == 'Clarified business feature.'
    assert ctx.active_batch.preview.graph.nodes == base.graph.nodes


def test_tool_and_plain_fallback_expose_the_same_direct_patch_schema():
    from langchain_core.utils.function_calling import convert_to_openai_tool
    ctx, base, a, b, _ = setup()
    model = RecordingChat([AIMessage(content='', invalid_tool_calls=[invalid()]),
                           AIMessage(content='{"explanation":"No justified change."}')])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    tool = next(t for t in model.bindings[0][0] if getattr(t, 'name', '') == 'submit_patch')
    parameters = convert_to_openai_tool(tool)['function']['parameters']
    assert 'patch' not in parameters['properties'] and 'corrections' not in parameters['properties']
    fallback = json.loads(notices(model.requests[1])[0].split('schema: ', 1)[1])
    assert set(fallback['properties']) == set(parameters['properties'])
    assert fallback['$defs']['NewEdge']['required'] == ['id', 'edge_type', 'from_node_id', 'to_node_id', 'properties', 'evidence_ids']
    assert 'add_evidence' not in fallback['$defs']['NewEdge']['properties']


def test_placeholder_failure_can_be_corrected_as_a_complete_draft():
    ctx, base, a, b, _ = setup()
    model = RecordingChat([
        AIMessage(content='', tool_calls=[{'name': 'submit_patch', 'id': 'bad', 'args': {
            'failure': 'null', 'explanation': 'No justified change.'}}]),
        AIMessage(content='{"failure":null,"explanation":"No justified change."}')])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    feedback = json.loads(next(m.content for m in model.requests[1] if isinstance(m, ToolMessage)))
    assert feedback['status'] == 'rejected_unapplied' and 'failure' in str(feedback)
    assert ctx.active_batch.preview == base


def test_structural_rejection_can_use_the_same_plain_json_patch_contract():
    ctx, base, a, b, e = setup()
    model = RecordingChat([
        AIMessage(content='', tool_calls=[{'name': 'submit_patch', 'id': 'bad', 'args': {
            'edge_update': [], 'explanation': 'Clarify relation.'}}]),
        AIMessage(content=json.dumps({'edge_updates': [{'id': e, 'properties': {'description': 'Precise feature relation.'}}],
                                     'explanation': 'Clarify relation.'}))])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    assert len(model.requests) == 2
    assert model.bindings[-1][1]['tool_choice'] == 'auto'
    assert len(notices(model.requests[-1])) == 1
    assert next(x for x in ctx.active_batch.preview.graph.edges if x.id == e).properties['description'] == 'Precise feature relation.'


def test_repeated_invalid_result_respects_configured_budget_without_committing():
    ctx, base, a, b, _ = setup()
    raw = {'unknown': [], 'explanation': 'Invalid field.'}
    model = RecordingChat([AIMessage(content='', tool_calls=[{'name': 'submit_patch', 'id': 'first', 'args': raw}]),
                           AIMessage(content=json.dumps(raw))])
    with pytest.raises(RuntimeError, match='configured local call budget'):
        IncrementalIntegrationAgent(model, max_calls=2).invoke('reconcile', ctx, packet(ctx, base, a, b))
    assert ctx.active_batch.preview is None and ctx.candidate == base
    assert len(model.requests) == 2
