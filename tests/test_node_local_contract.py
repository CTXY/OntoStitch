from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cortex.capabilities.graph_merge.llm.node_contract import (
    NODE_TYPES,
    NodeAssessmentResult,
    NodeWriterResult,
    build_node_read_tools,
    node_output_model,
    node_prompt_modules,
    node_system,
    normalize_node_result,
)
from cortex.schema import SourceReference
from cortex.capabilities.graph_merge.integration.source_units import PatchContractError


def local_fixture():
    content = {'name': '登记日期', 'description': '保留两种匹配表示。', 'aliases': ['登记日'],
               'tags': [], 'properties': {'targets': [['column-A'], ['column-B']]}}
    packet = {
        'group': {'owner_id': 'N1', 'member_ids': ['N1', 'N2'], 'node_type': 'concept'},
        'source_unit_manifest': [{'owner_id': 'N1', 'source_units': [
            {'unit_id': 'unit-A', 'node_id': 'N1', 'kind': 'normal_content'},
            {'unit_id': 'unit-B', 'node_id': 'N2', 'kind': 'normal_content'}]}],
        'relations': [{'id': 'E1'}, {'id': 'E2'}],
        'endpoint_directory': [{'id': 'column-A', 'node_type': 'column'}, {'id': 'column-B', 'node_type': 'column'}],
        'allowed_evidence_ids': ['evidence-A'],
    }
    result = {'integrate_groups': [{'owner_id': 'N1', 'member_ids': ['N1', 'N2'],
        'content': content, 'source_routes': [
            {'source_unit_id': ident, 'destination': {'kind': 'result'}, 'explanation': '保留'}
            for ident in ('unit-A', 'unit-B')], 'explanation': '相容表示合并'}],
        'explanation': '继承关系'}
    return packet, result


def test_writer_sidecar_is_separate_from_legacy_patch():
    packet, result = local_fixture()
    result['relation_followups'] = [{'edge_ids': ['E1'], 'problem': '需要确认关系范围',
                                     'required_outcome': '保留原关系范围'}]
    patch, queue = normalize_node_result('consolidate', result, payload=packet)
    assert patch.integrate_groups[0].owner_id == 'N1'
    assert queue[0]['edge_ids'] == ['E1']
    assert 'relation_followups' not in patch.model_dump()


@pytest.mark.parametrize('field', ['edge_updates', 'edge_merges', 'create_edges', 'retirements', 'conflicts'])
def test_writer_cannot_gain_generic_graph_edit_permissions(field):
    _, result = local_fixture()
    result[field] = []
    with pytest.raises(ValidationError):
        NodeWriterResult.model_validate(result)


def test_binding_checks_members_units_and_original_relation_handles():
    packet, result = local_fixture()
    bad = deepcopy(result)
    bad['integrate_groups'][0]['member_ids'] = ['N1', 'outside']
    with pytest.raises(ValueError, match='partition_coverage_mismatch'):
        normalize_node_result('writer', bad, payload=packet)
    bad = deepcopy(result)
    bad['integrate_groups'][0]['source_routes'].pop()
    with pytest.raises(ValueError, match='exactly once'):
        normalize_node_result('writer', bad, payload=packet)
    bad = deepcopy(result)
    bad['relation_followups'] = [{'edge_ids': ['outside'], 'problem': 'unknown', 'required_outcome': 'unknown'}]
    with pytest.raises(ValueError, match='original edges'):
        normalize_node_result('writer', bad, payload=packet)


def test_assessment_accepts_node_with_pending_relation_but_not_global_claim():
    packet, _ = local_fixture()
    packet['before_relations'] = packet.pop('relations')
    packet['candidate'] = {'nodes': [{'id': 'N1'}], 'relations': [{'id': 'deduplicated-E'}]}
    result = {'verdict': 'accept', 'issues': [], 'relation_followups': [
        {'edge_ids': ['E2'], 'problem': '分支归属需协调', 'required_outcome': '保留原含义'}]}
    review, queue = normalize_node_result('node_assess', result, payload=packet)
    assert review.verdict == 'accept' and queue[0]['edge_ids'] == ['E2']
    bad = {'verdict': 'needs_context', 'issues': [{'refs': [{'kind': 'node', 'id': 'N1'}],
        'problem': '缺少前提', 'required_outcome': '说明前提', 'claim': {'kind': 'relation_absent'}}]}
    with pytest.raises(ValidationError):
        NodeAssessmentResult.model_validate(bad)
    bad['issues'][0].pop('claim')
    bad['issues'][0]['refs'][0]['id'] = 'outside'
    with pytest.raises(ValueError, match='displayed local object IDs'):
        normalize_node_result('assessment', bad, payload=packet)


def test_evidence_read_is_scoped_even_when_context_has_other_cases():
    source = SourceReference(source_id='case-A')
    context = SimpleNamespace(evidence=[
        SimpleNamespace(id='evidence-A', source=source, content={'question': 'allowed'}),
        SimpleNamespace(id='evidence-other-case', source=source, content={'question': 'private'})])
    packet, _ = local_fixture()
    tools = build_node_read_tools(context, packet)
    assert set(tools) == {'read_evidence'}
    assert tools['read_evidence'].invoke({'ids': ['evidence-A']})['items'][0]['content']['question'] == 'allowed'
    with pytest.raises(ValueError, match='outside this local task'):
        tools['read_evidence'].invoke({'ids': ['evidence-other-case']})


def test_stable_shared_prefix_and_one_type_guidance_last():
    for role in ('writer', 'assessment'):
        systems = [node_system(role, kind) for kind in NODE_TYPES]
        first_type = systems[0].index('# Guidance for This Type Group:')
        prefix = systems[0][:first_type]
        assert all(system.startswith(prefix) for system in systems)
        assert all(system.count('# Guidance for This Type Group:') == 1 for system in systems)
        assert node_prompt_modules(role, 'metric')[-1] == 'types/metric.en.md'
        assert node_output_model(role) is node_output_model(role)
    assert 'preserves determinable branch scope' in node_system('writer', 'concept')
    assert 'When text is duplicated but still carries provenance or relationships that must be preserved' in node_system('writer', 'concept')


@pytest.mark.parametrize('kind,properties', [
    ('concept', {'targets': [['global-hidden-column']]}),
    ('metric', {'expression': {'expr': 'SUM(${amount})', 'refs': {'amount': 'global-hidden-metric'}}}),
])
def test_writer_cannot_reference_undisplayed_global_node_ids(kind, properties):
    packet, result = local_fixture()
    packet['group']['node_type'] = kind
    result['integrate_groups'][0]['content']['properties'] = properties
    with pytest.raises(PatchContractError) as caught:
        normalize_node_result('writer', result, payload=packet)
    assert caught.value.details['code'] == 'node_reference_outside_local_input'
    assert caught.value.details['path'] == 'integrate_groups[0].content.properties'
    assert caught.value.details['extra'] == [next(iter(properties.get('expression', {}).get('refs', {}).values()), 'global-hidden-column')]


def test_writer_alternative_contents_have_the_same_local_ref_boundary():
    packet, result = local_fixture()
    integration = result['integrate_groups'][0]
    content = integration.pop('content')
    integration['alternatives'] = [
        {'alternative_id': 'A', 'content': content},
        {'alternative_id': 'B', 'content': {**deepcopy(content), 'properties': {'targets': [['global-hidden-column']]}}}]
    integration['conflict_points'] = [{'point_id': 'p', 'aspect': 'meaning', 'description': 'difference',
        'choices': [{'alternative_ids': ['A'], 'statement': 'first'}, {'alternative_ids': ['B'], 'statement': 'second'}]}]
    with pytest.raises(PatchContractError) as caught:
        normalize_node_result('writer', result, payload=packet)
    assert caught.value.details['path'] == 'integrate_groups[0].alternatives[1].content.properties'


def test_writer_cannot_create_relation_metadata_even_with_known_edge_ids():
    packet,result=local_fixture()
    result['integrate_groups'][0]['content']['properties']['relation_metadata']=[
        {'id':'copies','edges':['E1','E2'],'description':'Repeated source assertions.'}]
    with pytest.raises(ValueError,match='Unsupported property relation_metadata'):
        normalize_node_result('writer',result,payload=packet)



def test_source_route_feedback_retains_exact_missing_extra_and_duplicate_handles():
    packet, result = local_fixture()
    routes = result['integrate_groups'][0]['source_routes']
    routes.pop()
    routes.extend([deepcopy(routes[0]), {**deepcopy(routes[0]), 'source_unit_id': 'invented'}])
    with pytest.raises(PatchContractError) as caught:
        normalize_node_result('writer', result, payload=packet)
    details = caught.value.details
    assert details['code'] == 'source_routes_mismatch'
    assert details['owner_id'] == 'N1' and details['path'] == 'integrate_groups[0].source_routes'
    assert details['missing'] == ['unit-B'] and details['extra'] == ['invented']
    assert details['duplicates'] == ['unit-A']


def test_member_feedback_retains_expected_owner_and_member_differences():
    packet, result = local_fixture()
    result['integrate_groups'][0]['member_ids'] = ['N1', 'outside']
    with pytest.raises(PatchContractError) as caught:
        normalize_node_result('writer', result, payload=packet)
    details = caught.value.details
    assert details['code'] == 'partition_coverage_mismatch'
    assert details['missing'] == ['N2'] and details['extra'] == ['outside']
    assert details['duplicates'] == []
    assert details['owner_id'] == 'N1'
