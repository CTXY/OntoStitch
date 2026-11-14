"""Business traversal stops at one hop without truncating executable references."""
from test_graph_integration import catalog, concept, edge, state
from cortex.schema import GraphNode
from cortex.capabilities.retrieve_db.graph.context import expand_context, build_retrieved_graph_state


def test_custom_business_relations_are_bidirectional_but_do_not_recurse():
    graph = state([*catalog(), concept('object', 'a'), concept('owner', 'b'), concept('email', 'c')],
                  [edge('one', 'object', 'owner', 'managed_by'), edge('two', 'owner', 'email', 'has_email')])
    selected = expand_context(graph, {'object'})
    assert {'object', 'owner', 'a', 'b', 'table'} <= selected
    assert 'email' not in selected and 'c' not in selected
    assert 'object' in expand_context(graph, {'owner'})


def test_required_physical_binding_does_not_expand_to_unselected_semantic_owner():
    metric = GraphNode(id='other', node_type='metric', name='total', properties={'expression': {'expr': 'SUM(${value})', 'refs': {'value': 'a'}}})
    graph = state([*catalog(), concept('first', 'a'), metric],
                  [edge('one', 'first', 'a'), edge('two', 'other', 'a')])
    assert 'other' not in expand_context(graph, {'first'})
    assert {'first', 'other'} <= expand_context(graph, {'a'})


def test_metric_reference_chain_is_retained_beyond_business_hop():
    metrics = [GraphNode(id=k, node_type='metric', name=k,
               properties={'expression': {'expr': '${value} + 1', 'refs': {'value': target}}})
               for k, target in [('outer', 'inner'), ('inner', 'operand')]]
    graph = state([*catalog(), concept('operand', 'a'), *metrics],
                  [edge('one', 'outer', 'inner', 'depends_on'), edge('two', 'inner', 'operand', 'depends_on')])
    refs = expand_context(graph, {'outer'})
    assert {'outer', 'inner', 'operand', 'a', 'table'} <= refs
    assert len(build_retrieved_graph_state(graph, refs).graph.nodes) == len(refs)


def test_public_contract_accepts_only_exact_relation_group_projection():
    import pytest
    from cortex.application.retrieve_db import RetrieveDbResult, RetrieveDbError, _validate_result
    from cortex.schema import load_root_schema
    b=concept('owner','b').model_copy(update={'properties':{'targets':[['b']], 'relation_metadata':[{'id':'group','edges':['two','three'],'description':'These categories can overlap.'}]}})
    original=state([*catalog(),concept('root','a'),b,concept('left','c'),concept('right','a')],
                   [edge('one','root','owner','has_feature'),edge('two','owner','left','has_feature'),edge('three','owner','right','has_feature')])
    projected=build_retrieved_graph_state(original,expand_context(original,{'root'}))
    raw=RetrieveDbResult(status='completed',question='request',graph_fingerprint='fingerprint',retrieved_graph_state=projected)
    validate=lambda r:_validate_result(r,original.graph,'request','fingerprint',load_root_schema())
    assert validate(raw).retrieved_graph_state==projected
    for field,value in [('name','Changed identity'),('description','Invented description')]:
        nodes=tuple(n.model_copy(update={field:value}) if n.id=='owner' else n for n in projected.graph.nodes)
        bad=projected.model_copy(update={'graph':projected.graph.model_copy(update={'nodes':nodes})})
        with pytest.raises(RetrieveDbError,match='modified or unknown node'):
            validate(raw.model_copy(update={'retrieved_graph_state':bad}))
