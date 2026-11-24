"""Readable prompts preserve public facts, bounded history and cache prefixes."""
from copy import deepcopy
import json

import pytest
from test_reconciliation_context import runtime
from cortex.capabilities.graph_merge.reconciliation.presentation import state_prompt, user_assets


@pytest.mark.parametrize('phase',['reconcile','graph_assess'])
def test_role_specific_user_prompts_explain_task_and_material(phase):
    rt=runtime(phase=phase);messages=rt.messages()
    assert [m.type for m in messages]==['system','human','human']
    assert 'task for this round' in messages[1].content.lower() and '# Local graph before modification' in messages[1].content
    assert '# Original evidence' in messages[1].content
    assert 'No historical case graph or retired node snapshot is available.' in messages[1].content
    assert ('# Assessment task for this round' if phase=='graph_assess' else '# Task for this round') in messages[1].content
    forbidden=('initial_complete_bundle','immutable_task','read_supplements','canonical_draft','content_change',
               'bundle_ready','source_snapshot_id','before_hash','candidate_hash','latest_receipt','id_to_body','coverage')
    assert not any(key in ''.join(m.content for m in messages[1:]) for key in forbidden)
    assert len(user_assets())==4


def test_first_valid_empty_draft_has_no_status_lists_or_empty_recovery_receipts():
    rt=runtime();rt.set_compiled_candidate(rt.before,rt.draft.version)
    rt.set_feedback({'mechanical':None,'latest_receipt':{'draft_saved':True,'errors':[]}})
    text=state_prompt(rt)
    assert 'No modification has been proposed yet' in text
    assert '```json' not in text and 'errors' not in text and 'unchanged' not in text


def test_prefix_unchanged_after_edit_compile_evidence_and_feedback_updates():
    rt=runtime();prefix=rt.messages()[:2]
    base=rt.before;edges=tuple(e.model_copy(update={'properties':{**e.properties,'description':'A meaningful revised relationship.'}})
        if e.id=='e1' else e for e in base.graph.edges)
    rt.set_compiled_candidate(base.model_copy(update={'graph':base.graph.model_copy(update={'edges':edges})}),rt.draft.version)
    rt.read_evidence(ids=['ev-A']);rt.set_feedback({'protocol_error':{'error':'Use the public edit fields.'}})
    assert rt.messages()[:2]==prefix
    assert 'A meaningful revised relationship.' in rt.messages()[2].content
    assert 'targets' not in rt.messages()[2].content
    assert rt.evidence_view['bodies']
    assert 'id_to_body' not in rt.messages()[2].content


def test_assessment_sees_full_actual_changed_content_without_writer_patch_duplication():
    rt=runtime(phase='graph_assess');before=rt._candidate
    node=next(n for n in before.graph.nodes if n.id=='N1')
    updated=node.model_copy(update={'properties':{**node.properties,'definition':'ACTUAL_CANDIDATE_DEFINITION'}})
    state=before.model_copy(update={'graph':before.graph.model_copy(update={'nodes':tuple(updated if n.id=='N1' else n for n in before.graph.nodes)})})
    rt.set_compiled_candidate(state,rt.draft.version)
    text=rt.messages()[2].content
    assert text.count('ACTUAL_CANDIDATE_DEFINITION')==1
    assert 'Saved modification draft' not in text and 'Modified Node' in text
    assert json.dumps(updated.properties['targets']) in text or '"targets"' in text


def test_error_dedup_does_not_drop_distinct_structured_details():
    rt=runtime();error={'code':'BAD_ENDPOINT','path':'put_changes.0.set.to_node_id','object_ids':['missing']}
    other={'code':'BAD_FIELD','path':'put_changes.0.extra'}
    rt.set_feedback({'mechanical':error,'protocol_error':other,'latest_receipt':{'draft_saved':True,'errors':[error,other]}})
    text=rt.messages()[2].content
    assert text.count('BAD_ENDPOINT')==1 and text.count('BAD_FIELD')==1
    assert 'put_changes.0.set.to_node_id' in text and 'missing' in text
    assert rt.snapshot()['feedback']['latest_receipt']['errors']==[error,other]


def test_hidden_bookkeeping_does_not_create_new_visible_facts():
    rt=runtime();old=rt.premise_hash()
    rt.working['session']['candidate_hash']='different internal hash'
    rt.coverage[0]['key']='internal coverage bookkeeping'
    assert rt.premise_hash()==old
    rt.set_feedback({'protocol_error':{'error':'A feedback change is not a newly read fact.'}})
    assert rt.premise_hash()==old
    rt.read_evidence(ids=['ev-A'])
    assert rt.premise_hash()!=old


def test_source_selection_evidence_is_explicit_without_historical_graph_reads():
    rt=runtime(signals=[{'id':'history','kind':'source_selection_variants',
        'node_ids':['N1'], 'variants':[{'node_id':'N1', 'evidence_id':'ev-A',
                                     'selected_physical_inputs':['a']}]}])
    assert not hasattr(rt, '_source_handles')
    assert 'ev-A' in ''.join(message.content for message in rt.messages())
    rt.read_evidence(ids=['ev-A'])
    assert rt.evidence_view['id_to_body']['ev-A']
    assert 'original' in rt.messages()[2].content
    with pytest.raises(ValueError, match='OUT_OF_SCOPE'):
        rt.read_nodes(ids=['source:N1'])
    assert 'source_snapshot_id' not in rt.messages()[2].content


def test_supplemental_original_fact_not_falsely_reported_deleted():
    rt=runtime(phase='graph_assess')
    # Runtime may know an extra fixed-before object before the corresponding
    # candidate public read; authoritative candidate existence wins.
    original=deepcopy(rt.working['candidate']['nodes'].pop('N1'))
    assert 'Deleted Node' not in state_prompt(rt)
    rt.working['candidate']['nodes']['N1']=original


def test_source_selection_keeps_current_node_content_without_source_handles():
    from test_reconciliation_context import node_context
    ctx=node_context()
    current=next(node for node in ctx.task_before.graph.nodes if node.id=='N1')
    ctx.raw_nodes={'N1':current.model_copy(update={'name':'ARCHIVED_SOURCE_MEANING'})}
    ctx.raw_edges={}
    rt=runtime(ctx=ctx,signals=[{'id':'source','kind':'source_selection_variants',
        'node_ids':['N1'], 'variants':[{'node_id':'N1', 'evidence_id':'ev-A',
                                     'selected_physical_inputs':['a']}]}])
    text=''.join(message.content for message in rt.messages())
    assert rt.working['before']['nodes']['N1']['name']==current.name
    assert 'ARCHIVED_SOURCE_MEANING' not in text
    assert 'source_handle' not in text and 'source_snapshot_id' not in text
    assert 'input-source:' not in text


def test_required_outcome_and_original_task_are_explicit_even_when_unchanged():
    rt=runtime(phase='graph_assess',signals=[{'id':'required','kind':'value_concepts','mode':'required',
        'check_question':'补齐业务维度 Concept。','required_outcome':'物理 Column 不能替代此业务 Concept。','node_ids':['N1']}])
    assert 'Specific task for this round (original text):补齐业务维度 Concept。' in rt.messages()[1].content
    assert 'Required outcome:物理 Column 不能替代此业务 Concept。' in rt.messages()[1].content
    assert 'retaining original knowledge does not mean the task is complete' in rt.messages()[2].content
