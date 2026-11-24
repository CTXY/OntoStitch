"""Public patch contracts must match compile/scope feedback behavior."""
import pytest

from cortex.capabilities.graph_merge.graph.edits import RelationPatch, submit_patch
from cortex.capabilities.graph_merge.graph.scope import ScopeContextRequired
from test_merge_simplified_interface import region


def test_null_removes_optional_relation_metadata_without_removing_bindings():
    ctx, ids, _, _ = region()
    submit_patch(ctx, RelationPatch(node_updates=[{
        'id': ids['A'], 'properties': {'relation_metadata': []},
        'explanation': 'A legal empty grouping property.'}], explanation='Store grouping.'))
    before = next(n for n in ctx.active_batch.preview.graph.nodes if n.id == ids['A'])
    assert before.properties['relation_metadata'] == []
    submit_patch(ctx, RelationPatch(node_updates=[{
        'id': ids['A'], 'properties': {'relation_metadata': None},
        'explanation': 'Remove unused grouping property.'}], explanation='Remove grouping.'))
    after = next(n for n in ctx.active_batch.preview.graph.nodes if n.id == ids['A'])
    assert 'relation_metadata' not in after.properties
    assert after.properties['targets'] == before.properties['targets']
    assert after.sources == before.sources


@pytest.mark.parametrize('field,kind', [('node_updates', 'node'), ('edge_updates', 'edge'), ('retirements', 'edge'), ('retirements', 'node')])
def test_unknown_current_object_returns_field_specific_error_without_mutation(field, kind):
    ctx, _, _, _ = region()
    data = ({'before': [{'kind': kind, 'id': 'missing'}], 'after': [], 'explanation': 'Remove old assertion.'}
            if field == 'retirements' else {'id': 'missing', 'explanation': 'Update assertion.'})
    patch = RelationPatch.model_validate({field: [data], 'explanation': 'Local repair.'})
    with pytest.raises(ValueError, match=rf'{field}.*unknown current {kind} missing'):
        submit_patch(ctx, patch)
    assert not ctx.active_batch.pending and ctx.active_batch.preview is None


def test_name_only_scope_feedback_does_not_echo_inherited_targets():
    ctx, ids, _, _ = region(visible=False)
    patch = RelationPatch(node_updates=[{'id': ids['B'], 'name': 'Representative role',
                                        'explanation': 'Normalize existing referent.'}],
                          explanation='Naming correction.')
    with pytest.raises(ScopeContextRequired) as error:
        submit_patch(ctx, patch)
    properties = error.value.context['nodes'][0].get('properties', {})
    assert 'targets' not in properties
    assert ctx.active_batch.preview is None
    submit_patch(ctx, patch)
    after = next(n for n in ctx.active_batch.preview.graph.nodes if n.id == ids['B'])
    before = next(n for n in ctx.active_batch.seed.graph.nodes if n.id == ids['B'])
    assert after.properties == before.properties


def test_scope_feedback_includes_only_changed_property_old_value():
    ctx, ids, _, _ = region(visible=False)
    patch = RelationPatch(node_updates=[{'id': ids['B'], 'properties': {'definition': 'A clearer same-role definition.'},
                                        'explanation': 'Clarify the same referent.'}],
                          explanation='Definition clarification.')
    with pytest.raises(ScopeContextRequired) as error:
        submit_patch(ctx, patch)
    properties = error.value.context['nodes'][0]['properties']
    assert 'definition' in properties and 'targets' not in properties


@pytest.mark.parametrize('refs', [[], ()])
def test_revise_requires_an_affected_object_reference(refs):
    from cortex.capabilities.graph_merge.consolidation.review import Review
    with pytest.raises(ValueError):
        Review(verdict='revise', issues=[{'refs': refs, 'basis': [{'kind':'edge','id':'edge.one'}], 'problem':'Incorrect attribute bearer.', 'required_outcome':'Attach the attribute to its true bearer.'}])


def test_structural_review_accepts_cross_object_references_without_field_paths():
    from cortex.capabilities.graph_merge.consolidation.review import Review
    review = Review(verdict='revise', issues=[{'refs': [{'kind':'edge','id':'edge.one'}, {'kind':'node','id':'concept.bearer'}], 'basis':[{'kind':'edge','id':'edge.one'}], 'problem':'The relation is attached to the wrong bearer.', 'required_outcome':'Use the original supported bearer.'}])
    assert review.verdict == 'revise'
    assert all(not ref.fields for ref in review.issue_refs)


def test_review_acceptance_does_not_require_references():
    from cortex.capabilities.graph_merge.consolidation.review import Review
    assert Review(verdict='accept').issue_refs == ()


def test_unknown_field_feedback_does_not_advertise_blocked_whole_properties():
    from test_light_reconciliation import setup
    from cortex.capabilities.graph_merge.evidence import EvidencePresentation
    from cortex.capabilities.graph_merge.reconciliation.reads import RelationReads
    ctx, _, a, _, _ = setup()
    reply = RelationReads(ctx, EvidencePresentation(ctx)).read('items', [a], ['targets'])
    fields = reply['read_errors'][0]['available_fields']
    assert 'properties' not in fields
    assert 'properties.targets' in fields



def test_edge_merge_schema_explains_edge_ids_and_separate_property_update():
    from cortex.capabilities.graph_merge.graph.batch import EdgeMerge
    fields = EdgeMerge.model_json_schema()['properties']
    assert set(fields) == {'source_ids', 'survivor_id', 'explanation'}
    assert 'Graph EDGE IDs' in fields['source_ids']['description']
    assert 'not evidence IDs' in fields['source_ids']['description']
    assert 'edge_updates using id' in fields['survivor_id']['description']


def test_canonical_edit_examples_match_the_actual_public_tool_schema():
    from cortex.capabilities.graph_merge.reconciliation.contract import EditDraft
    edit=EditDraft.model_validate({"put_changes":[
        {"change_id":"create","kind":"edge_create","edge_id":"new","edge_type":"has_feature",
         "from_node_id":"A","to_node_id":"B","properties":{"description":"An evidence-supported relationship"},
         "evidence_ids":["evidence"],"reason":"Create a concrete relation"},
        {"change_id":"update","kind":"edge_update","edge_id":"existing",
         "set":{"properties":{"description":"A clarified relation"}},"evidence_ids":["evidence"],
         "reason":"Preserve and clarify the existing relation"}]})
    assert edit.put_changes[0].evidence_ids and edit.put_changes[1].evidence_ids
    assert set(EditDraft.model_json_schema()["properties"])=={"put_changes","remove_changes"}
    assert not hasattr(edit,"patch") and not hasattr(edit,"corrections")


def test_validation_feedback_keeps_all_paths_without_echoing_patch():
    from pydantic import ValidationError
    from cortex.capabilities.graph_merge.graph.edits import RelationPatch
    from cortex.capabilities.graph_merge.runtime.feedback import validation_feedback
    with pytest.raises(ValidationError) as caught:
        RelationPatch.model_validate({'edge_merges': [{'kept_edge_id': 'SECRET_BODY_SENTINEL'}]})
    text = validation_feedback(caught.value)
    assert 'edge_merges.0.source_ids' in text and 'edge_merges.0.survivor_id' in text
    assert 'edge_merges.0.kept_edge_id' in text and 'explanation' in text
    assert 'SECRET_BODY_SENTINEL' not in text and 'https://' not in text


@pytest.mark.parametrize('model,data', [
    ('NodeUpdate', {'id':'node', 'relation_metadata':[], 'explanation':'Old spelling.'}),
    ('EdgeUpdate', {'id':'edge', 'description':'Old spelling.'}),
    ('EdgeUpdate', {'id':'edge', 'branches':[]}),
    ('NewEdge', {'id':'edge', 'edge_type':'has_feature', 'from_node_id':'left', 'to_node_id':'right',
                 'properties':{'description':'A real relation.'}, 'evidence':['Q1']}),
])
def test_obsolete_field_aliases_are_rejected_instead_of_silently_rewritten(model, data):
    import cortex.capabilities.graph_merge.graph.edits as relation_edits
    with pytest.raises(ValueError):
        getattr(relation_edits, model).model_validate(data)
