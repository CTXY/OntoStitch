"""Controlled reconciliation probes preserve real sources and disclose faults."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
spec = importlib.util.spec_from_file_location('reconciliation_smoke_test', REPO / 'scripts/reconciliation_probe_fixtures.py')
smoke = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smoke
spec.loader.exec_module(smoke)

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.schema import load_root_schema
from run_node_consolidation_smoke import resolve_input_root


def args_for_existing_sources(tmp_path):
    if not (REPO / 'output/latest_graphs/toxicology/case_level_graphs/266/state.json').exists():
        pytest.skip('Optional real case-level graph data is not present')
    return SimpleNamespace(output=tmp_path, graph_root=REPO / 'output/latest_graphs',
        source_input_root=resolve_input_root(None), discovery_limit=32)


def test_real_metric_fixture_preserves_original_record_and_all_retired_sources():
    path = REPO / 'tests/fixtures/node_local_metric_retirement.json'
    original = path.read_bytes()
    fixture = smoke.real_metric_fixture(path)
    assert path.read_bytes() == original
    assert fixture.provenance['controlled_changes'] == []
    with pytest.raises(GraphRejected):
        GraphManager(load_root_schema()).validate_state(fixture.before)
    GraphManager(load_root_schema()).validate_state(fixture.expected)
    retired = fixture.changes[0]
    before = {edge.id: edge for edge in fixture.before.graph.edges}
    after = {edge.id: edge for edge in fixture.expected.graph.edges}
    absorbed, survivor = retired['edge_id'], retired['replacement_edge_id']
    assert absorbed not in after
    assert {*before[absorbed].sources, *before[survivor].sources} <= set(after[survivor].sources)
    smoke.semantic_oracle(fixture.expected, fixture)


def test_oracle_rejects_structurally_valid_source_loss():
    fixture = smoke.real_metric_fixture(REPO / 'tests/fixtures/node_local_metric_retirement.json')
    survivor = fixture.changes[0]['replacement_edge_id']
    corrupted = fixture.expected.model_copy(update={'graph': fixture.expected.graph.model_copy(update={
        'edges': tuple(edge.model_copy(update={'sources': edge.sources[:1]}) if edge.id == survivor else edge
                       for edge in fixture.expected.graph.edges)})})
    GraphManager(load_root_schema()).validate_state(corrupted)
    with pytest.raises(AssertionError, match='lost expected sources'):
        smoke.semantic_oracle(corrupted, fixture)


def test_bounded_real_parallel_discovery_can_preserve_every_existing_role(tmp_path):
    fixture = smoke.select_healthy(args_for_existing_sources(tmp_path))
    assert fixture.before == fixture.expected and fixture.changes == []
    GraphManager(load_root_schema()).validate_state(fixture.before)
    assert fixture.signal['kind'] == 'parallel_assertions'
    roles = [edge.properties['description'] for edge in fixture.before.graph.edges
             if edge.id in fixture.signal['edge_ids']]
    assert len(roles) >= 2 and len(set(roles)) == len(roles)


def test_abstract_fault_is_explicit_and_does_not_change_node_knowledge(tmp_path):
    fixture = smoke.select_abstract(args_for_existing_sources(tmp_path))
    change = fixture.changes[0]
    before = {edge.id: edge for edge in fixture.before.graph.edges}
    after = {edge.id: edge for edge in fixture.expected.graph.edges}
    assert before[change['edge_id']].edge_type == 'has_attribute'
    assert after[change['edge_id']].edge_type == change['set']['edge_type']
    assert fixture.before.graph.nodes == fixture.expected.graph.nodes
    assert fixture.provenance['controlled_changes']
    assert before[change['edge_id']].sources == after[change['edge_id']].sources


def test_metadata_alias_repair_keeps_group_meaning_and_real_members(tmp_path):
    fixture = smoke.metadata_fixture(args_for_existing_sources(tmp_path))
    with pytest.raises(GraphRejected):
        GraphManager(load_root_schema()).validate_state(fixture.before)
    GraphManager(load_root_schema()).validate_state(fixture.expected)
    old = next(node for node in fixture.before.graph.nodes if node.id == fixture.write_node_id).properties['relation_metadata'][0]
    new = next(node for node in fixture.expected.graph.nodes if node.id == fixture.write_node_id).properties['relation_metadata'][0]
    assert old['id'] == new['id'] and old['description'] == new['description']
    assert len(new['edges']) == 2 and set(new['edges']) <= {edge.id for edge in fixture.expected.graph.edges}
    assert fixture.before.graph.edges == fixture.expected.graph.edges


def test_conflict_fixture_preserves_all_real_branches_and_scopes(tmp_path):
    fixture=smoke.preserve_conflict_fixture(args_for_existing_sources(tmp_path))
    assert fixture.before==fixture.expected and not fixture.changes
    owner=next(n for n in fixture.before.graph.nodes if n.alternatives)
    assert len(owner.alternatives)==2
    scoped=[e for e in fixture.before.graph.edges if e.from_node_id==owner.id]
    assert len(scoped)==4 and all(e.properties.get('alternative_ids') for e in scoped)
    GraphManager(load_root_schema()).validate_state(fixture.expected)


def test_live_oracle_allows_prose_rewrite_but_does_not_claim_semantic_proof():
    fixture = smoke.real_metric_fixture(REPO / 'tests/fixtures/node_local_metric_retirement.json')
    survivor = fixture.changes[0]['replacement_edge_id']
    rewritten = fixture.expected.model_copy(update={'graph': fixture.expected.graph.model_copy(update={
        'edges': tuple(edge.model_copy(update={'properties': {**edge.properties,
            'description': 'The same direct counted population; explanatory wording revised.'}}) if edge.id == survivor else edge
            for edge in fixture.expected.graph.edges)})})
    result = smoke.live_invariant_oracle(rewritten, fixture)
    assert result['strict_graph_validation'] and not result['needs_manual_oracle_review']
    assert result['semantic_equivalence_proven_by_code'] is False
    with pytest.raises(AssertionError, match='differs from'):
        smoke.semantic_oracle(rewritten, fixture)


def test_live_oracle_flags_formula_change_for_review_instead_of_accepting_blindly():
    fixture = smoke.real_metric_fixture(REPO / 'tests/fixtures/node_local_metric_retirement.json')
    owner = fixture.signal['node_ids'][0]
    changed = []
    for node in fixture.expected.graph.nodes:
        if node.id == owner:
            properties = deepcopy(node.properties)
            properties['expression']['expr'] = properties['expression']['expr'].replace('COUNT(', 'COUNT(DISTINCT ')
            node = node.model_copy(update={'properties': properties})
        changed.append(node)
    state = fixture.expected.model_copy(update={'graph': fixture.expected.graph.model_copy(update={'nodes': tuple(changed)})})
    result = smoke.live_invariant_oracle(state, fixture)
    assert result['strict_graph_validation'] and result['needs_manual_oracle_review']


def test_wire_without_advertised_tools_is_not_a_permission_verification(tmp_path):
    fixture = SimpleNamespace(name='metric_retirement')
    wire = tmp_path / 'wire'
    wire.mkdir()
    smoke.write(wire / '001_request.json', {'body': {'messages': [
        {'content': 'reconciliation-smoke:metric_retirement'}]},
        'timeout': {'read': 120, 'connect': 30, 'write': 60, 'pool': 30}})
    row, = smoke.wire_evidence(SimpleNamespace(output=tmp_path, request_timeout=120), fixture)
    assert row['advertised_tools'] == []
    assert row['permissions_valid'] is None
    assert row['permission_evidence'] == 'no_tools_advertised_role_unreported'


def test_actual_validation_failure_is_preserved_apart_from_wrapper_exception(tmp_path):
    import json
    feedback = {'status': 'rejected_unapplied', 'error': 'Complete actual reason',
        'details': {'code': 'MISSING_SOURCE_UNIT', 'path': 'changes[0]', 'missing': ['unit-real']}}
    (tmp_path / 'model_calls.jsonl').write_text(json.dumps({'event_type': 'tool_result',
        'name': 'submit_draft', 'component': 'integration_reconcile', 'sequence': 8, 'result': feedback}) + '\n')
    row, = smoke.validation_failures(tmp_path)
    assert row['code'] == 'MISSING_SOURCE_UNIT' and row['path'] == 'changes[0]'
    assert row['target'] == 'submit_draft' and row['feedback'] == feedback




