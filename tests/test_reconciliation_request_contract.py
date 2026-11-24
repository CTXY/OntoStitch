"""No public decision ledger; exact local fields and stable draft handles."""
import pytest
from cortex.capabilities.graph_merge.integration.reconciliation_contract import (
    AssessmentResult,DraftDocument,EditDraft,TOOLS,apply_edit_shape,NodeFields,validate_combinations)


def change(**kw):
    return {'change_id':'c','kind':'edge_update','edge_id':'e','reason':'Preserve the relationship.',
        'set':{'properties':{'description':'Explicit business relation'}},**kw}


def test_public_output_has_only_two_fields_and_no_categories():
    assert set(AssessmentResult.model_fields)=={'explanation','feedback'}
    assert set(EditDraft.model_fields)=={'put_changes','remove_changes'}
    assert 'submit_draft' not in TOOLS and 'decisions' not in DraftDocument.model_fields
    assert apply_edit_shape(DraftDocument(task_id='t',before_id='b',version=0),EditDraft()).version==0
    assert 'AlternativeUpdate' not in str(EditDraft.model_json_schema())


@pytest.mark.parametrize('extra',[{'expected_version':0},{'operations':[]},{'put_decision':{}},{'status':'repair'}])
def test_obsolete_inputs_rejected(extra):
    with pytest.raises(ValueError): EditDraft.model_validate({'put_changes':[change()],**extra})


@pytest.mark.parametrize('extra',[{'verdict':'accept'},{'findings':[]},{'kind':'defect'},{'signal_reviews':[]}])
def test_assessment_has_no_routing_or_verdict(extra):
    with pytest.raises(ValueError): AssessmentResult.model_validate({'explanation':'Valid','feedback':[],**extra})


def test_identical_edits_do_not_change_version_and_remove_is_draft_only():
    doc=DraftDocument(task_id='task',before_id='hash',version=0)
    first=apply_edit_shape(doc,{'put_changes':[change()]})
    assert apply_edit_shape(first,{'put_changes':[change()]})==first
    final=apply_edit_shape(first,{'remove_changes':['c']})
    assert final.version==2 and not final.changes
    with pytest.raises(ValueError): apply_edit_shape(first,{'remove_changes':['missing']})
    with pytest.raises(ValueError,match='STALE_REQUEST'): apply_edit_shape(first,{'put_changes':[change()]},request_version=0)


def test_one_edit_per_handle_and_one_write_per_object():
    with pytest.raises(ValueError): EditDraft(put_changes=[change(),change()])
    with pytest.raises(ValueError): EditDraft(put_changes=[change()],remove_changes=['c'])
    doc=DraftDocument(task_id='t',before_id='h',version=1,changes=[change(),change(change_id='other')])
    with pytest.raises(ValueError): validate_combinations(doc.changes)


def test_explicit_unset_and_null_contract():
    with pytest.raises(ValueError): NodeFields(name=None)
    assert NodeFields(description=None).model_dump(exclude_unset=True)=={'description':None}


def test_replace_requires_distinct_survivor_and_no_chains():
    def row(a,b,c): return {'change_id':c,'kind':'edge_replace','edge_id':a,'replacement_edge_id':b,'reason':'Retain supported knowledge.'}
    with pytest.raises(ValueError): DraftDocument(task_id='t',before_id='h',version=1,changes=[row('a','a','c')])
    doc=DraftDocument(task_id='t',before_id='h',version=1,changes=[row('a','b','c'),row('b','c','d')])
    with pytest.raises(ValueError): validate_combinations(doc.changes)


@pytest.mark.parametrize('text',[ '{"explanation":"ok","feedback":[]}',
    '```json\n{"explanation":"ok","feedback":[]}\n```',
    '```\n{"explanation":"ok","feedback":[]}\n```'])
def test_one_exact_transport_envelope_is_normalized_without_semantic_repair(text):
    from cortex.capabilities.graph_merge.llm.reconciliation_agent import _json_object
    assert AssessmentResult.model_validate(_json_object(text)).feedback==()


@pytest.mark.parametrize('text',['Here is the result: {"explanation":"ok","feedback":[]}',
    '```json\n{"explanation":"ok","feedback":[]}\n```\nextra explanation',
    '{"explanation":"ok","feedback":[],"feedback":[]}',
    '[]','{} {}'])
def test_narrative_multiple_results_and_duplicate_keys_stay_invalid(text):
    from cortex.capabilities.graph_merge.llm.reconciliation_agent import _json_object
    with pytest.raises(ValueError): _json_object(text)
