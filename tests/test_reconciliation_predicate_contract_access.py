"""Full task-local predicate contracts arrive before model decisions."""
from copy import deepcopy

import pytest

from test_reconciliation_context import runtime, node_context, wire
from cortex.schema import GraphEdge, load_root_schema


def with_contract(*, abstract_edge=False, legacy_edge=False):
    ctx = node_context()
    parent = load_root_schema().edge_type_index()['has_attribute']
    contract = parent.model_copy(update={'id': 'has_precise_feature', 'name': 'Precise feature',
        'description': 'The business object has the evidenced precise feature.',
        'extends': 'has_attribute', 'from_node_types': ['concept'], 'to_node_types': ['concept'], 'fields': {}})
    edges = list(ctx.task_before.graph.edges)
    if abstract_edge or legacy_edge:
        edges[0] = GraphEdge(id='e1', edge_type='related_to' if legacy_edge else 'has_attribute',
            from_node_id='N1', to_node_id='N2', properties={'description': 'Existing local fact.'})
    ctx.task_before = ctx.task_before.model_copy(update={'graph': ctx.task_before.graph.model_copy(update={
        'edges': tuple(edges), 'relation_contracts': (contract,)})})
    ctx.candidate = ctx.task_before
    return ctx, contract


def review(contract, *, field='direction_semantics'):
    reference = {'kind': 'contract', 'id': contract, 'view': 'candidate', 'field': field}
    return {'explanation':'A local predicate discrepancy remains.', 'feedback':[{
        'refs':[reference,{'kind':'edge','id':'e1','view':'candidate'}], 'problem':'The direction conflicts with the concrete assertion.',
        'required_outcome':'Preserve the concrete meaning.'}]}


def test_selected_effective_contract_is_complete_and_unchanged_body_has_one_copy():
    rt = runtime(phase='graph_assess')
    definition = rt.working['before']['contracts']['grounded_on']
    assert definition['authorable']
    assert definition['direction_semantics'] and definition['evidence_requirement']
    assert definition['fields']['description']['required']
    assert 'default' not in definition['fields']['description']
    row = wire(rt)['contracts']['grounded_on']
    assert row['content_change'] == 'unchanged' and row['before'] == definition


def test_unused_installed_contract_is_loaded_on_demand_without_expanding_write_scope():
    ctx, contract = with_contract()
    rt = runtime(ctx=ctx, signals=[{'id': 'S', 'kind': 'value_concepts', 'node_ids': ['N1']}])
    assert not any(edge.edge_type == contract.id for edge in ctx.task_before.graph.edges)
    assert contract.id not in rt.working['before'].get('contracts', {})
    prefix = rt.messages()[1]
    writable = (set(rt.write_node_ids), set(rt.write_edge_ids))
    result = rt.read_predicates(from_node_type='concept', to_node_type='concept')
    assert contract.id in result['predicate_ids']
    definition = rt.working['before']['contracts'][contract.id]
    assert definition['fields']['description']['required'] and definition['authorable']
    assert rt.messages()[1] == prefix
    assert contract.id in rt.messages()[2].content
    assert (rt.write_node_ids, rt.write_edge_ids) == writable
    assert 'request_context' not in rt.build_read_tools()


def test_abstract_fix_can_use_complete_installed_contract_without_an_extra_read():
    ctx, contract = with_contract(abstract_edge=True)
    rt = runtime(ctx=ctx, signals=[{'id': 'S', 'kind': 'abstract_predicate', 'edge_ids': ['e1']}])
    rt.set_draft({'task_id': 'task', 'before_id': rt.before_id, 'version': 1, 'changes': [{
        'change_id': 'repair', 'kind': 'edge_update', 'edge_id':'e1', 'set':{'edge_type':contract.id}, 'reason':'Use precise established business role.'}]})
    rt.validate_submission(rt.draft)
    assert rt.working['before']['contracts']['has_attribute']['authorable'] is False


def test_unknown_or_readonly_predicate_is_not_authorized_by_broad_read_visibility():
    ctx, _ = with_contract(legacy_edge=True)
    rt = runtime(ctx=ctx)
    assert rt.working['before']['contracts']['related_to']['authorable'] is False
    for predicate, message in [('not_installed', 'MISSING_PREDICATE_CONTRACT'), ('related_to', 'READONLY_PREDICATE')]:
        rt.set_draft({'task_id': 'task', 'before_id': rt.before_id, 'version': rt.draft.version + 1, 'changes': [{
            'change_id': 'rewrite', 'kind': 'edge_update', 'edge_id':'e1', 'set':{'edge_type':predicate},
                'reason':'Cannot bypass concrete authoring requirements.'}]})
        with pytest.raises(ValueError, match=message): rt.validate_submission(rt.draft)


def test_review_uses_only_exact_provided_contracts_and_public_fields():
    rt = runtime(phase='graph_assess')
    assert rt.validate_review(review('grounded_on', field='fields.description')).candidate_hash
    with pytest.raises(ValueError, match='UNKNOWN_PUBLIC_FIELD'):
        rt.validate_review(review('grounded_on', field='fields.not_declared'))
    with pytest.raises(ValueError, match='NONPUBLIC_REFERENCE_FIELD'):
        rt.validate_review(review('grounded_on', field='common_sources.source_id'))
    with pytest.raises(ValueError, match='UNPROVIDED_REFERENCE_OBJECT'):
        rt.validate_review(review('not_provided'))


def test_contract_facts_cannot_be_forged_through_recovery():
    rt = runtime()
    saved = deepcopy(rt.snapshot())
    saved['working']['before']['contracts']['grounded_on']['direction_semantics'] = 'forged'
    with pytest.raises(ValueError, match='facts.*differ'): runtime().restore(saved)
