"""Final caps include expansion, whole formulas, alternatives and JOIN columns."""
import asyncio
import json

import pytest

from cortex.application.ontology_graph import GraphManager
from cortex.application.retrieval import RetrievedRepresentation
from cortex.application.retrieve_db import RetrieveDbService, RetrievalCheckpointCompatibilityError
from cortex.capabilities.retrieve_db.engine import RetrieveDbCapability
from cortex.capabilities.retrieve_db.graph.context import select_context, expand_context, build_retrieved_graph_state
from cortex.schema import GraphNode, GraphEdge, GraphSpace, GraphState, load_root_schema
from graph_merge_fixtures import concept, edge, state
from test_unified_retrieval_workflow import source


def catalog(size):
    return [GraphNode(id='table', node_type='table', name='records', properties={'table_name': 'records'}),
        *[GraphNode(id=f'col{i}', node_type='column', name=f'c{i}',
            properties={'table_ref': 'table', 'column_name': f'c{i}'}) for i in range(size)]]


def validate_projection(graph, result):
    view = build_retrieved_graph_state(graph, result.node_ids)
    GraphManager(load_root_schema()).validate_state(view)
    return view


def test_all_semantic_seeds_together_are_capped_at_30():
    graph = state([*catalog(1), *[concept(f'n{i:02}', 'col0') for i in range(45)]])
    seeds = {f'n{i:02}' for i in range(45)}
    result = select_context(graph, seeds, ranked_seed_ids=sorted(seeds, reverse=True))
    assert result.semantic_nodes == 30 and result.physical_nodes == 2
    assert 'n44' in result.seed_ids and 'n00' not in result.seed_ids
    assert len(result.skipped) == 15
    validate_projection(graph, result)


def test_concepts_events_rules_and_metrics_share_one_semantic_budget():
    base = catalog(1)
    nodes = [concept('concept', 'col0'), GraphNode(id='event', node_type='event', name='event',
        properties={'definition': 'A documented observation occurs.'}),
        GraphNode(id='rule', node_type='generation_rule', name='rule',
            properties={'condition': 'Current task', 'action': 'Keep the stored value.', 'reason': 'Source instruction.',
                        'triggers': {'question': {'phrases': ['stored value']}, 'sql': {'columns': [], 'operations': []}}}),
        GraphNode(id='metric', node_type='metric', name='metric',
            properties={'expression': {'expr': 'SUM(${v})', 'refs': {'v': 'col0'}}})]
    graph = state([*base, *nodes], [edge('input', 'metric', 'col0')])
    result = select_context(graph, {node.id for node in nodes},
        ranked_seed_ids=['metric', 'rule', 'event', 'concept'], semantic_node_limit=3)
    assert result.semantic_nodes == 3 and result.seed_ids == {'metric', 'rule', 'event'}
    validate_projection(graph, result)


def test_initial_physical_seeds_and_one_hop_share_the_same_50_slots():
    graph = state(catalog(80))
    result = select_context(graph, {f'col{i}' for i in range(80)}, ranked_seed_ids=['col79'])
    assert result.physical_nodes == 50 and result.semantic_nodes == 0
    assert 'table' in result.node_ids and 'col79' in result.seed_ids
    assert len(result.node_ids) == 50
    validate_projection(graph, result)


def test_a_one_hop_hub_cannot_add_more_than_30_semantic_nodes():
    graph = state([*catalog(1), concept('root', 'col0'),
        *[concept(f'neighbor{i:02}', 'col0') for i in range(60)]],
        [edge(f'e{i}', 'root', f'neighbor{i:02}', 'has_feature') for i in range(60)])
    result = select_context(graph, {'root'})
    assert result.semantic_nodes == 30 and result.physical_nodes == 2
    assert result.seed_ids == {'root'}
    assert all(row['phase'] == 'one_hop' for row in result.skipped.values())
    validate_projection(graph, result)


def test_complete_formula_that_cannot_fit_is_skipped_not_truncated():
    metric = GraphNode(id='large', node_type='metric', name='sum',
        properties={'expression': {'expr': '${a}+${b}+${c}',
            'refs': {'a': 'col0', 'b': 'col1', 'c': 'col2'}}})
    graph = state([*catalog(3), metric, concept('small', 'col0')],
        [edge(f'e{i}', 'large', f'col{i}') for i in range(3)])
    result = select_context(graph, {'large', 'small'}, ranked_seed_ids=['large', 'small'], physical_node_limit=3)
    assert 'large' not in result.node_ids and 'small' in result.node_ids
    assert result.skipped['large']['candidate_physical_nodes'] == 4
    validate_projection(graph, result)


def test_dependency_closure_cannot_bypass_semantic_cap():
    terms = [concept(f'operand{i}', 'col0') for i in range(3)]
    metric = GraphNode(id='large', node_type='metric', name='sum', properties={'expression': {
        'expr': '${a}+${b}+${c}', 'refs': {k: f'operand{i}' for i,k in enumerate('abc')}}})
    graph = state([*catalog(1), *terms, metric], [edge(f'e{i}', 'large', term.id, 'depends_on') for i,term in enumerate(terms)])
    result = select_context(graph, {'large'}, semantic_node_limit=3)
    assert not result.node_ids and result.skipped['large']['candidate_semantic_nodes'] == 4


def test_a_complete_conflict_never_loses_one_branch_to_fit(source):
    seed = 'concept.amount'
    small = select_context(source, {seed}, physical_node_limit=2)
    assert not small.node_ids
    full = select_context(source, {seed}, physical_node_limit=3)
    node = next(n for n in validate_projection(source, full).graph.nodes if n.id == seed)
    assert node == next(n for n in source.graph.nodes if n.id == seed)
    assert {a.alternative_id for a in node.alternatives} == {'option_a', 'option_b'}


def test_one_hop_does_not_recurse_and_rejected_neighbors_stay_rejected():
    graph = state([*catalog(1), *[concept(n, 'col0') for n in ('root', 'neighbor', 'far', 'rejected')]],
        [edge('a', 'root', 'neighbor', 'has_feature'), edge('b', 'neighbor', 'far', 'has_feature'),
         edge('c', 'root', 'rejected', 'has_feature')])
    result = select_context(graph, {'root'}, excluded_node_ids={'rejected'})
    assert 'neighbor' in result.node_ids and not {'far', 'rejected'} & result.node_ids


def test_join_route_and_column_pairs_count_toward_physical_budget():
    nodes = []; edges = []
    for key in 'abc':
        nodes.extend([GraphNode(id=key, node_type='table', name=key, properties={'table_name': key}),
            GraphNode(id=key+'_id', node_type='column', name='id', properties={'table_ref': key, 'column_name': 'id'})])
    for left,right in [('a','b'),('b','c')]:
        edges.append(GraphEdge(id=left+right, edge_type='joins_with', from_node_id=left, to_node_id=right,
            properties={'description': 'Database key equality', 'column_pairs': [
                {'from_column_ref': left+'_id', 'to_column_ref': right+'_id'}]}))
    graph = GraphManager(load_root_schema()).prepare_state(GraphState(graph=GraphSpace(nodes=tuple(nodes),edges=tuple(edges))))[0]
    result = select_context(graph, {'a'}, physical_node_limit=5)
    assert result.physical_nodes <= 5 and 'c' not in result.node_ids
    validate_projection(graph, result)


def test_budget_does_not_modify_input_and_seed_order_is_reproducible():
    graph = state([*catalog(2), *[concept(f'n{i}', 'col0') for i in range(5)]])
    before = graph.model_dump_json()
    a = expand_context(graph, {'n1','n2','n3','n4'}, semantic_node_limit=2)
    b = expand_context(graph, {'n4','n3','n2','n1'}, semantic_node_limit=2)
    assert a == b and before == graph.model_dump_json()


@pytest.mark.parametrize('value', [0, -1, True, 3.5])
def test_invalid_caps_cannot_disable_guard(value):
    with pytest.raises(ValueError, match='positive integers'):
        select_context(state(catalog(1)), {'col0'}, semantic_node_limit=value)


def test_checkpoint_replay_keeps_the_cap_and_changed_cap_needs_new_identity(tmp_path):
    class Recall:
        limits = {'concept': 100}
        def __init__(self): self.calls = 0
        async def recall(self, index, *args):
            self.calls += 1
            return tuple(RetrievedRepresentation(document=d, score=1, channels=('question_exact',))
                for d in index.documents if d.representation_kind == 'canonical_node' and d.node_type == 'concept')
    graph = state([*catalog(1), *[concept(f'n{i:02}', 'col0') for i in range(40)]])
    recall = Recall()
    def engine(limit):
        return RetrieveDbCapability(typed_recall=recall, relevance_filter=False,
            checkpoint_root=tmp_path, semantic_node_limit=limit)
    first = engine(30)
    async def invoke(implementation):
        return await RetrieveDbService(implementation, schema=load_root_schema()).run(
            question='known concepts', graph_state=graph, dialect='sqlite', initial_sql=None, run_id='stable-budget-case')
    one = asyncio.run(invoke(first)); two = asyncio.run(invoke(first))
    assert one == two and recall.calls == 1
    assert len(one.relevant_semantic_node_ids) == len(one.selected_node_ids) == 30
    audit = json.loads(next(row.observation for row in one.trace if row.action == 'final_node_budget'))
    assert audit['semantic_nodes'] == 30 and len(audit['skipped_complete_units']) == 10
    changed = engine(29)
    assert first.context_fingerprint != changed.context_fingerprint
    with pytest.raises(RetrievalCheckpointCompatibilityError):
        asyncio.run(invoke(changed))
    assert recall.calls == 1
