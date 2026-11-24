"""Conservative type filtering never guesses semantics or expands graph access."""
from copy import deepcopy

import pytest
from test_reconciliation_context import runtime, node_context
from cortex.capabilities.graph_merge.integration.reconciliation_contract import ReadPredicates
from cortex.schema import GraphNode, GraphEdge


def context_with_event_contract(*, event_in_graph=False, event_edge=False):
    ctx=node_context()
    from cortex.schema import load_root_schema
    parent=load_root_schema().edge_type_index()['has_attribute']
    contract=parent.model_copy(update={'id':'examines','name':'examines','description':'An examination event examines its subject.',
        'extends':None,'from_node_types':['event'],'to_node_types':['concept'],'fields':{}})
    nodes=list(ctx.task_before.graph.nodes);edges=list(ctx.task_before.graph.edges)
    if event_in_graph:
        nodes.append(GraphNode(id='E',node_type='event',name='Examination',properties={'definition':'A recorded examination.'}))
        edges.append(GraphEdge(id='event-link',edge_type='examines' if event_edge else 'is_a',from_node_id='E',to_node_id='N1',
            properties={'description':'The examination concerns this subject.'}))
    ctx.task_before=ctx.task_before.model_copy(update={'graph':ctx.task_before.graph.model_copy(update={
        'nodes':tuple(nodes),'edges':tuple(edges),'relation_contracts':(contract,)})})
    ctx.candidate=ctx.task_before
    return ctx


def test_no_hypothetical_event_type_or_unused_physical_predicate_in_first_bundle():
    rt=runtime(ctx=context_with_event_contract())
    assert 'examines' not in rt.working['before']['contracts']
    assert '### Predicate `examines`' not in rt.messages()[1].content
    assert 'joins_with' not in rt.working['before']['contracts']
    assert 'grounded_on' in rt.working['before']['contracts']


def test_unrelated_existing_event_relation_does_not_expand_a_signal_scope():
    rt=runtime(ctx=context_with_event_contract(event_in_graph=True,event_edge=True))
    assert 'event-link' not in rt.working['before']['edges']
    assert 'examines' not in rt.working['before']['contracts']
    rt = runtime(ctx=context_with_event_contract(event_in_graph=True,event_edge=True), signals=[{
        'id': 'event-pair', 'kind': 'parallel_assertions', 'edge_ids': ['event-link'], 'node_ids': ['E', 'N1']}])
    assert 'event-link' in rt.working['before']['edges']
    assert 'examines' in rt.working['before']['contracts']


def test_new_type_lookup_only_adds_schema_and_keeps_cached_prefix_and_write_scope():
    rt=runtime(ctx=context_with_event_contract());prefix=rt.messages()[:2]
    allowed=(set(rt.write_node_ids),set(rt.write_edge_ids),deepcopy(rt.visible_node_ids))
    result=rt.read_predicates(from_node_type='event',to_node_type='concept')
    assert result['status']=='merged' and 'examines' in result['predicate_ids']
    assert rt.working['before']['contracts']['examines']['direction_semantics']
    assert '### Predicate `examines`' in rt.messages()[2].content
    assert rt.messages()[:2]==prefix
    assert allowed==(rt.write_node_ids,rt.write_edge_ids,rt.visible_node_ids)
    old=rt.premise_hash()
    assert rt.read_predicates(from_node_type='event',to_node_type='concept')['status']=='already_provided'
    assert rt.premise_hash()==old


def test_query_and_full_contracts_restore_from_checkpoint_with_tamper_detection():
    rt=runtime(ctx=context_with_event_contract());rt.read_predicates(from_node_type='event',to_node_type='concept')
    saved=rt.snapshot();restored=runtime(ctx=context_with_event_contract());restored.restore(saved)
    assert restored.snapshot()==saved
    changed=deepcopy(saved);changed['working']['before']['contracts']['examines']['description']='forged meaning'
    with pytest.raises(ValueError,match='facts|permissions'): runtime(ctx=context_with_event_contract()).restore(changed)


def test_no_match_is_a_visible_read_fact_and_does_not_repeat_as_progress():
    rt=runtime(ctx=context_with_event_contract());old=rt.premise_hash()
    result=rt.read_predicates(from_node_type='event',to_node_type='metric')
    assert result['predicate_ids']==[] and result['status']=='merged'
    assert old!=rt.premise_hash() and 'no matching predicate' in rt.messages()[2].content
    current=rt.premise_hash()
    assert rt.read_predicates(from_node_type='event',to_node_type='metric')['status']=='already_provided'
    assert rt.premise_hash()==current


def test_assessment_has_full_candidate_contract_but_no_catalog_browsing_tool():
    ctx=context_with_event_contract();node=GraphNode(id='E',node_type='event',name='Examination',properties={'definition':'A recorded examination.'})
    ctx.candidate=ctx.task_before.model_copy(update={'graph':ctx.task_before.graph.model_copy(update={
        'nodes':(*ctx.task_before.graph.nodes,node),'edges':(*ctx.task_before.graph.edges,
        GraphEdge(id='new-event',edge_type='examines',from_node_id='E',to_node_id='N1',properties={'description':'The examination concerns its subject.'}))})})
    rt=runtime(ctx=ctx,phase='graph_assess')
    assert 'examines' in rt.working['candidate']['contracts']
    assert 'read_predicates' not in rt.available_read_tools(rt.build_read_tools())
    assert 'Predicate contract(not expanded before, not newly created or rewritten)' in rt.messages()[2].content
    with pytest.raises(ValueError,match='READONLY_ASSESSMENT'): rt.read_predicates(from_node_type='event',to_node_type='concept')


def test_catalog_reader_does_not_accept_physical_sources_or_graph_scan_arguments():
    with pytest.raises(ValueError): ReadPredicates(from_node_type='table',to_node_type='table')
    with pytest.raises(ValueError): ReadPredicates(from_node_type='event',to_node_type='concept',node_ids=['any'])


def test_signal_location_is_actual_input_not_example_or_guidance():
    ctx=node_context();ctx.task_before=ctx.task_before.model_copy(update={'graph':ctx.task_before.graph.model_copy(update={
        'edges':tuple(edge.model_copy(update={'edge_type':'has_attribute','to_node_id':'N2'}) if edge.id=='e1' else edge for edge in ctx.task_before.graph.edges)})});ctx.candidate=ctx.task_before
    rt=runtime(ctx=ctx,signals=[{'id':'abstract-specific-role','kind':'abstract_predicate','node_ids':['N1'],
        'edge_ids':['e1'],'observed':{'edge_id':'e1','abstract_predicate':'has_attribute','available_original_predicate':'has_sex'}}])
    text=rt.messages()[1].content
    assert 'not generic examples or guidance' in text and 'Matched relationship IDs' in text
    assert '相关对象：' not in text and '触发检查的已知事实：' not in text
    assert 'Additional candidate hint for this round' in text and 'Why detected this time' in text
    assert '"edge_id"' not in text.split('# 处理范围')[0]


def test_new_event_write_must_read_its_contract_without_redefining_installed_predicate():
    rt=runtime(ctx=context_with_event_contract())
    rt.set_draft({'task_id':rt.task_id,'before_id':rt.before_id,'version':1,'changes':[
        {'change_id':'event','kind':'node_create','node_id':'E','node_type':'event',
            'content':{'name':'Examination','description':'An examination event.','aliases':[],'tags':[],'properties':{'definition':'An examination event.'}},
            'evidence_ids':['ev-A'],'reason':'A local evidence-backed event role.'},
        {'change_id':'relation','kind':'edge_create','edge_id':'new-event','from_node_id':'E','to_node_id':'N1',
            'edge_type':'examines','properties':{'description':'The event examines the subject.'},
            'evidence_ids':['ev-A'],'reason':'Use the installed event relation.'}]})
    with pytest.raises(ValueError,match='MISSING_PREDICATE_CONTRACT.*read_predicates'): rt.validate_submission(rt.draft)
    rt.read_predicates(from_node_type='event',to_node_type='concept')
    assert rt.validate_submission(rt.draft)==rt.draft
