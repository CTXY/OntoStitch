"""Canonical draft presentation must preserve diagnostics and their bindings."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from cortex.capabilities.graph_merge.llm.packets import TaskDialogue
from legacy_merge_task_tools import build_read_tools

DRAFT = {'explanation': 'A complete current draft', 'node_updates': []}
OLD = {'code': 'compiler', 'error': 'Old diagnostic'}
NEW = {'code': 'compiler', 'error': 'New exact diagnostic'}


def packet(dialogue):
    return json.loads(dialogue.messages()[1].content)


def full_read(dialogue, *, issues, binding='D7'):
    action = AIMessage(content='', tool_calls=[{'name': 'inspect_draft', 'args': {}, 'id': 'read'}])
    value = {'status': 'draft', 'draft': deepcopy(DRAFT), 'draft_id': 'D7',
             'issues': deepcopy(issues), 'issues_draft_id': binding}
    dialogue.exchange(action, [('read', value)], result_name='submit_patch')


def initial(**extra):
    return TaskDialogue('Fixed policy', {'draft': DRAFT, 'draft_id': 'D7', 'issues': [OLD],
                                        'issues_draft_id': 'D7', **extra})


def test_same_draft_read_updates_diagnostics_without_duplicating_body():
    dialogue = initial()
    full_read(dialogue, issues=[NEW])
    value = packet(dialogue)
    assert value['draft'] == DRAFT and value['issues'] == [NEW]
    assert value['issues_draft_id'] == value['draft_id'] == 'D7'
    assert len(dialogue.messages()) == 2


def test_explicit_empty_diagnostics_clear_old_errors_for_the_same_draft():
    dialogue = initial()
    full_read(dialogue, issues=[])
    assert not packet(dialogue).get('issues')
    assert not dialogue.latest_draft_feedback


def test_old_bound_diagnostics_are_visible_but_not_rebound_to_current_draft():
    dialogue = initial(issues=[NEW])
    full_read(dialogue, issues=[OLD], binding='D6')
    value = packet(dialogue)
    assert value['issues'] == [NEW] and value['issues_draft_id'] == 'D7'
    assert value['latest_output_error']['issues'] == [OLD]
    assert value['latest_output_error']['issues_draft_id'] == 'D6'


def test_explicit_legacy_none_stays_unbound_and_visible_in_the_initial_packet():
    dialogue = initial(issues_draft_id=None)
    value = packet(dialogue)
    assert value['issues'] == [OLD] and value['issues_draft_id'] is None
    assert value['issues_binding'] == 'unbound_historical'


def test_legacy_none_read_diagnostics_do_not_replace_current_bound_errors():
    dialogue = initial(issues=[NEW])
    full_read(dialogue, issues=[OLD], binding=None)
    value = packet(dialogue)
    assert value['issues'] == [NEW] and value['issues_draft_id'] == 'D7'
    assert value['latest_output_error']['issues'] == [OLD]
    assert value['latest_output_error']['issues_draft_id'] is None


def test_semantic_findings_have_one_presentation_before_and_after_draft_read():
    finding = {'problem': 'Preserve the alternative boundary', 'basis': [{'id': 'N'}]}
    dialogue = initial(issues=[finding], semantic_issues=[finding])
    full_read(dialogue, issues=[finding])
    value = packet(dialogue)
    assert value['semantic_issues'] == [finding] and not value.get('issues')
    assert str(dialogue.messages()).count('Preserve the alternative boundary') == 1


def tool_context(*, errors_binding='D7'):
    return SimpleNamespace(evidence=(), active_draft=deepcopy(DRAFT), task_draft_id='D7',
                           task_draft_errors=(OLD,), task_draft_errors_draft_id=errors_binding)


def test_draft_tool_projection_inherits_full_identity_and_explicit_error_binding():
    ctx = tool_context()
    tool = build_read_tools(ctx, {})['inspect_draft']
    value = tool.invoke({'sections': ['explanation']})
    assert value['draft'] == {'explanation': DRAFT['explanation']}
    assert value['draft_id'] == value['issues_draft_id'] == 'D7'
    assert value['projection']['sections'] == ['explanation']
    assert value['issues'] == [OLD]


def test_draft_tool_must_not_rebind_explicit_none_error_binding():
    value = build_read_tools(tool_context(errors_binding=None), {})['inspect_draft'].invoke({})
    assert value['draft_id'] == 'D7' and value['issues'] == [OLD]
    assert value['issues_draft_id'] is None


def test_draft_tool_explicitly_reports_diagnostic_clear():
    ctx = tool_context()
    ctx.task_draft_errors = ()
    value = build_read_tools(ctx, {})['inspect_draft'].invoke({})
    assert value['issues'] == [] and value['draft_id'] == 'D7'


def cached_dialogue():
    dialogue = initial()
    projected = {'status': 'draft', 'draft': {'explanation': DRAFT['explanation']}, 'draft_id': 'D7',
                 'projection': {'sections': ['explanation']}, 'issues': [OLD], 'issues_draft_id': 'D7'}
    action = AIMessage(content='', tool_calls=[{'name': 'inspect_draft', 'args': {'sections': ['explanation']}, 'id': 'projection'}])
    dialogue.exchange(action, [('projection', projected)], result_name='submit_patch')
    action = AIMessage(content='', tool_calls=[{'name': 'inspect_graph', 'args': {'ids': ['N'], 'view': 'before'}, 'id': 'graph'}])
    dialogue.exchange(action, [('graph', {'view': 'before', 'items': [{'id': 'N', 'name': 'Newly acquired exact fact'}]})], result_name='submit_patch')
    return dialogue


def test_restore_keeps_acquired_facts_but_authoritative_new_errors_override_old_cache():
    saved = cached_dialogue().snapshot()
    restored = initial(issues=[NEW])
    restored.restore(saved)
    value = packet(restored)
    assert value['issues'] == [NEW] and value['issues_draft_id'] == 'D7'
    assert 'Newly acquired exact fact' in str(restored.messages())


def test_restore_authoritative_clear_cannot_be_undone_by_cached_projection_errors():
    restored = initial(issues=[])
    restored.restore(cached_dialogue().snapshot())
    assert not packet(restored).get('issues')
    assert 'Newly acquired exact fact' in str(restored.messages())


def test_restore_without_authoritative_errors_keeps_saved_diagnostics_and_fact_scope():
    saved = cached_dialogue().snapshot()
    restored = TaskDialogue('Fixed policy', {'draft': DRAFT, 'draft_id': 'D7'})
    restored.restore(saved)
    assert packet(restored)['issues'] == [OLD]
    assert restored.premise_hash(policy='p') == cached_dialogue().premise_hash(policy='p')
    saved['reads'].clear()
    saved['issues'].clear()
    assert packet(restored)['issues'] == [OLD]
    assert 'Newly acquired exact fact' in str(restored.messages())


def test_restoring_new_authoritative_draft_does_not_restore_old_draft_projection():
    newer = {'explanation': 'New draft body'}
    restored = TaskDialogue('Fixed policy', {'draft': newer, 'draft_id': 'D8', 'issues': [NEW], 'issues_draft_id': 'D8'})
    restored.restore(cached_dialogue().snapshot())
    value = packet(restored)
    assert value['draft'] == newer and value['draft_id'] == 'D8' and value['issues'] == [NEW]
    assert not any(call['name'] == 'inspect_draft' for msg in restored.messages()
                   if isinstance(msg, AIMessage) for call in msg.tool_calls)
    assert 'Newly acquired exact fact' in str(restored.messages())


def test_agent_read_does_not_rebind_legacy_none_diagnostics():
    from test_graph_integration import state
    from test_incremental_agent import Chat

    from cortex.capabilities.graph_merge.integration.context import IntegrationContext
    from cortex.capabilities.graph_merge.llm.task_agent import (
        IncrementalIntegrationAgent,
    )
    from cortex.schema import load_root_schema

    ctx = IntegrationContext(state(), state(), load_root_schema())
    ctx.candidate = ctx.union_state()
    ctx.active_draft = deepcopy(DRAFT)
    ctx.task_draft_id = 'D7'
    ctx.task_draft_errors = [OLD]
    ctx.task_draft_errors_draft_id = None
    ctx.compile_task_patch = lambda patch: object()
    query = AIMessage(content='', tool_calls=[{'name': 'inspect_graph',
        'args': {'mode': 'items', 'ids': ['missing'], 'view': 'candidate'}, 'id': 'read'}])
    result = AIMessage(content='', tool_calls=[{'name': 'submit_patch', 'args': {'explanation': 'valid'}, 'id': 'result'}])
    model = Chat([query, result])
    IncrementalIntegrationAgent(model).invoke('reconcile', ctx,
        {'draft': DRAFT, 'draft_id': 'D7', 'issues': [OLD], 'issues_draft_id': None})
    value = json.loads(model.requests[1][1].content)
    assert value['issues_draft_id'] is None and value['issues_binding'] == 'unbound_historical'


def test_malformed_saved_read_cannot_partially_mutate_canonical_state():
    import pytest
    saved = cached_dialogue().snapshot()
    saved['reads'][0]['call']['args'] = ['not', 'an', 'object']
    restored = initial(issues=[NEW])
    before = restored.snapshot()
    with pytest.raises((ValueError, TypeError)):
        restored.restore(saved)
    assert restored.snapshot() == before


def test_saved_final_result_cannot_be_restored_as_a_read():
    import pytest
    saved = cached_dialogue().snapshot()
    saved['reads'][0]['call']['name'] = 'submit_patch'
    restored = initial(issues=[NEW])
    before = restored.snapshot()
    with pytest.raises(ValueError):
        restored.restore(saved)
    assert restored.snapshot() == before


def test_fact_supplied_by_compiler_feedback_survives_draft_and_error_replacement():
    feedback = {'code': 'patch_compile', 'error': 'Scope requires the exact neighbour',
                'required_context': {'view': 'before', 'nodes': [{'id': 'N', 'name': 'Exact required neighbour'}]}}
    dialogue = TaskDialogue('Fixed policy', {})
    dialogue.set_draft(DRAFT, feedback)
    before = dialogue.premise_hash(policy='p')
    dialogue.set_draft({'explanation': 'Replacement draft'}, {'code': 'schema', 'error': 'Different error'})
    assert dialogue.premise_hash(policy='p') == before
    assert 'Exact required neighbour' in str(dialogue.messages())
    restored = TaskDialogue('Fixed policy', {'draft': {'explanation': 'Replacement draft'}})
    restored.restore(dialogue.snapshot())
    assert restored.premise_hash(policy='p') == before
    assert 'Exact required neighbour' in str(restored.messages())


def test_compiler_fact_body_is_sent_once_and_candidate_provenance_stays_historical():
    feedback = {'error': 'A precise failure', 'required_context': {'view': 'candidate', 'edges': [
        {'id': 'E', 'edge_type': 'grounded_on', 'from_node_id': 'N', 'to_node_id': 'C',
         'properties': {'description': 'Unique historical fact body'}}]}}
    dialogue = TaskDialogue('Fixed policy', {})
    dialogue.set_draft(DRAFT, feedback, draft_id='D7')
    dialogue.set_draft({'explanation': 'New result'}, {'error': 'New issue'}, draft_id='D8')
    value = packet(dialogue)
    assert value['draft_id'] == 'D8'
    assert len(value['repair_facts']) == 1
    fact = value['repair_facts'][0]
    assert fact['snapshot_role'] == 'historical_candidate'
    assert fact['snapshot_binding']['draft_id'] == 'D7'
    assert str(dialogue.messages()).count('Unique historical fact body') == 1


def test_feedback_references_pinned_body_instead_of_repeating_graph_records():
    feedback = {'error': 'A precise failure', 'required_context': {'view': 'before',
        'nodes': [{'id': 'N', 'name': 'Unique required fact'}]}}
    dialogue = TaskDialogue('Fixed policy', {})
    dialogue.set_draft(DRAFT, feedback)
    value = packet(dialogue)
    ref = value['issues'][0]['required_context']['repair_fact_ref']
    assert value['repair_facts'][0]['fact_id'] == ref
    assert str(dialogue.messages()).count('Unique required fact') == 1
    restored = TaskDialogue('Fixed policy', {'draft': DRAFT})
    restored.restore(dialogue.snapshot())
    assert str(restored.messages()).count('Unique required fact') == 1


def test_before_after_reference_only_corrections_are_not_pinned_as_new_facts():
    dialogue = TaskDialogue('Fixed policy', {})
    dialogue.set_draft(DRAFT, {'error': 'Correct the named scope',
        'before': [{'kind': 'node', 'id': 'N'}], 'after': [{'kind': 'node', 'id': 'M'}]})
    assert not packet(dialogue).get('repair_facts')


def test_missing_pinned_fact_reference_fails_before_restore_mutation():
    import pytest
    saved = cached_dialogue().snapshot()
    saved['issues'] = [{'error': 'scope', 'required_context': {'repair_fact_ref': 'missing'}}]
    restored = initial(issues=[NEW])
    before = restored.snapshot()
    with pytest.raises(ValueError, match='missing compiler facts'):
        restored.restore(saved)
    assert restored.snapshot() == before


def test_counterexample_without_view_is_preserved_without_inventing_current_snapshot():
    dialogue = TaskDialogue('Fixed policy', {})
    dialogue.set_draft(DRAFT, {'error': 'A precise counterexample', 'counterexamples': [
        {'id': 'E', 'edge_type': 'depends_on', 'from_node_id': 'N', 'to_node_id': 'C'}]})
    fact = packet(dialogue)['repair_facts'][0]
    assert fact['view'] == 'unknown' and fact['snapshot_role'] == 'historical_unqualified'
    assert fact['snapshot_binding']['draft_hash'] == dialogue._draft_hash(DRAFT)
    restored = TaskDialogue('Fixed policy', {'draft': DRAFT})
    restored.restore(dialogue.snapshot())
    assert packet(restored)['repair_facts'] == [fact]


@pytest.mark.parametrize('projection', [False, True])
@pytest.mark.parametrize('binding', [None, 'D6'])
def test_old_diagnostic_context_is_pinned_once_with_its_old_binding(projection, binding):
    dialogue = initial(issues=[NEW])
    old_issue = {'error': 'Original old scope error', 'required_context': {'view': 'candidate',
        'nodes': [{'id': 'N', 'name': 'Single historical diagnostic fact'}]}}
    args = {'sections': ['explanation']} if projection else {}
    result = {'status': 'draft', 'draft': {'explanation': DRAFT['explanation']} if projection else deepcopy(DRAFT),
              'draft_id': 'D7', 'issues': [old_issue], 'issues_draft_id': binding}
    if projection:
        result['projection'] = {'sections': ['explanation']}
    response = AIMessage(content='', tool_calls=[{'name': 'inspect_draft', 'args': args, 'id': 'old'}])
    dialogue.exchange(response, [('old', result)], result_name='submit_patch')
    value = packet(dialogue)
    assert value['issues'] == [NEW] and value['issues_draft_id'] == 'D7'
    assert value['latest_output_error']['issues'][0]['error'] == old_issue['error']
    ref = value['latest_output_error']['issues'][0]['required_context']['repair_fact_ref']
    fact = value['repair_facts'][0]
    assert fact['fact_id'] == ref and fact['snapshot_binding']['draft_id'] == binding
    assert fact['snapshot_binding']['draft_hash'] is None
    assert str(dialogue.messages()).count('Single historical diagnostic fact') == 1
    restored = initial(issues=[NEW])
    restored.restore(dialogue.snapshot())
    assert str(restored.messages()).count('Single historical diagnostic fact') == 1
    assert packet(restored)['issues'] == [NEW]


def test_current_projection_diagnostics_share_pinned_body_and_keep_error_text():
    dialogue = initial()
    issue = {'error': 'Current precise error', 'required_context': {'view': 'before',
        'nodes': [{'id': 'N', 'name': 'One projected diagnostic fact'}]}}
    result = {'status': 'draft', 'draft': {'explanation': DRAFT['explanation']}, 'draft_id': 'D7',
              'projection': {'sections': ['explanation']}, 'issues': [issue], 'issues_draft_id': 'D7'}
    response = AIMessage(content='', tool_calls=[{'name': 'inspect_draft',
        'args': {'sections': ['explanation']}, 'id': 'part'}])
    dialogue.exchange(response, [('part', result)], result_name='submit_patch')
    value = packet(dialogue)
    assert value['issues'][0]['error'] == issue['error']
    assert str(dialogue.messages()).count('One projected diagnostic fact') == 1
    assert value['issues'][0]['required_context']['repair_fact_ref'] == value['repair_facts'][0]['fact_id']


def test_stale_full_draft_read_preserves_its_diagnostic_facts_without_replacing_current():
    newer = {'explanation': 'Current D8'}
    dialogue = initial(draft=newer, draft_id='D8', issues=[NEW], issues_draft_id='D8')
    old_issue = {'error': 'Old precise error', 'required_context': {'view': 'candidate',
        'nodes': [{'id': 'N', 'name': 'One stale diagnostic fact'}]}}
    full_read(dialogue, issues=[old_issue], binding='D7')
    value = packet(dialogue)
    assert value['draft'] == newer and value['draft_id'] == 'D8' and value['issues'] == [NEW]
    assert value['latest_output_error']['code'] == 'stale_draft_read'
    assert value['latest_output_error']['issues'][0]['error'] == 'Old precise error'
    assert value['repair_facts'][0]['snapshot_binding']['draft_id'] == 'D7'
    assert str(dialogue.messages()).count('One stale diagnostic fact') == 1
