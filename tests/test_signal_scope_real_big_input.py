"""The two real 200k+ prompts stay bounded without losing selected facts."""
import gzip
import json
from hashlib import sha256
from pathlib import Path

import pytest
from regress_signal_subgraph_inputs import characters, controls, request, scope, setup

FIXTURES = Path(__file__).parent / 'fixtures/reconciliation_signal_subgraphs'


@pytest.mark.parametrize('database,old_size', [('thrombosis', 212383), ('toxicology', 212061)])
def test_real_large_input_selective_scope_permissions_receipts_and_recovery(database, old_size):
    fixture_path = FIXTURES / (database + '.json.gz')
    manifest = json.loads((FIXTURES / 'manifest.json').read_text())
    assert sha256(fixture_path.read_bytes()).hexdigest() == manifest[database]['fixture_sha256']
    with gzip.open(fixture_path, 'rt') as stream:
        fixture = json.load(stream)
    context, runtime = setup(fixture)
    result = controls(fixture, context, runtime)
    assert result['model_calls'] == 0
    assert characters(request(runtime)) < old_size
    assert characters(request(runtime)) < 500000
    # These real tasks have large hubs; they must not recover all old neighbors.
    selected = scope(runtime)
    assert len(selected['node_ids']) < 30
    assert len(selected['edge_ids']) < 35
    _, assessment = setup(fixture, 'graph_assess')
    assert characters(request(assessment)) < 500000
    assert set(selected['write_node_ids']) <= set(selected['node_ids'])
    assert set(selected['write_edge_ids']) <= set(selected['edge_ids'])
