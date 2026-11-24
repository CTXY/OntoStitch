"""Narrow node contracts preserve relations and escalate semantic obligations."""

import pytest

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.consolidation.relations import (
    NodeRelationCoordinationRequired,
    sync_node_relations,
)
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState, SourceReference


def state(nodes, edges=()):
    return GraphState(graph=GraphSpace(nodes=tuple(nodes), edges=tuple(edges)))


def column(key):
    return GraphNode(id=key, node_type='column', name=key,
        properties={'table_ref': 'table', 'column_name': key})


def concept(key, **properties):
    return GraphNode(id=key, node_type='concept', name=key,
        properties={'targets': [['col']], **properties})


def metric(key='metric', operand='col'):
    return GraphNode(id=key, node_type='metric', name=key,
        properties={'expression': {'expr': 'COUNT(${x})', 'refs': {'x': operand}}})


def edge(key, source='metric', target='col', kind='grounded_on', **properties):
    return GraphEdge(id=key, edge_type=kind, from_node_id=source, to_node_id=target,
        properties={'description': 'Authored relation.', **properties})


def sync(base, integrated=None, **kwargs):
    return sync_node_relations(base, integrated or base,
        node_aliases=kwargs.get('node_aliases', {}),
        edge_aliases=kwargs.get('edge_aliases', {}), owner_ids=kwargs.get('owner_ids', ['metric']))


def test_valid_metric_does_not_rewrite_or_create_relations():
    original = state([column('col'), metric()], [edge('e')])
    result, aliases, changes = sync(original)
    assert result == original
    assert aliases == {'e': 'e'}
    assert changes == ()


def test_confirmed_operand_alias_retargets_formula_and_edge_together():
    original = state([column('old'), metric(operand='old')], [edge('e', target='old')])
    integrated = state([column('new'), metric(operand='old')], [edge('e', target='old')])
    result, _, changes = sync(original, integrated, node_aliases={'old': 'new'})
    node = next(n for n in result.graph.nodes if n.id == 'metric')
    assert node.properties['expression']['refs'] == {'x': 'new'}
    assert result.graph.edges[0].to_node_id == 'new'
    assert len(changes) == 2
    assert original.graph.edges[0].to_node_id == 'old'


def test_undecided_operand_does_not_silently_drop_or_invent_dependencies():
    original = state([column('col'), concept('A'), concept('B'), metric(operand='A')],
        [edge('source-relation', target='A', kind='depends_on')])
    nodes = [metric(operand='B') if n.id == 'metric' else n for n in original.graph.nodes]
    draft = state(nodes, original.graph.edges)
    with pytest.raises(NodeRelationCoordinationRequired) as caught:
        sync(original, draft)
    error = caught.value
    assert error.integrated_state == draft
    assert error.diagnostics[0]['missing'] == [{'edge_type': 'depends_on', 'to_node_id': 'B'}]
    assert error.diagnostics[0]['stale'] == [{'edge_type': 'depends_on', 'to_node_id': 'A'}]
    assert error.followups[0]['edge_ids'] == ['source-relation']
    assert len(error.integrated_state.graph.edges) == 1
    assert error.integrated_state.graph.edges[0].to_node_id == 'A'
    assert error.details['code'] == 'node_relation_coordination_required'


def test_conflict_metric_checks_each_branch_without_broadcasting_edges():
    ordinary = metric(operand='A')
    branches = [('a', ordinary.model_dump(include={'name','description','aliases','tags','properties'}), ()),
                ('b', metric(operand='B').model_dump(include={'name','description','aliases','tags','properties'}), ())]
    from cortex.schema import NodeContent
    conflict, _ = GraphManager.compact_conflict(ordinary,
        [(key, NodeContent.model_validate(content), sources) for key,content,sources in branches])
    original = state([column('col'), concept('A'), concept('B'), conflict],
        [edge('ea', target='A', kind='depends_on', alternative_ids=['a']),
         edge('eb', target='B', kind='depends_on', alternative_ids=['b'])])
    result, _, changes = sync(original)
    assert result == original and changes == ()
    broken = state(original.graph.nodes,
        [original.graph.edges[0].model_copy(update={'properties': {'description':'Authored relation.'}}),
         original.graph.edges[1]])
    with pytest.raises(NodeRelationCoordinationRequired) as caught:
        sync(original, broken)
    assert any(row['alternative_id']=='b' and row['stale'] for row in caught.value.diagnostics)
    assert caught.value.integrated_state.graph.edges[0].properties.get('alternative_ids') is None


def test_migrated_combination_text_survives_node_relation_sync():
    from cortex.capabilities.graph_merge.integration.semantic_notes import migrate_relation_notes
    original=state([column('col'),concept('owner'),concept('A'),concept('B')],
        [edge('e1',source='owner',target='A',kind='has_property'),
         edge('e2',source='owner',target='B',kind='has_property')])
    raw=original.model_dump(mode='json')
    next(n for n in raw['graph']['nodes'] if n['id']=='owner')['properties']['relation_metadata']=[
        {'id':'pair','edges':['e1','e2'],'description':'Two authored roles form one combination.'}]
    migrated,_=migrate_relation_notes(raw)
    result,_,_=sync(migrated,owner_ids=['owner'])
    owner=next(n for n in result.graph.nodes if n.id=='owner')
    assert 'Two authored roles form one combination.' in owner.properties['definition']
    assert 'relation_metadata' not in owner.properties


def test_exact_dedup_preserves_sources_and_migrated_business_meaning():
    from cortex.capabilities.graph_merge.integration.semantic_notes import migrate_relation_notes
    first=edge('e1',source='owner',target='A',kind='has_property').model_copy(update={'sources':(SourceReference(source_id='case-A'),)})
    second=first.model_copy(update={'id':'e2','sources':(SourceReference(source_id='case-B'),)})
    original=state([column('col'),concept('owner'),concept('A')],[first,second]).model_dump(mode='json')
    next(n for n in original['graph']['nodes'] if n['id']=='owner')['properties']['relation_metadata']=[
        {'id':'pair','edges':['e1','e2'],'description':'An authored statement must survive.'}]
    migrated,_=migrate_relation_notes(original)
    result,aliases,_=sync(migrated,owner_ids=['owner'])
    assert len(result.graph.edges)==1 and aliases['e2']=='e1'
    assert {s.source_id for s in result.graph.edges[0].sources}=={'case-A','case-B'}
    assert 'An authored statement must survive.' in next(n for n in result.graph.nodes if n.id=='owner').properties['definition']


def test_matching_endpoints_with_different_meaning_are_not_deduplicated():
    original = state([column('col'), concept('owner'),concept('A')],
        [edge('e1',source='owner',target='A',kind='has_property'),
         edge('e2',source='owner',target='A',kind='has_property',description='A distinct authored role.')])
    result, _, changes = sync(original, owner_ids=['owner'])
    assert len(result.graph.edges) == 2 and changes == ()


def test_missing_operand_identity_stays_a_node_error_not_relation_handoff():
    draft = state([column('col'), metric(operand='unseen-operand')], [edge('e')])
    with pytest.raises(ValueError, match='unknown operand') as caught:
        sync(draft)
    assert not isinstance(caught.value, NodeRelationCoordinationRequired)


def conflict_concept(owner='owner', *, second='A second supported definition.'):
    from cortex.schema import NodeContent
    first=concept(owner,definition='The original supported definition.')
    other=first.model_copy(update={'properties':{**first.properties,'definition':second}})
    def content(node):
        return NodeContent.model_validate(node.model_dump(mode='json',include={'name','description','aliases','tags','properties'}))
    result,_=GraphManager.compact_conflict(first,[('first',content(first),()),('second',content(other),())])
    return result


def test_controller_routes_incoming_target_conflict_without_exposing_incoming_to_writer():
    old=concept('old-target',definition='The original supported definition.')
    source=concept('source')
    before=state([column('col'),source,old],[edge('incoming',source='source',target='old-target',kind='has_property')])
    merged=conflict_concept()
    after=state([column('col'),source,merged],[edge('incoming',source='source',target='owner',kind='has_property')])
    with pytest.raises(NodeRelationCoordinationRequired) as caught:
        sync(before,after,node_aliases={'old-target':'owner'},owner_ids=['owner'])
    error=caught.value
    row=next(row for row in error.diagnostics if row['code']=='incoming_target_conflict_scope')
    assert row['original_edge_id']=='incoming' and row['before_target_id']=='old-target'
    assert row['target_owner_id']=='owner'
    assert row['before_target']['content']['properties']['definition']=='The original supported definition.'
    assert len(row['candidate_target']['alternatives'])==2
    assert 'source alternative_ids' in row['required_outcome']
    assert error.followups[0]['edge_ids']==['incoming']
    assert set(error.owner_ids)=={'owner','source'}
    assert 'alternative_ids' not in error.integrated_state.graph.edges[0].properties


def test_unchanged_existing_conflict_target_does_not_reopen_coordination():
    target=conflict_concept()
    original=state([column('col'),concept('source'),target],
        [edge('incoming',source='source',target='owner',kind='has_property')])
    result,_,changes=sync(original,owner_ids=['source'])
    assert result==original and changes==()


def test_exact_same_target_versions_with_confirmed_owner_alias_can_inherit():
    from cortex.capabilities.graph_merge.inputs import remap_node
    target=conflict_concept('old-target')
    before=state([column('col'),concept('source'),target],
        [edge('incoming',source='source',target='old-target',kind='has_property')])
    known={'old-target':'owner'}
    after=state([column('col'),concept('source'),remap_node(target,known)],
        [edge('incoming',source='source',target='owner',kind='has_property')])
    result,_,changes=sync(before,after,node_aliases=known,owner_ids=['owner'])
    assert result==after and changes==()


def test_changed_existing_conflict_target_requires_incoming_version_review():
    old=conflict_concept()
    newer=conflict_concept(second='A substantively changed supported definition.')
    before=state([column('col'),concept('source'),old],
        [edge('incoming',source='source',target='owner',kind='has_property')])
    after=state([column('col'),concept('source'),newer],before.graph.edges)
    with pytest.raises(NodeRelationCoordinationRequired) as caught:
        sync(before,after,owner_ids=['owner'])
    assert caught.value.diagnostics[0]['code']=='incoming_target_conflict_scope'
