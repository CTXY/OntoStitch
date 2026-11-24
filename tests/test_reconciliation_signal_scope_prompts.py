"""Signal-local input promises match bounded reads and explicit write rights."""
from test_reconciliation_context import runtime

from cortex.capabilities.graph_merge.reconciliation.presentation import fixed_prompt
from cortex.capabilities.graph_merge.reconciliation.policy import (
    SIGNALS,
    reconciliation_system,
)


def test_explicit_relation_set_completeness_and_read_only_boundary():
    rt = runtime()
    rt.packet['relation_sets'] = [{
        'kind': 'directed_pairs',
        'description': 'All assertions directed from N1 to N2.',
        'pairs': [['N1', 'N2']], 'node_ids': ['N1', 'N2'], 'edge_ids': ['e1'],
    }]
    text = fixed_prompt(rt, rt.working['before'])
    assert 'All assertions directed from N1 to N2.' in text
    assert 'without a generic one-hop expansion' in text
    assert 'All other existing objects are read-only' in text
    assert 'Evidence or predicate reads do not expand this write scope' in text
    assert 'complete adjacent relationships' not in text
    # Internal match lists are already represented by actual node/edge records.
    assert 'Selector: `"' not in text
    assert 'Selector: `{"pairs":[["N1","N2"]]}`' in text


def test_selector_fallback_does_not_claim_global_adjacency():
    rt = runtime()
    rt.packet['relation_sets'] = [{'kind': 'physical_owners', 'physical_ids': ['P'],
                                  'node_ids': ['N1'], 'edge_ids': ['e1']}]
    text = fixed_prompt(rt, rt.working['before'])
    assert 'physical_owners' in text and '"physical_ids"' in text
    assert 'unlisted graph relationships are not proven absent' in text


def test_every_signal_explains_necessary_material_without_role_leakage():
    for kind in SIGNALS:
        writer = reconciliation_system('writer', [kind])
        assessment = reconciliation_system('assessment', [kind])
        assert 'Necessary local material' in writer and 'Necessary local material' in assessment
        assert '## Assessment check points' not in writer
        assert '## Writer judgment and modification' not in assessment
        assert 'Visibility is not permission to modify' in writer
        assert 'It does not fetch arbitrary adjacency' in assessment


def test_metadata_only_write_rights_are_not_presented_as_full_node_permission():
    rt = runtime()
    rt.packet['write_scope']['metadata_only_node_ids'] = ['N1']
    text = fixed_prompt(rt, rt.working['before'])
    ordinary = text.split('Existing business nodes allowed to be modified:', 1)[1].split('Existing Concepts writable only', 1)[0]
    assert '"N1"' not in ordinary
    restricted = text.split('Existing Concepts writable only', 1)[1].split('Existing business relationships', 1)[0]
    assert '`properties.relation_metadata`' in restricted and '"N1"' in restricted
    assert 'retain unrelated groups unchanged' in restricted
