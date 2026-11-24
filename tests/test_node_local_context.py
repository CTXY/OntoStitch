"""Node-local permissions and immutable request prefixes against real graph rows."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.consolidation.presentation import NodeTaskDialogue, build_node_packet
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState, SourceReference, load_root_schema, validate_graph_state


def context():
    source = SourceReference(source_id='case-A')
    table = GraphNode(id='table', node_type='table', name='objects', properties={'table_name': 'objects'})
    columns = [GraphNode(id=ident, node_type='column', name=ident, properties={'table_ref': 'table', 'column_name': ident}) for ident in ('a', 'b')]
    members = [GraphNode(id=ident, node_type='concept', name='Object', properties={'targets': [[column]]}, sources=(source,))
               for ident, column in [('N1', 'a'), ('N2', 'b')]]
    outsider = GraphNode(id='OUT', node_type='concept', name='Unrelated full graph knowledge', properties={'targets': [['a']]})
    edges = [GraphEdge(id='e1', edge_type='grounded_on', from_node_id='N1', to_node_id='a', properties={'description': 'Representation A'}),
             GraphEdge(id='e2', edge_type='grounded_on', from_node_id='N2', to_node_id='b', properties={'description': 'Representation B'}),
             GraphEdge(id='incoming', edge_type='is_a', from_node_id='OUT', to_node_id='N1', properties={'description': 'Omitted unrelated incoming'})]
    graph = GraphState(graph=GraphSpace(nodes=(table, *columns, *members, outsider), edges=tuple(edges)))
    evidence = [SimpleNamespace(id='ev-A', source=source, content={'question': 'original', 'gold_sql': 'SELECT a FROM objects'}),
                SimpleNamespace(id='private', source=SourceReference(source_id='unrelated'), content={'question': 'unrelated'})]
    return SimpleNamespace(task_before=graph, candidate=graph, schema=load_root_schema(), evidence=evidence,
                           task_identity_groups=[{'owner_id': 'N1', 'member_ids': ['N1', 'N2']}])


def packet():
    return build_node_packet(context(), {'task_id': 'group-one', 'phase': 'consolidate'})


def current(dialogue):
    return json.loads(dialogue.messages()[-1].content.split('\n', 1)[1])


def read(dialogue, ids, items, call_id='read'):
    response = AIMessage(content='', tool_calls=[{'id': call_id, 'name': 'read_evidence', 'args': {'ids': ids}}])
    dialogue.exchange(response, [(call_id, {'items': items, 'missing_ids': []})], result_name='submit_patch')


def test_complete_group_outgoing_only_and_allowlisted_evidence():
    value = packet()
    assert [row['id'] for row in value['group']['members']] == ['N1', 'N2']
    assert [row['id'] for row in value['relations']] == ['e1', 'e2']
    assert {row['id'] for row in value['endpoint_directory']} == {'a', 'b'}
    assert value['allowed_evidence_ids'] == ['ev-A']
    assert 'private' not in json.dumps(value) and 'consumer_constraints' not in value
    assert 'unrelated' not in json.dumps(value).lower()
    assert value['node_schema']['node_type'] == 'concept'
    assert all('content' not in unit for row in value['source_unit_manifest'] for unit in row['source_units'])


def test_legacy_combination_text_migrates_without_expanding_incoming_neighbors():
    from graph_merge_archive import migrate_relation_notes
    ctx=context();raw=ctx.task_before.model_dump(mode='json')
    owner=next(n for n in raw['graph']['nodes'] if n['id']=='N1')
    owner['properties']['relation_metadata']=[{'id':'business-fact','edges':['e1','e2'],
        'description':'Both recorded representations refer to this business object.'}]
    ctx.task_before,records=migrate_relation_notes(raw)
    packet=build_node_packet(ctx,{})
    assert records and 'relation_metadata' not in packet['group']['members'][0]['properties']
    assert 'Both recorded representations' in packet['group']['members'][0]['properties']['definition']
    assert 'incoming' not in {edge['id'] for edge in packet['relations']}



def test_alternatives_are_full_resolved_without_storage_overrides():
    ctx = context()
    owner = next(node for node in ctx.task_before.graph.nodes if node.id == 'N1')
    first = {'name': owner.name, 'description': None, 'aliases': [], 'tags': [], 'properties': owner.properties}
    second = {**first, 'description': 'A competing complete meaning'}
    conflict, _ = GraphManager.compact_conflict(owner, [('A', first, owner.sources), ('B', second, owner.sources)], conflict_points=())
    ctx.task_before = ctx.task_before.model_copy(update={'graph': ctx.task_before.graph.model_copy(update={'nodes': tuple(conflict if row.id == owner.id else row for row in ctx.task_before.graph.nodes)})})
    value = build_node_packet(ctx, {})
    alternatives = value['group']['members'][0]['alternatives']
    assert len(alternatives) == 2 and all(set(row['content']) == {'name', 'description', 'aliases', 'tags', 'properties'} for row in alternatives)
    assert 'overrides' not in json.dumps(value)


def test_assessment_actual_candidate_moves_only_current_tail():
    ctx = context()
    first = build_node_packet(ctx, {'phase': 'node_assess'})
    owner = next(row for row in ctx.candidate.graph.nodes if row.id == 'N1')
    changed = owner.model_copy(update={'description': 'Actual compiled different content'})
    ctx.candidate = ctx.candidate.model_copy(update={'graph': ctx.candidate.graph.model_copy(update={'nodes': tuple(changed if row.id == 'N1' else row for row in ctx.candidate.graph.nodes)})})
    second = build_node_packet(ctx, {'phase': 'node_assess'})
    left, right = NodeTaskDialogue('fixed', first), NodeTaskDialogue('fixed', second)
    assert left.messages()[:-1] == right.messages()[:-1]
    assert left.messages()[-1] != right.messages()[-1]
    assert left.premise_hash(policy='p') != right.premise_hash(policy='p')


def test_draft_replacement_keeps_prefix_errors_and_binds_current_draft():
    value = packet()
    value.update(draft={'explanation': 'old'}, draft_id='D1', issues=[{'code': 'bad', 'path': 'properties.targets'}], issues_draft_id='D1')
    dialogue = NodeTaskDialogue('fixed', value)
    prefix = dialogue.messages()[:-1]
    dialogue.set_draft({'explanation': 'new'}, [{'code': 'new_error', 'missing': ['unit-A'], 'extra': ['unit-B']}], draft_id='D2')
    assert dialogue.messages()[:-1] == prefix
    tail = current(dialogue)
    assert tail['draft_id'] == tail['issues_draft_id'] == 'D2'
    assert tail['issues'][0]['missing'] == ['unit-A'] and tail['issues'][0]['extra'] == ['unit-B']
    assert 'old' not in str(dialogue.messages())


def test_evidence_first_read_order_deduplication_and_checkpoint_restore():
    dialogue = NodeTaskDialogue('fixed', {**packet(), 'allowed_evidence_ids': ['ev-A', 'ev-B']})
    initial_hash = dialogue.premise_hash(policy='p')
    read(dialogue, ['ev-A'], [{'id': 'ev-A', 'content': {'question': 'original'}}])
    once = dialogue.messages()
    seen_hash = dialogue.premise_hash(policy='p')
    assert seen_hash != initial_hash
    read(dialogue, ['ev-A'], [{'id': 'ev-A', 'content': {'question': 'original'}}], 'repeat')
    assert dialogue.messages() == once and dialogue.premise_hash(policy='p') == seen_hash
    read(dialogue, ['ev-B'], [{'id': 'ev-B', 'same_content_as': 'ev-A'}], 'second')
    assert dialogue.messages()[:-2] == once[:-1]
    assert str(dialogue.messages()).count('\"question\":\"original\"') == 1
    saved = dialogue.snapshot()
    restored = NodeTaskDialogue('fixed', {**packet(), 'allowed_evidence_ids': ['ev-A', 'ev-B']})
    restored.restore(saved)
    assert restored.messages() == dialogue.messages()
    assert restored.premise_hash(policy='p') == dialogue.premise_hash(policy='p')


def test_read_cannot_escape_allowlist_or_change_original_evidence():
    dialogue = NodeTaskDialogue('fixed', packet())
    with pytest.raises(ValueError, match='outside'):
        read(dialogue, ['private'], [{'id': 'private', 'content': 'secret'}])
    read(dialogue, ['ev-A'], [{'id': 'ev-A', 'content': 'original'}])
    before = dialogue.snapshot()
    with pytest.raises(ValueError, match='changed'):
        read(dialogue, ['ev-A'], [{'id': 'ev-A', 'content': 'different'}], 'changed')
    assert dialogue.snapshot() == before


def test_unknown_group_members_or_multiple_groups_fail_closed():
    ctx = context()
    with pytest.raises(ValueError, match='missing'):
        build_node_packet(ctx, {'group': {'owner_id': 'N1', 'member_ids': ['N1', 'MISSING']}})
    ctx.task_identity_groups *= 2
    with pytest.raises(ValueError, match='exactly one'):
        build_node_packet(ctx, {})


def test_restore_rejects_different_local_permissions_without_mutation():
    original = NodeTaskDialogue('fixed', packet())
    read(original, ['ev-A'], [{'id': 'ev-A', 'content': 'original'}])
    restricted = NodeTaskDialogue('fixed', {**packet(), 'allowed_evidence_ids': []})
    before = restricted.snapshot()
    with pytest.raises(ValueError, match='fixed local input'):
        restricted.restore(original.snapshot())
    assert restricted.snapshot() == before


def test_history_keeps_local_errors_but_does_not_reintroduce_full_graph():
    ctx = context()
    rows = [row.model_dump(mode='json') for row in ctx.task_before.graph.nodes]
    value = build_node_packet(ctx, {'issues': [{'code': 'exact-code', 'path': 'properties.targets',
        'missing': ['source-unit-1'], 'extra': ['source-unit-2'], 'before': rows}], 'issues_draft_id': None,
        'required_context': [{'result': {'graph': {'nodes': rows}}}]})
    assert 'Unrelated full graph knowledge' not in json.dumps(value)
    assert value['issues'][0]['code'] == 'exact-code'
    assert value['issues'][0]['missing'] == ['source-unit-1']
    assert value['issues'][0]['extra'] == ['source-unit-2']
    assert value['diagnostic_projection']['out_of_scope_record_count'] > 0
    assert value['issues_draft_id'] is None


def test_live_compiler_error_also_cannot_reintroduce_graph_via_repair_facts():
    ctx = context()
    dialogue = NodeTaskDialogue('fixed', packet(), context=ctx)
    rows = [row.model_dump(mode='json') for row in ctx.task_before.graph.nodes]
    feedback = {'code': 'exact-code', 'path': 'properties.targets', 'missing': ['unit-A'], 'before': rows}
    dialogue.set_draft({'explanation': 'current'}, [feedback])
    rendered = str(dialogue.messages())
    assert 'Unrelated full graph knowledge' not in rendered
    assert 'exact-code' in rendered and 'unit-A' in rendered
    dialogue.plain_failure(AIMessage(content=''), {'error': 'malformed', 'after': rows})
    assert 'Unrelated full graph knowledge' not in str(dialogue.messages())
    assert current(dialogue)['latest_output_error']['error'] == 'malformed'


def test_new_draft_drops_inactive_historical_candidate_facts():
    ctx = context()
    dialogue = NodeTaskDialogue('fixed', packet(), context=ctx)
    node = next(row for row in ctx.task_before.graph.nodes if row.id == 'N1')
    old = node.model_copy(update={'description': 'obsolete rejected candidate body'}).model_dump(mode='json')
    dialogue.set_draft({'explanation': 'first'}, [{'code': 'bad', 'after': [old]}])
    assert 'obsolete rejected candidate body' in str(dialogue.messages())
    assert dialogue.repair_facts
    dialogue.set_draft({'explanation': 'second'}, [{'code': 'new', 'path': 'name'}])
    assert not dialogue.repair_facts
    assert 'obsolete rejected candidate body' not in str(dialogue.messages())


def test_legacy_field_repair_and_partial_reads_cannot_leak_external_node():
    ctx = context()
    value = build_node_packet(ctx, {'current_repair_fields': [{'kind': 'node', 'id': 'OUT', 'field': 'properties', 'value': {'secret': 'outside'}}],
        'required_context': [{'result': {'items': [{'id': 'OUT', 'properties': {'secret': 'outside'}}]}}]})
    assert 'current_repair_fields' not in value
    assert 'outside' not in json.dumps(value.get('required_context', []))
    assert value['diagnostic_projection']['out_of_scope_record_count'] == 1


def test_changed_endpoint_meaning_or_permissions_changes_premise_version():
    value = packet()
    first = NodeTaskDialogue('fixed', value)
    endpoint = deepcopy(value)
    endpoint['endpoint_directory'][0]['description'] = 'Newly supplied specific role'
    assert NodeTaskDialogue('fixed', endpoint).premise_hash(policy='p') != first.premise_hash(policy='p')
    changed = {**value, 'allowed_evidence_ids': []}
    assert NodeTaskDialogue('fixed', changed).premise_hash(policy='p') != first.premise_hash(policy='p')


def test_optional_property_descriptor_does_not_advertise_null_as_default():
    ctx = context()
    value = build_node_packet(ctx, {})
    actual = ctx.schema.effective_node_fields('concept')
    for name, descriptor in value['node_schema']['fields'].items():
        original = actual[name].model_dump(mode='json')
        assert not ('default' in descriptor and descriptor['default'] is None)
        from cortex.capabilities.graph_merge.llm.prompt_language import english_contract
        assert descriptor == english_contract({key: val for key, val in original.items() if key != 'default' or val is not None})
    # Nullable content.description belongs to NodeContent, not the semantic
    # property descriptor. Complete normal node content keeps its real null.
    assert value['group']['members'][0]['description'] is None
    assert value['node_schema']['fields']['mapping_type']['required'] is False


def test_plain_json_recovery_is_explicit_and_preserves_protocol_and_draft_errors():
    value = packet()
    value.update(draft={'explanation': 'retained'}, draft_id='D1',
                 issues=[{'code': 'source_routes_mismatch', 'path': 'source_routes', 'missing': ['unit-A']}],
                 issues_draft_id='D1')
    dialogue = NodeTaskDialogue('fixed', value)
    read(dialogue, ['ev-A'], [{'id': 'ev-A', 'content': {'question': 'original'}}])
    prefix = dialogue.messages()[:-1]
    protocol = {'code': 'output_protocol', 'error': 'Malformed tool JSON', 'result_calls': 1}
    dialogue.plain_failure(AIMessage(content='broken'), protocol)
    schema = {'type': 'object', 'properties': {'explanation': {'type': 'string'}}}
    dialogue.use_fallback(schema)
    tail = current(dialogue)
    assert dialogue.messages()[:-1] == prefix
    assert tail['result_only_recovery']['mode'] == 'plain_json'
    assert tail['result_only_recovery']['tools_available'] is False
    assert tail['result_only_recovery']['read_allowed'] is False
    assert 'do not call' in tail['result_only_recovery']['instruction']
    assert tail['result_only_recovery_schema'] == schema
    assert tail['latest_output_error'] == protocol
    assert tail['draft'] == {'explanation': 'retained'}
    assert tail['issues'][0]['missing'] == ['unit-A']
    assert tail['draft_id'] == tail['issues_draft_id'] == 'D1'
