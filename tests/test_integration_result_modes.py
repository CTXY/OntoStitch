"""Normal results must never silently consume declared unresolved conflicts."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from test_graph_integration import catalog, concept, state
from test_merge_identity_integration_v2 import content, points, prepare

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.patch import IdentityIntegration, MergePatch, compile_patch
from cortex.capabilities.graph_merge.integration.source_units import PatchContractError, source_units
from cortex.capabilities.graph_merge.llm.node_contract import NodeWriterResult
from cortex.capabilities.graph_merge.llm.task_agent import failure_feedback
from cortex.schema import ConflictPoint, GraphState, load_root_schema


def normal_fixture():
    base = state([*catalog(), concept('A', 'a'), concept('B', 'a')])
    context, _ = prepare(base)
    group = {'owner_id': 'A', 'member_ids': ['A', 'B'],
             'content': content(concept('A', 'a')),
             'source_routes': [{'source_unit_id': unit['unit_id'],
                               'destination': {'kind': 'result'},
                               'explanation': 'Retain this complete source.'}
                              for unit in source_units(base, 'A', ['A', 'B'])],
             'explanation': 'Integrate compatible definitions.'}
    return base, context, {'integrate_groups': [group], 'explanation': 'Complete local integration.'}


@pytest.mark.parametrize('model', [IdentityIntegration, NodeWriterResult, MergePatch])
def test_mixed_normal_content_and_conflict_points_rejected_at_submission(model):
    _, _, raw = normal_fixture()
    raw['integrate_groups'][0]['conflict_points'] = points(['left', 'right'])
    arguments = raw['integrate_groups'][0] if model is IdentityIntegration else raw
    with pytest.raises(ValidationError, match='normal_content_with_conflict_points') as caught:
        model.model_validate(arguments)
    feedback = str(failure_feedback(caught.value))
    for required in ('conflict_points', 'normal_content_with_conflict_points', 'owner_id',
                     'two complete alternatives', 'Preserve genuine competing meanings'):
        assert required in feedback


@pytest.mark.parametrize('explicit_empty', [False, True])
def test_normal_result_with_omitted_or_empty_conflict_points_still_compiles(explicit_empty):
    base, context, raw = normal_fixture()
    if explicit_empty:
        raw['integrate_groups'][0]['conflict_points'] = []
    NodeWriterResult.model_validate(raw)
    patch = MergePatch.model_validate(raw)
    result = compile_patch(context, base, patch, targets={'A', 'B'}, aliases={'A': 'A', 'B': 'A'})
    GraphManager(load_root_schema()).validate_state(result.state)
    assert not result.state.conflict_nodes
    assert {node.id for node in result.state.graph.nodes}.isdisjoint({'B'})
    expected = {s for node in base.graph.nodes if node.id in {'A', 'B'} for s in node.sources}
    assert set(next(n for n in result.state.graph.nodes if n.id == 'A').sources) == expected


@pytest.mark.parametrize('bypass', ['model_copy', 'model_construct'])
def test_compiler_rejects_bypassed_mixed_mode_without_changing_prior_candidate(bypass):
    base, context, raw = normal_fixture()
    patch = MergePatch.model_validate(raw)
    original_group = patch.integrate_groups[0]
    declared_points = tuple(ConflictPoint.model_validate(p) for p in points(['left', 'right']))
    if bypass == 'model_copy':
        mixed = original_group.model_copy(update={'conflict_points': declared_points})
    else:
        mixed = IdentityIntegration.model_construct(**{**dict(original_group), 'conflict_points': declared_points})
    unsafe_patch = patch.model_copy(update={'integrate_groups': (mixed,)})
    context.candidate = base
    prior_batch = object()
    context.active_batch = prior_batch
    before_json = base.model_dump_json()
    with pytest.raises(PatchContractError) as caught:
        compile_patch(context, base, unsafe_patch, targets={'A', 'B'}, aliases={'A': 'A', 'B': 'A'})
    assert caught.value.details['code'] == 'normal_content_with_conflict_points'
    assert caught.value.details['owner_id'] == 'A'
    assert caught.value.details['path'] == 'conflict_points'
    assert caught.value.details['conflict_point_ids'] == ['definition']
    assert context.candidate is base and context.active_batch is prior_batch
    assert base.model_dump_json() == before_json


def test_valid_conflict_keeps_branches_points_and_sources_after_serialization():
    base, context, raw = normal_fixture()
    group = raw['integrate_groups'][0]
    ordinary = group.pop('content')
    group['alternatives'] = [{'alternative_id': key, 'content': {
        **deepcopy(ordinary), 'properties': {**ordinary['properties'], 'definition': key}}}
        for key in ('left', 'right')]
    group['conflict_points'] = points(['left', 'right'])
    for route, key in zip(group['source_routes'], ('left', 'right'), strict=True):
        route['destination'] = {'kind': 'alternatives', 'alternative_ids': [key]}
    NodeWriterResult.model_validate(raw)
    # Equivalent to reloading a serialized valid draft, with no new fields or
    # changes to the representation used by existing checkpoints.
    patch = MergePatch.model_validate_json(MergePatch.model_validate(raw).model_dump_json())
    compiled = compile_patch(context, base, patch, targets={'A', 'B'}, aliases={'A': 'A', 'B': 'A'})
    restored = GraphState.model_validate_json(compiled.state.model_dump_json())
    GraphManager(load_root_schema()).validate_state(restored)
    owner, = restored.conflict_nodes
    assert [branch.alternative_id for branch in owner.alternatives] == ['left', 'right']
    assert [point.point_id for point in owner.conflict_points] == ['definition']
    sources = {n.id: n.sources for n in base.graph.nodes}
    assert owner.alternatives[0].sources == sources['A']
    assert owner.alternatives[1].sources == sources['B']


def test_reloading_a_malformed_saved_draft_rejects_it_instead_of_normalizing_it():
    _, _, raw = normal_fixture()
    raw['integrate_groups'][0]['conflict_points'] = points(['left', 'right'])
    with pytest.raises(ValidationError, match='normal_content_with_conflict_points'):
        MergePatch.model_validate(raw)
    assert raw['integrate_groups'][0]['conflict_points'] == points(['left', 'right'])
