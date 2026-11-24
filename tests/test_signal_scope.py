"""Exact signal sets replace generic one-hop graph expansion."""
from test_signal_premises import add, check, edge, node, setup

from cortex.capabilities.graph_merge.integration.signal_premises import signal_premise
from cortex.capabilities.graph_merge.integration.signal_scope import signal_scope


def test_triangle_omits_unrelated_core_and_boundary_adjacency_and_owners():
    ctx, state = setup([node('A', targets=[['P']]), node('B'), node('C'), node('Elsewhere'),
                        node('P', 'column'), node('UnrelatedOwner')], [
        edge('AB', 'A', 'B'), edge('BC', 'B', 'C'), edge('AC', 'A', 'C'),
        edge('Noise', 'A', 'Elsewhere', 'has_attribute'),
        edge('Owner', 'UnrelatedOwner', 'P', 'grounded_on')])
    signal = check('triangle', node_ids=['A', 'B', 'C'], edge_ids=['AB', 'BC', 'AC'])
    selected = signal_scope(ctx, state, [signal])
    assert selected.edge_ids == {'AB', 'BC', 'AC'}
    assert selected.node_ids == {'A', 'B', 'C', 'P'}
    assert selected.writable_edge_ids == selected.edge_ids
    assert selected.writable_node_ids == {'A', 'B', 'C'}
    assert signal_premise(ctx, state, signal) == signal_premise(ctx,
        add(state, nodes=[node('More')], edges=[edge('Unrelated', 'A', 'More')]), signal)
    assert signal_premise(ctx, state, signal) != signal_premise(ctx,
        add(state, edges=[edge('Relevant', 'A', 'B')]), signal)


def test_binding_includes_complete_claimants_and_only_between_claimant_relations():
    ctx, state = setup([node('A'), node('B'), node('P', 'column'), node('Hub')], [
        edge('AP', 'A', 'P', 'grounded_on'), edge('BP', 'B', 'P', 'grounded_on'),
        edge('AB', 'A', 'B'), edge('Noise', 'A', 'Hub', 'has_attribute')])
    signal = check('binding_contest', physical_id='P', node_ids=['A', 'B'], edge_ids=['AP', 'BP'])
    selected = signal_scope(ctx, state, signal)
    assert selected.edge_ids == {'AP', 'BP', 'AB'}
    assert selected.node_ids == {'A', 'B', 'P'}
    assert selected.writable_node_ids == {'A', 'B'}
    assert signal_premise(ctx, state, signal) == signal_premise(ctx,
        add(state, edges=[edge('AnotherNoise', 'B', 'Hub')]), signal)
    assert signal_premise(ctx, state, signal) != signal_premise(ctx,
        add(state, nodes=[node('NewClaimant')], edges=[edge('New', 'NewClaimant', 'P', 'grounded_on')]), signal)


def test_value_concepts_closes_column_values_owners_and_between_concept_relations():
    ctx, state = setup([node('Column', 'column'), node('V', 'dimension_value', column_ref='Column'),
        node('VC'), node('CC', targets=[['Column']]), node('Hub')], [
        edge('Binding', 'VC', 'V', 'grounded_on'), edge('ValueRole', 'VC', 'CC', 'value_of'),
        edge('Noise', 'VC', 'Hub', 'has_attribute')])
    signal = check('value_concepts', column_id='Column', members=[{'concept': 'VC', 'value': 'V'}])
    selected = signal_scope(ctx, state, signal)
    assert selected.edge_ids == {'Binding', 'ValueRole'}
    assert selected.node_ids == {'Column', 'V', 'VC', 'CC'}
    assert selected.writable_node_ids == {'VC', 'CC'}


def test_same_role_is_complete_for_the_nominated_predicate_only():
    ctx, state = setup([node('A'), node('B'), node('C'), node('D')], [
        edge('AB', 'A', 'B'), edge('AC', 'A', 'C'), edge('Noise', 'A', 'D', 'has_attribute')])
    signal = check('same_role_neighbors', edge_ids=['AB'])
    selected = signal_scope(ctx, state, signal)
    assert selected.edge_ids == {'AB', 'AC'}
    assert 'D' not in selected.node_ids
    assert 'C' in selected.node_ids and 'C' not in selected.writable_node_ids


def test_abstract_role_pair_does_not_import_other_attribute_roles():
    ctx, state = setup([node('A'), node('B'), node('C')], [
        edge('AB', 'A', 'B', 'has_attribute'), edge('OtherRole', 'A', 'C', 'has_attribute')])
    selected = signal_scope(ctx, state, check('abstract_predicate', edge_ids=['AB'], node_ids=['A', 'B']))
    assert selected.edge_ids == {'AB'}
    assert selected.node_ids == {'A', 'B'}
    assert 'has_attribute' in selected.contract_ids


def test_metadata_closes_only_impacted_groups_not_every_group_on_owner():
    ctx, state = setup([node('A'), node('B'), node('C'), node('D'), node('E'),
        node('Owner', relation_metadata=[{'id': 'coupled', 'edges': ['AB', 'BC'], 'description': 'paired'},
                                         {'id': 'unrelated', 'edges': ['DE', 'ED'], 'description': 'other'}])],
        [edge('AB', 'A', 'B'), edge('BC', 'B', 'C'), edge('DE', 'D', 'E'), edge('ED', 'E', 'D')])
    selected = signal_scope(ctx, state, check('parallel_assertions', edge_ids=['AB']))
    assert selected.edge_ids == {'AB', 'BC'}
    assert selected.node_ids == {'A', 'B', 'C', 'Owner'}
    assert 'Owner' in selected.writable_node_ids
    assert selected.metadata_only_node_ids == {'Owner'}
    assert 'C' not in selected.writable_node_ids


def test_explicit_metric_obligation_includes_all_direct_inputs_without_operand_neighbors():
    ctx, state = setup([node('M', 'metric', expression={'expression': 'A', 'refs': {'a': 'A'}}),
        node('A'), node('B'), node('P', 'column'), node('Noise')], [
        edge('MA', 'M', 'A', 'depends_on'), edge('MP', 'M', 'P', 'grounded_on'),
        edge('MB', 'M', 'B', 'has_attribute'), edge('ANoise', 'A', 'Noise')])
    selected = signal_scope(ctx, state, check('metric_direct_inputs', node_ids=['M']))
    assert selected.edge_ids == {'MA', 'MP'}
    assert selected.node_ids == {'M', 'A', 'P'}


def test_signal_union_does_not_promote_one_signals_boundary_to_other_core():
    ctx, state = setup([node('A'), node('B'), node('C'), node('D')], [
        edge('AB', 'A', 'B'), edge('BC', 'B', 'C'), edge('Noise', 'B', 'D', 'has_attribute')])
    one = check('parallel_assertions', edge_ids=['AB'])
    two = {**check('parallel_assertions', edge_ids=['BC']), 'id': 'another'}
    selected = signal_scope(ctx, state, [one, two])
    assert selected.edge_ids == {'AB', 'BC'}
    assert selected.node_ids == {'A', 'B', 'C'}
    assert all(q.get('description') for q in selected.queries)


def test_binding_after_receipt_keeps_nominated_former_owner_and_its_representation_successor():
    ctx, original = setup([node('School'), node('SchoolName'), node('Physical', 'column')], [
        edge('SchoolBinding', 'School', 'Physical', 'grounded_on'),
        edge('NameBinding', 'SchoolName', 'Physical', 'grounded_on')])
    signal = check('binding_contest', physical_id='Physical', node_ids=['School', 'SchoolName'],
                   edge_ids=['SchoolBinding', 'NameBinding'])
    # The contest no longer triggers, so accepted_signal_checks keeps the
    # explicit original check. Its after-premise must cover the real successor.
    after = original.model_copy(update={'graph': original.graph.model_copy(update={'edges': (
        original.graph.edges[0], edge('Representation', 'School', 'SchoolName', 'has_attribute'))})})
    selected = signal_scope(ctx, after, signal)
    assert 'Representation' in selected.edge_ids
    assert selected.writable_node_ids == {'School', 'SchoolName'}
    initial = signal_premise(ctx, after, signal)
    revised = after.model_copy(update={'graph': after.graph.model_copy(update={'edges': (
        after.graph.edges[0], edge('Representation', 'School', 'SchoolName', 'has_attribute',
                                   description='A materially changed representation condition.'))})})
    assert signal_premise(ctx, revised, signal) != initial
    without_successor = after.model_copy(update={'graph': after.graph.model_copy(update={'edges': (after.graph.edges[0],)})})
    assert signal_premise(ctx, without_successor, signal) != initial
    assert signal_premise(ctx, add(after, nodes=[node('Other')], edges=[edge('Noise', 'School', 'Other')]), signal) == initial
