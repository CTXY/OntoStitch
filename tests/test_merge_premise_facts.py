"""Premise versions follow visible facts, not call IDs or context formatting."""
from copy import deepcopy

import pytest

from cortex.capabilities.graph_merge.llm.packets import (
    compact_node_packet,
    premise_fingerprint,
)


def fingerprint(payload=None, reads=(), policy='fixed-policy'):
    return premise_fingerprint(payload or {}, reads, policy=policy)


def read(items, *, view='candidate', name='inspect_graph', mode='items', **extra):
    return {'name': name, 'args': {'mode': mode, 'ids': ['N'], 'view': view},
            'result': {'view': view, 'items': items, **extra}}


NODE = {'id': 'N', 'name': 'Exact business meaning', 'properties': {'definition': 'Original meaning', 'id': 'real property'}}


def test_packet_covered_projected_reread_does_not_change_premises():
    packet = {'nodes': [NODE], 'view': 'candidate'}
    query = read([{'id': 'N', 'properties': {'definition': 'Original meaning'}}])
    assert fingerprint(packet) == fingerprint(packet, [query])


def test_node_content_wrapper_matches_the_same_packet_business_fields():
    packet = {'nodes': [NODE], 'view': 'candidate'}
    query = read([{'id': 'N', 'content': {k: v for k, v in NODE.items() if k != 'id'}}])
    assert fingerprint(packet) == fingerprint(packet, [query])


def test_same_facts_ignore_read_id_query_order_row_order_and_repeats():
    first = read([NODE, {'id': 'M', 'name': 'second'}])
    first['args'].update(ids=['N', 'M'], fields=['name', 'properties.definition'])
    second = deepcopy(first)
    second['args'].update(ids=['M', 'N'], fields=['properties.definition', 'name'])
    second['id'] = 'another-call'
    second['result']['items'].reverse()
    second['result'].update(revision=44, read_errors=[{'id': 'unused', 'error': 'Unknown ID'}])
    assert fingerprint(reads=[first]) == fingerprint(reads=[second, first, second])


@pytest.mark.parametrize('field,value', [('name', 'New name'), ('properties', {'definition': 'New meaning'}),
                                        ('description', 'Newly read field')])
def test_changed_or_added_record_field_changes_premises(field, value):
    packet = {'nodes': [NODE]}
    changed = {'id': 'N', field: value}
    assert fingerprint(packet) != fingerprint(packet, [read([changed])])


def test_nested_id_property_is_a_real_field_not_transport_metadata():
    packet = {'nodes': [NODE]}
    assert fingerprint(packet) != fingerprint(packet, [read([{'id': 'N', 'properties': {'id': 'new property'}}])])


def test_distinct_snapshot_views_remain_distinct():
    assert fingerprint({'nodes': [NODE]}) != fingerprint({'nodes': [NODE]}, [read([NODE], view='before')])


def test_id_only_failed_selector_and_missing_id_do_not_add_facts():
    query = read([{'id': 'N'}], read_errors=[{'id': 'N', 'error': 'Unknown field'}], missing_ids=['missing'])
    assert fingerprint() == fingerprint(reads=[query])


def test_transport_task_and_draft_state_do_not_advance_premises():
    packet = {'nodes': [NODE], 'task': {'stage': 'writing', 'draft_revision': 2}, 'revision': 4,
              'draft': {'explanation': 'old'}, 'issues': [{'error': 'old'}]}
    changed = deepcopy(packet)
    changed.update(draft={'explanation': 'new'}, draft_id='new', revision=88,
                   latest_output_error={'error': 'new'}, semantic_issues=[{'problem': 'new'}],
                   task={'stage': 'validating', 'draft_id': 'new', 'candidate_draft_id': 'old',
                         'candidate_matches_draft': False, 'revision': 8})
    assert fingerprint(packet) == fingerprint(changed)


def test_policy_change_always_changes_premises():
    assert fingerprint(policy='old') != fingerprint(policy='new')


def test_complete_empty_relation_page_is_scoped_absence_but_incomplete_page_is_not():
    incomplete = read([], mode='relations', complete=False, next_offset=30)
    empty = read([], mode='relations', complete=True, next_offset=None)
    another = deepcopy(empty)
    another['args']['ids'] = ['M']
    assert fingerprint() == fingerprint(reads=[incomplete])
    assert fingerprint() != fingerprint(reads=[empty])
    assert fingerprint(reads=[empty]) != fingerprint(reads=[another])
    assert fingerprint(reads=[empty]) == fingerprint(reads=[empty, empty])


def test_relation_query_qualifiers_are_order_independent_but_direction_is_not():
    first = read([], mode='relations', complete=True, next_offset=None)
    first['args'].update(ids=['N', 'M'], edge_types=['depends_on', 'grounded_on'])
    second = deepcopy(first)
    second['args'].update(ids=['M', 'N'], edge_types=['grounded_on', 'depends_on'])
    assert fingerprint(reads=[first]) == fingerprint(reads=[second])
    second['args']['direction'] = 'incoming'
    assert fingerprint(reads=[first]) != fingerprint(reads=[second])


def test_failed_empty_relation_query_is_not_an_absence_fact():
    failed = read([], mode='relations', complete=True, read_errors=[{'id': 'N', 'error': 'invalid'}])
    assert fingerprint() == fingerprint(reads=[failed])


def test_contract_reads_share_packet_atoms_and_new_fields_count():
    rule = {'id': 'depends_on', 'description': 'Expression operand'}
    packet = {'contracts': [rule]}
    query = read([rule], mode='contracts')
    assert fingerprint(packet) == fingerprint(packet, [query])
    query['result']['items'][0] = {**rule, 'direction_semantics': 'from metric to operand'}
    assert fingerprint(packet) != fingerprint(packet, [query])


def test_evidence_reads_share_packet_atoms_and_new_content_counts():
    evidence = {'id': 'E', 'content': {'question': 'Q', 'sql': 'SELECT 1'}}
    packet = {'evidence': [evidence]}
    query = read([evidence], name='read_evidence')
    assert fingerprint(packet) == fingerprint(packet, [query])
    assert fingerprint(packet) != fingerprint(packet, [read([{'id': 'E2', 'content': {'question': 'new'}}], name='read_evidence')])


def test_grouped_evidence_aliases_resolve_regardless_of_read_order():
    packet = {'evidence': [{'id': 'E1', 'evidence_ids': ['E1', 'E2'], 'content': {'sql': 'SELECT 1'}}]}
    alias = read([{'id': 'E2', 'same_content_as': 'E1'}], name='read_evidence')
    body = read([{'id': 'E1', 'content': {'sql': 'SELECT 1'}}], name='read_evidence')
    assert fingerprint(packet) == fingerprint({}, [alias, body]) == fingerprint({}, [body, alias])


def test_source_unit_reread_is_covered_and_new_unit_changes_premises():
    group = {'owner_id': 'N', 'member_ids': ['N', 'M'], 'required_operation': 'integrate_groups',
             'source_units': [{'unit_id': 'U', 'kind': 'normal_content', 'node_id': 'N'}]}
    packet = {'source_unit_manifest': [group]}
    query = {'name': 'read_source_units', 'args': {'owner_ids': ['N']},
             'result': {'view': 'before', 'source_unit_manifest': [deepcopy(group)]}}
    query['result']['source_unit_manifest'][0]['member_ids'].reverse()
    assert fingerprint(packet) == fingerprint(packet, [query])
    query['result']['source_unit_manifest'][0]['source_units'].append({'unit_id': 'NEW'})
    assert fingerprint(packet) != fingerprint(packet, [query])


def test_semantic_basis_facts_and_required_context_survive_narration_filter():
    packet = {'semantic_issues': [{'problem': 'not a fact'}]}
    with_basis = {**packet, 'semantic_issue_basis': [{'candidate_hash': 'historical',
        'records': [{'view': 'before', 'kind': 'node', 'id': 'N', 'record': NODE}]}]}
    assert fingerprint(packet) != fingerprint(with_basis)
    assert fingerprint(with_basis) == fingerprint(with_basis, [read([NODE], view='before')])
    with_context = {**packet, 'required_context': [{'request': {'id': 'N', 'view': 'candidate'},
                                                  'result': {'items': [NODE], 'view': 'candidate'}}]}
    assert fingerprint(packet) != fingerprint(with_context)
    assert fingerprint(with_context) == fingerprint({'nodes': [NODE]})


def test_direct_required_context_record_fields_are_not_lost():
    packet = {'required_context': [{'request': {'id': 'N', 'view': 'candidate'},
                                    'result': {'name': 'Exact business meaning'}}]}
    assert fingerprint(packet) == fingerprint({'nodes': [{'id': 'N', 'name': 'Exact business meaning'}]})


def test_shared_input_candidate_projection_has_same_facts_as_expanded_packet():
    packet = {'nodes': [NODE], 'candidate_nodes': [NODE]}
    assert fingerprint(packet) == fingerprint(compact_node_packet(packet))


def test_compiler_counterexamples_are_facts_but_repeated_identical_rows_are_not_new():
    packet = {'nodes': [NODE]}
    query = {'name': 'counterexamples', 'result': {'facts': [NODE]}}
    assert fingerprint(packet) == fingerprint(packet, [query])
    assert fingerprint() != fingerprint({}, [query])


def test_empty_packet_projection_and_transport_envelopes_are_not_new_facts():
    assert fingerprint({}) == fingerprint(compact_node_packet({}))
    assert fingerprint({}) == fingerprint({'candidate_hash': 'new', 'draft_revision': 3, 'presentation': 'changed'})


def test_unknown_error_narration_is_not_a_fact():
    assert fingerprint() == fingerprint(reads=[{'name': 'inspect_graph', 'args': {},
        'result': {'error': 'Unknown field', 'details': {'explanation': 'new narration'}}}])


def test_required_context_contract_projection_is_covered_by_packet_contract():
    contract = {'id': 'depends_on', 'description': 'Expression operand'}
    context = {'required_context': [{'request': {'id': 'depends_on', 'kind': 'contract', 'view': 'candidate'},
                                     'result': {'description': 'Expression operand'}}]}
    assert fingerprint(context) == fingerprint({'contracts': [contract]})


def test_compiler_reference_only_error_rows_do_not_imply_new_graph_content():
    references = {'name': 'before', 'result': {'facts': [{'kind': 'node', 'id': 'N', 'view': 'candidate'}]}}
    assert fingerprint() == fingerprint(reads=[references])
