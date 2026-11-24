"""Source scope reads and draft projections preserve fixed-base boundaries."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_graph_integration import catalog, concept, state
from legacy_merge_task_tools import build_read_tools


def test_reading_planned_member_returns_full_group_and_keeps_base_unchanged():
    base = state([*catalog(), concept('A', 'a'), concept('B', 'a')])
    before = base.model_dump(mode='json')
    ctx = SimpleNamespace(evidence=(), task_before=base, task_identity_groups=[
        {'owner_id': 'A', 'member_ids': ['A', 'B']}])
    tools = build_read_tools(ctx, {})
    row = tools['read_source_units'].invoke({'owner_ids': ['B']})['source_unit_manifest'][0]
    assert row['owner_id'] == 'A' and set(row['member_ids']) == {'A', 'B'}
    assert row['required_operation'] == 'integrate_groups'
    assert {u['node_id'] for u in row['source_units']} == {'A', 'B'}
    assert base.model_dump(mode='json') == before
    with pytest.raises(ValueError, match='Physical'):
        tools['read_source_units'].invoke({'owner_ids': ['a']})


def test_draft_projection_reads_one_owner_route_without_replacing_full_draft():
    draft = {'integrate_groups': [
        {'owner_id': 'A', 'source_routes': [{'source_unit_id': 'sA'}], 'content': {'name': 'A'}},
        {'owner_id': 'B', 'source_routes': [{'source_unit_id': 'sB'}], 'content': {'name': 'B'}}],
        'node_updates': [{'id': 'C', 'name': 'updated'}], 'explanation': 'full draft'}
    original = deepcopy(draft)
    ctx = SimpleNamespace(evidence=(), active_draft=draft, task_draft_errors=[{'code': 'bad_route'}])
    tools = build_read_tools(ctx, {})
    result = tools['inspect_draft'].invoke({'owner_ids': ['A'], 'sections': ['source_routes']})
    assert result['draft'] == {'integrate_groups': [{'owner_id': 'A', 'source_routes': [{'source_unit_id': 'sA'}]}]}
    assert result['projection'] == {'owner_ids': ['A'], 'sections': ['source_routes']}
    assert result['issues'] == ctx.task_draft_errors
    assert ctx.active_draft == original
    assert tools['inspect_draft'].invoke({})['draft'] == original
