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


def test_runtime_retains_invalid_draft_and_accepts_only_local_correction():
    ctx, base, a, b, e = setup()
    model = RecordingChat([
        AIMessage(content='', tool_calls=[{'name': 'submit_changes', 'id': 'bad', 'args': {'patch': {
            'edge_update': [{'id': e, 'properties': {'description': 'A clarified business feature.'}}],
            'explanation': 'Preserve the fact while clarifying its wording.'}}}]),
        AIMessage(content='', tool_calls=[{'name': 'submit_changes', 'id': 'fix', 'args': {
            'corrections': [{'op': 'move', 'from_path': '/edge_update', 'path': '/edge_updates'}]}}])])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    feedback = json.loads(next(m.content for m in model.requests[1] if isinstance(m, ToolMessage)))
    assert feedback['status'] == 'rejected_unapplied'
    assert 'error' not in feedback  # Do not duplicate structured diagnostics as a large raw error.
    assert feedback['errors'][0]['path'] == '/edge_update'
    assert 'edge_updates' in feedback['errors'][0]['details']['allowed_fields']
    assert next(x for x in ctx.active_batch.preview.graph.edges if x.id == e).properties['description'] == 'A clarified business feature.'
    assert ctx.active_batch.preview.graph.nodes == base.graph.nodes


def test_tool_and_plain_fallback_expose_same_submission_envelope():
    from langchain_core.utils.function_calling import convert_to_openai_tool
    ctx, base, a, b, _ = setup()
    model = RecordingChat([AIMessage(content='', invalid_tool_calls=[invalid()]),
                           AIMessage(content='{"patch":{"explanation":"No justified change."}}')])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, packet(ctx, base, a, b))
    tool = next(t for t in model.bindings[0][0] if getattr(t, 'name', '') == 'submit_changes')
    parameters = convert_to_openai_tool(tool)['function']['parameters']
    assert set(parameters['properties']) == {'patch', 'corrections'}
    notice = next(m.content for m in model.requests[1] if isinstance(m, HumanMessage) and 'Result schema:' in m.content)
    fallback = json.loads(notice.split('Result schema: ', 1)[1])
    assert set(fallback['properties']) == set(parameters['properties'])
    assert fallback['$defs']['NewEdge']['required'] == ['id', 'edge_type', 'from_node_id', 'to_node_id', 'properties', 'evidence_ids']
    assert 'add_evidence' not in fallback['$defs']['NewEdge']['properties']


def test_placeholder_failure_is_correctable_instead_of_ending_writer():
    ctx, base, a, b, _ = setup()
    model = RecordingChat([
        AIMessage(content='', tool_calls=[{'name':'submit_changes','id':'bad','args':{
            'patch':{'failure':'null','explanation':'No justified change.'}}}]),
        AIMessage(content='', tool_calls=[{'name':'submit_changes','id':'fix','args':{
            'corrections':[{'op':'replace','path':'/failure','value':None}]}}])])
    IncrementalIntegrationAgent(model,max_calls=None).invoke('reconcile',ctx,packet(ctx,base,a,b))
    feedback = json.loads(next(m.content for m in model.requests[1] if isinstance(m,ToolMessage)))
    assert feedback['status'] == 'rejected_unapplied'
    assert feedback['errors'][0]['path'] == '/failure'
    assert ctx.active_batch.preview == base


def test_structurally_rejected_tool_arguments_switch_to_same_plain_json_contract():
    ctx, base, a, b, e = setup()
    patch = {'edge_update': [{'id': e, 'properties': {'description': 'Precise feature relation.'}}], 'explanation': 'Clarify relation.'}
    model = RecordingChat([
        AIMessage(content='',tool_calls=[{'name':'submit_changes','id':key,'args':{'patch':patch}}])
        for key in ['first']
    ] + [AIMessage(content=json.dumps({'corrections':[{'op':'move','from_path':'/edge_update','path':'/edge_updates'}]}))])
    IncrementalIntegrationAgent(model,max_calls=None).invoke('reconcile',ctx,packet(ctx,base,a,b))
    assert len(model.requests) == 2
    assert model.bindings[-1][1]['tool_choice'] == 'auto'
    assert 'submit_changes' not in {getattr(t,'name','') for t in model.bindings[-1][0]}
    assert sum('Result schema:' in m.content for m in model.requests[-1] if isinstance(m,HumanMessage)) == 1
    assert next(x for x in ctx.active_batch.preview.graph.edges if x.id == e).properties['description'] == 'Precise feature relation.'


def test_repeated_invalid_result_after_fallback_exits_without_changing_graph():
    import pytest
    from cortex.capabilities.graph_merge.integration.relation_edits import RegionIncomplete
    ctx, base, a, b, _ = setup()
    args = {'patch':{'unknown':[], 'explanation':'Invalid field.'}}
    model = RecordingChat([
        AIMessage(content='',tool_calls=[{'name':'submit_changes','id':key,'args':args}])
        for key in ['first']
    ] + [AIMessage(content=json.dumps(args))])
    with pytest.raises(RegionIncomplete, match='No progress'):
        IncrementalIntegrationAgent(model,max_calls=None).invoke('reconcile',ctx,packet(ctx,base,a,b))
    assert ctx.active_batch.preview is None
    assert len(model.requests) == 2


def test_repeated_correction_does_not_insert_list_item_twice(monkeypatch):
    import pytest
    from cortex.capabilities.graph_merge.integration.relation_edits import RegionIncomplete
    import cortex.capabilities.graph_merge.llm.reconciliation as runtime
    original = runtime.ProposalRepair
    instances = []
    class ObservedRepair(original):
        def __init__(self):
            super().__init__()
            instances.append(self)
    monkeypatch.setattr(runtime,'ProposalRepair',ObservedRepair)
    ctx, base, a, b, e = setup()
    correction={'corrections':[{'op':'add','path':'/edge_updates/-','value':{'id':e,'properties':{'description':'Updated once.'}}}]}
    model=RecordingChat([
        AIMessage(content='',tool_calls=[{'name':'submit_changes','id':'seed','args':{'patch':{'edge_updates':[],'unexpected':True,'explanation':'Clarify.'}}}]),
        AIMessage(content=json.dumps(correction)),AIMessage(content=json.dumps(correction))])
    with pytest.raises(RegionIncomplete,match='No progress'):
        IncrementalIntegrationAgent(model,max_calls=None).invoke('reconcile',ctx,packet(ctx,base,a,b))
    assert len(instances[0].draft['edge_updates']) == 1
    assert ctx.active_batch.preview is None
