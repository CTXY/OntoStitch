"""Full local bundles, bounded object reads and exact v2 recovery."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from test_node_local_context import context as node_context
from cortex.capabilities.graph_merge.llm.reconciliation_context import ReconciliationRuntime
from cortex.schema import GraphEdge, GraphNode, SourceReference


def runtime(*, phase='reconcile', signals=None, ctx=None, payload_extra=None):
    ctx = ctx or node_context()
    ctx.active_task = SimpleNamespace(id='task', targets={'N1', 'a', 'e1'}, feedback=[], checks=signals or [{
        'id': 'S', 'kind': 'parallel_assertions', 'node_ids': ['N1'], 'edge_ids': ['e1'],
        'observed': {'explanation': 'Examine only the nominated relation.'}}])
    payload = {'task_id': 'task', 'nodes': [{'id': 'N1'}, {'id': 'a'}],
        'edges': [{'id': 'e1'}], **(payload_extra or {})}
    return ReconciliationRuntime(ctx, payload, phase, system='fixed policy')


def wire(rt):
    return rt._wire_working()


def test_complete_first_bundle_and_only_full_object_tools():
    rt = runtime()
    assert len(rt.messages()) == 3
    assert set(rt.build_read_tools()) == {'read_nodes', 'read_relations', 'read_evidence','read_predicates'}
    assert set(rt.build_read_tools()['read_nodes'].args_schema.model_fields) == {'ids'}
    assert set(rt.build_read_tools()['read_relations'].args_schema.model_fields) == {'node_ids'}
    bundle = wire(rt)
    node = bundle['nodes']['N1']['before']
    assert node['properties']['targets'] == [['a']] and node['content_complete']
    assert bundle['bundle_ready'] and all(row['coverage']['complete'] for row in bundle['coverage'])
    assert 'available_fields' not in str(bundle) and 'same_as' not in str(bundle)
    assert rt.evidence_view == {'bodies': {}, 'id_to_body': {}}
    assert rt.allowed_evidence_ids == {'ev-A'}
    assert set(rt.available_read_tools(rt.build_read_tools())) == {'read_evidence','read_predicates'}


def test_graph_read_availability_requires_actual_missing_material():
    rt = runtime()
    reads = rt.build_read_tools()
    rt.working['before']['nodes']['N1']['content_complete'] = False
    assert 'read_nodes' in rt.available_read_tools(reads)
    rt.read_nodes(ids=['N1'])
    assert 'read_nodes' not in rt.available_read_tools(reads)
    rt.coverage = []
    assert 'read_relations' in rt.available_read_tools(reads)
    rt.read_relations(node_ids=sorted(rt._relation_anchors))
    assert 'read_relations' not in rt.available_read_tools(reads)
    for item in rt.coverage:
        item['coverage']['snapshot'] = 'a-different-snapshot'
    assert 'read_relations' in rt.available_read_tools(reads)


def test_reading_already_complete_material_adds_no_new_fact_or_message_history():
    rt = runtime()
    before, prefix = rt.premise_hash(), rt.messages()[:2]
    assert rt.read_nodes(ids=['N1'])['status'] == 'already_provided'
    assert rt.read_relations(node_ids=['N1'])['status'] == 'already_provided'
    assert rt.premise_hash() == before and rt.messages()[:2] == prefix
    assert len(rt.messages()) == 3


def test_old_fields_versions_and_mixed_router_are_not_public_backdoors():
    rt = runtime()
    with pytest.raises(TypeError): rt.read_nodes(ids=['N1'], fields=['name'])
    with pytest.raises(TypeError): rt.read_nodes(ids=['N1'], view='candidate')
    assert not hasattr(rt, 'request_context')


def test_failed_multi_object_read_is_atomic():
    rt = runtime()
    before = rt.snapshot()
    with pytest.raises(ValueError, match='OUT_OF_SCOPE'):
        rt.read_nodes(ids=['N1', 'outside'])
    assert rt.snapshot() == before


def test_evidence_is_deduplicated_and_preserves_existing_errors():
    ctx = node_context()
    ctx.evidence.append(SimpleNamespace(id='ev-B', source=SourceReference(source_id='case-A'),
        content=deepcopy(ctx.evidence[0].content)))
    rt = runtime(ctx=ctx)
    rt.set_feedback({'mechanical': [{'code': 'BAD_DRAFT'}]})
    rt.read_evidence(ids=['ev-A', 'ev-B'])
    assert len(rt.evidence_view['bodies']) == 1
    before = rt.premise_hash()
    rt.read_evidence(ids=['ev-B'])
    assert rt.premise_hash() == before and rt.feedback['mechanical'] == [{'code': 'BAD_DRAFT'}]
    assert rt.messages()[1] == runtime(ctx=ctx).messages()[1]
    with pytest.raises(ValueError, match='OUT_OF_SCOPE'): rt.read_evidence(ids=['private'])


def test_unrelated_hub_relations_are_excluded_even_without_pagination():
    ctx = node_context()
    nodes, edges = list(ctx.task_before.graph.nodes), list(ctx.task_before.graph.edges)
    for number in range(35):
        ident = f'neighbor-{number:02}'
        nodes.append(GraphNode(id=ident, node_type='concept', name=ident, properties={'targets': [['a']]}))
        edges.append(GraphEdge(id=f'relation-{number:02}', edge_type='is_a', from_node_id='N1', to_node_id=ident,
            properties={'description': 'Known local relationship.'}))
    ctx.task_before = ctx.task_before.model_copy(update={'graph': ctx.task_before.graph.model_copy(update={
        'nodes': tuple(nodes), 'edges': tuple(edges)})})
    ctx.candidate = ctx.task_before
    rt = runtime(ctx=ctx)
    assert set(rt.working['before']['edges']) == {'e1'}
    before = rt.premise_hash()
    assert rt.read_relations(node_ids=['N1'])['complete']
    assert rt.premise_hash() == before
    with pytest.raises(ValueError, match='recursive'): rt.read_relations(node_ids=['neighbor-00'])
    with pytest.raises(ValueError, match='OUT_OF_SCOPE'):
        rt.read_nodes(ids=['neighbor-00'])


def test_source_graphs_are_not_readable_and_current_ids_remain_authoritative():
    ctx=node_context()
    current=next(n for n in ctx.task_before.graph.nodes if n.id=='N1')
    ctx.raw_nodes={'N1':current.model_copy(update={'name':'Historical meaning'})}
    ctx.raw_edges={}
    rt=runtime(ctx=ctx)
    assert not hasattr(rt,'source_snapshot')
    assert 'source_handles' not in rt.working and 'source' not in rt.working
    assert rt.working['before']['nodes']['N1']['name']!='Historical meaning'
    with pytest.raises(ValueError,match='OUT_OF_SCOPE'):rt.read_nodes(ids=['source:invented'])



def test_unchanged_node_content_does_not_hide_actual_relation_changes():
    ctx = node_context()
    old = ctx.candidate
    changed = tuple(edge.model_copy(update={'properties': {**edge.properties, 'description': 'Clarified relation'}})
                    if edge.id == 'e1' else edge for edge in old.graph.edges)
    ctx.candidate = old.model_copy(update={'graph': old.graph.model_copy(update={'edges': changed})})
    rt = runtime(ctx=ctx, phase='graph_assess')
    bundle = wire(rt)
    assert bundle['nodes']['N1']['content_change'] == 'unchanged'
    assert bundle['nodes']['N1']['before']['properties']['targets'] == [['a']]
    assert bundle['relations']['e1']['content_change'] == 'modified'
    assert 'e1' in bundle['actual_changes']['edges']['updated']
    assert 'same_as' not in json.dumps(bundle)


def test_optional_absence_and_removed_fields_are_not_source_graph_reads():
    rt=runtime()
    assert 'relation_metadata' not in rt.packet['node_contracts'][0]['properties']
    assert 'source_snapshot' not in rt.build_read_tools()
    assert 'source_units' not in rt.working['before']['nodes']['N1']



def test_obligations_are_not_extra_signals_but_keep_exact_decision_ids():
    rt = runtime(signals=[{'id': 'O', 'kind': 'metric_direct_inputs', 'node_ids': ['N1'],
        'observed': {'missing': []}}])
    assert rt.packet['signals'] == []
    assert [row['id'] for row in rt.packet['obligations']] == ['O']
    assert rt.signal_ids == {'O'}


def test_recovery_rebuilds_facts_and_rejects_tampering():
    rt = runtime()
    rt.read_evidence(ids=['ev-A'])
    rt.set_feedback({'mechanical': [{'code': 'BAD_DRAFT'}], 'protocol_error': {'code': 'BAD_JSON'}})
    saved = rt.snapshot()
    fresh = runtime()
    fresh.restore(saved)
    assert fresh.messages() == rt.messages()
    saved['working']['before']['nodes']['N1']['name'] = 'forged'
    with pytest.raises(ValueError, match='facts.*differ'): runtime().restore(saved)


def test_new_durable_draft_head_wins_over_old_read_checkpoint():
    rt = runtime()
    rt.set_feedback({'mechanical': [{'code': 'OLD_ERROR'}]})
    saved = rt.snapshot()
    ctx = node_context()
    ctx.active_draft = {'task_id': 'task', 'before_id': rt.before_id, 'version': 1}
    fresh = runtime(ctx=ctx)
    fresh.restore(saved)
    assert fresh.draft.version == 1
    assert fresh.feedback['historical_draft_feedback']['mechanical'][0]['code'] == 'OLD_ERROR'


def test_changed_property_is_not_hidden_by_equal_summary():
    ctx = node_context()
    old = next(node for node in ctx.candidate.graph.nodes if node.id == 'N1')
    changed = old.model_copy(update={'properties': {**old.properties, 'targets': [['b']]}})
    ctx.candidate = ctx.candidate.model_copy(update={'graph': ctx.candidate.graph.model_copy(update={
        'nodes': tuple(changed if node.id == 'N1' else node for node in ctx.candidate.graph.nodes)})})
    bundle = wire(runtime(ctx=ctx, phase='graph_assess'))
    assert bundle['nodes']['N1']['content_change'] == 'modified'
    assert bundle['nodes']['N1']['after']['properties']['targets'] == [['b']]


def feedback(refs):
    return {'explanation':'A local claim needs verification.','feedback':[{
        'problem':'A concrete local issue.','required_outcome':'Retain the supported original assertion.','refs':refs}]}


def test_displayed_evidence_ids_are_valid_feedback_references():
    rt=runtime(phase='graph_assess')
    result=rt.validate_review(feedback([{'kind':'edge','id':'e1','view':'candidate','field':'evidence_ids'}]))
    assert result.issues[0].refs[0].fields==('evidence_ids',)
    assert result.verdict=='revise'
    for field in ('sources','reference_origin','properties._private'):
        with pytest.raises(ValueError,match='NONPUBLIC_REFERENCE_FIELD'):
            rt.validate_review(feedback([{'kind':'edge','id':'e1','view':'candidate','field':field}]))


def test_false_missing_edge_is_refuted_by_authoritative_candidate():
    rt=runtime(phase='graph_assess')
    edge=next(e for e in rt._candidate.graph.edges if e.id=='e1')
    with pytest.raises(ValueError,match='contradicted'):
        rt.validate_review(feedback([{'kind':'missing_relation','view':'candidate',
            'from_node_id':edge.from_node_id,'edge_type':edge.edge_type,'to_node_id':edge.to_node_id}]))
    saved=rt.snapshot()
    restored=runtime(phase='graph_assess');restored.restore(saved)
    assert restored.snapshot()==saved


def test_compile_supplement_does_not_repeat_unchanged_node_bodies():
    rt=runtime()
    initial=rt.messages()[1].content
    rt.set_compiled_candidate(rt.before,rt.draft.version)
    state=rt.messages()[2].content
    assert 'properties' in initial
    assert 'targets' not in state and 'unchanged' not in state
    assert rt.messages()[1].content==initial


def test_model_feedback_omits_archived_graphs_but_checkpoint_keeps_them():
    rt=runtime()
    rt.set_feedback({'semantic_issue_basis':[{'record':{'large_graph':'full historic graph'}}],
        'semantic_issues':[{'problem':'Retain the definition','required_outcome':'Clarify it',
            'refs':[{'kind':'node','view':'candidate','id':'N1','fields':['properties.definition']}]}]})
    public=rt._public_feedback()
    assert 'full historic graph' not in rt.messages()[-1].content
    assert 'semantic_issue_basis' not in public
    assert public['previous_feedback'][0]['refs'][0]['field']=='properties.definition'
    assert rt.snapshot()['feedback']['semantic_issue_basis']


def test_abstract_assessment_restore_keeps_unused_authorized_predicate_contracts():
    signals=[{'id':'abstract','kind':'abstract_predicate','node_ids':['N1'],
        'edge_ids':['e1'],'observed':'Check the nominated relationship role.'}]
    def isolated_context():
        ctx=node_context()
        ctx.task_before=ctx.task_before.model_copy(update={'graph':ctx.task_before.graph.model_copy(update={
            'edges':tuple(e.model_copy(update={'edge_type':'has_attribute','to_node_id':'N2'}) if e.id=='e1' else e
                          for e in ctx.task_before.graph.edges if e.id!='incoming')})})
        ctx.candidate=ctx.task_before
        return ctx
    original=runtime(phase='graph_assess',signals=signals,ctx=isolated_context())
    contracts=set(original.working['candidate'].get('contracts',{}))
    used={e['edge_type'] for e in original.working['candidate']['edges'].values()}
    assert contracts-used  # Authorized comparison contracts, not only live predicates.
    saved=original.snapshot()
    restored=runtime(phase='graph_assess',signals=signals,ctx=isolated_context())
    restored.restore(saved)
    assert restored.snapshot()==saved
