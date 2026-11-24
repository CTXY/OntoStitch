"""The real batch-3 publication must retain branch-local relationship meaning."""
import gzip
from pathlib import Path

import pytest

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.graph_merge.integration.compiler import assessed_semantic_content
from cortex.capabilities.graph_merge.transactions import apply_merge_state
from cortex.schema import GraphState, load_root_schema

FIXTURE = Path(__file__).parent / 'fixtures/reconciliation_signal_subgraphs/thrombosis_publication_scope.json.gz'
SCOPES = {'incoming.edge.3dd07284f06edd280fcf': ['distinct_count'],
          'incoming.edge.660317b6406c1b2e4d14': ['plain_count']}


def state():
    return GraphState.model_validate_json(gzip.decompress(FIXTURE.read_bytes()))


def test_real_publication_preserves_partial_scopes_with_shared_metric_operands():
    reviewed = state()
    result = apply_merge_state(GraphManager(load_root_schema()), reviewed, request_id='scope-publication')
    assert assessed_semantic_content(result) == assessed_semantic_content(reviewed)
    edges = {e.id: e for e in result.graph.edges}
    for ident, scope in SCOPES.items():
        assert edges[ident].properties['alternative_ids'] == scope
        assert edges[ident].sources == next(e.sources for e in reviewed.graph.edges if e.id == ident)


def test_explicit_invalid_scope_is_rejected_instead_of_silently_repaired():
    reviewed = state()
    reviewed = reviewed.model_copy(update={'graph': reviewed.graph.model_copy(update={'edges': tuple(
        e.model_copy(update={'properties': {**e.properties, 'alternative_ids': ['unknown-branch']}})
        if e.id in SCOPES else e for e in reviewed.graph.edges)})})
    manager = GraphManager(load_root_schema())
    with pytest.raises(GraphRejected):
        apply_merge_state(manager, reviewed, request_id='invalid-scope')
    assert not manager.get_snapshot().graph.nodes


def test_explicit_all_branch_scope_and_unscoped_legacy_edges_remain_valid():
    reviewed = state()
    reviewed = reviewed.model_copy(update={'graph': reviewed.graph.model_copy(update={'edges': tuple(
        e.model_copy(update={'properties': {k: v for k, v in e.properties.items() if k != 'alternative_ids'}})
        if e.id in SCOPES else e for e in reviewed.graph.edges)})})
    result = apply_merge_state(GraphManager(load_root_schema()), reviewed, request_id='unscoped')
    for edge in result.graph.edges:
        if edge.id in SCOPES:
            assert set(edge.properties['alternative_ids']) == {'distinct_count', 'plain_count'}
