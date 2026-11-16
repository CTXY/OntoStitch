"""Rejected proposals are editable drafts, never partially applied graph edits."""
from copy import deepcopy
from types import SimpleNamespace as N

import pytest
from pydantic import ValidationError

from cortex.capabilities.graph_merge.integration.models import Element
from cortex.capabilities.graph_merge.integration.proposal_repair import ProposalRepair, PatchSubmission, CorrectionError
from cortex.capabilities.graph_merge.integration.provenance import SourceCoverageError
from cortex.capabilities.graph_merge.integration.relation_edits import RelationPatch
from cortex.schema import SourceReference


def context(*, evidence=(), nodes=(), edges=()):
    return N(active_batch=N(preview=None, seed=N(graph=N(nodes=nodes, edges=edges))), evidence=evidence)


def test_raw_invalid_patch_is_saved_and_corrected_without_losing_valid_parts():
    repair = ProposalRepair()
    raw = {'explanation': 'Keep a valid local decision.', 'edge_updates': [], 'new_edges': []}
    repair.prepare({'patch': raw})
    with pytest.raises(ValidationError) as failure:
        RelationPatch.model_validate(raw)
    feedback = repair.feedback(failure.value, context())
    assert feedback['status'] == 'rejected_unapplied'
    assert feedback['errors'][0]['path'] == '/new_edges'
    assert 'create_edges' in feedback['errors'][0]['details']['allowed_fields']
    fixed = repair.prepare({'corrections': [{'op': 'move', 'from_path': '/new_edges', 'path': '/create_edges'}]})
    assert fixed == {'explanation': raw['explanation'], 'edge_updates': [], 'create_edges': []}
    RelationPatch.model_validate(fixed)


def test_move_collision_rejects_entire_correction_batch():
    repair = ProposalRepair()
    repair.prepare({'patch': {'new_edges': [{'id': 'A'}], 'create_edges': [{'id': 'B'}], 'explanation': 'old'}})
    old = deepcopy(repair.draft)
    with pytest.raises(CorrectionError, match='already exists'):
        repair.prepare({'corrections': [
            {'op': 'replace', 'path': '/explanation', 'value': 'changed'},
            {'op': 'move', 'from_path': '/new_edges', 'path': '/create_edges'}]})
    assert repair.draft == old and repair.revision == 1


@pytest.mark.parametrize('invalid', [None, 1, 'not a list', {'id': 'x'}])
def test_feedback_safely_handles_invalid_draft_structure(invalid):
    repair = ProposalRepair()
    raw = repair.prepare({'patch': {'explanation': 'x', 'edge_updates': invalid}})
    with pytest.raises(ValidationError) as failure:
        RelationPatch.model_validate(raw)
    assert repair.feedback(failure.value, context())['status'] == 'rejected_unapplied'


def test_failed_correction_retains_draft_and_reports_correction_location():
    repair = ProposalRepair()
    repair.prepare({'patch': {'explanation': 'x'}})
    with pytest.raises(CorrectionError) as failure:
        repair.prepare({'corrections': [{'op': 'replace', 'path': '/explanation'}]})
    result = repair.feedback(failure.value, context())
    assert result['errors'][0]['path'] == '/corrections/0'
    assert 'unchanged' in result['errors'][0]['message']
    assert repair.draft == {'explanation': 'x'}


def test_missing_new_object_is_not_described_as_created():
    repair = ProposalRepair()
    repair.prepare({'patch': {'explanation': 'x', 'edge_updates': [{'id': 'E_new'}]}})
    result = repair.feedback(ValueError('unknown current edge'), context())
    issue = result['errors'][1]
    assert issue['path'] == '/edge_updates/0/id'
    assert issue['details'] == {'object_id': 'E_new', 'exists_in_candidate': False}
    assert 'create_edges' in issue['message']


def test_source_feedback_uses_original_retirement_path_and_reports_unmapped_sources():
    before = (Element(kind='edge', id='E_before'),)
    after = (Element(kind='edge', id='E_after'),)
    first, second = SourceReference(source_id='one'), SourceReference(source_id='two')
    repair = ProposalRepair()
    repair.prepare({'patch': {'explanation': 'x', 'retirements': [
        {'before': [r.model_dump(exclude_none=True) for r in before], 'after': [r.model_dump(exclude_none=True) for r in after], 'explanation': 'replacement'}]}})
    failure = SourceCoverageError('internal changes[17]', before=before, after=after, missing={first, second})
    result = repair.feedback(failure, context(evidence=(N(id='Q1', source=first),)))
    issue = result['errors'][0]
    assert issue['path'] == '/retirements/0'
    assert issue['details']['missing_evidence_ids'] == ['Q1']
    assert issue['details']['unmapped_source_count'] == 1
    assert 'changes[17]' not in str(result)


@pytest.mark.parametrize('arguments', [{}, {'patch': {}, 'corrections': []}, {'patch': {} ,'unknown': 1}, {'corrections': []}])
def test_invalid_envelope_does_not_replace_saved_draft(arguments):
    repair = ProposalRepair()
    repair.prepare({'patch': {'explanation': 'saved'}})
    with pytest.raises(ValueError):
        repair.prepare(arguments)
    assert repair.draft == {'explanation': 'saved'}


def test_public_envelope_schema_exposes_patch_and_corrections():
    schema = PatchSubmission.model_json_schema()
    assert 'RelationPatch' in schema['$defs']
    assert {'ValueCorrection', 'RemoveCorrection', 'MoveCorrection'} <= schema['$defs'].keys()
    with pytest.raises(ValueError):
        PatchSubmission.model_validate({'patch': {'explanation': 'x'}, 'corrections': [{'op': 'remove', 'path': '/x'}]})


def test_full_patch_replaces_previous_draft_and_explicit_null_is_valid_correction_value():
    repair = ProposalRepair()
    repair.prepare({'patch': {'explanation': 'old', 'old': 1}})
    repair.prepare({'patch': {'explanation': 'new', 'failure': 'null'}})
    fixed = repair.prepare({'corrections': [{'op': 'replace', 'path': '/failure', 'value': None}]})
    assert fixed == {'explanation': 'new', 'failure': None}


@pytest.mark.parametrize('field,key', [
    ('create_nodes', 'evidence_ids'), ('create_edges', 'evidence_ids'),
    ('node_updates', 'add_evidence'), ('edge_updates', 'add_evidence'),
])
def test_unknown_evidence_feedback_locates_only_invalid_ids_and_preserves_original_error(field, key):
    repair = ProposalRepair()
    repair.prepare({'patch': {'explanation': 'x', field: [
        {'id': 'existing', key: ['Q1']},
        {'id': 'existing', key: ['Q1', 'wrong', 'wrong']},
    ]}})
    result = repair.feedback(ValueError('original validation failure'),
                             context(evidence=(N(id='Q1'),), nodes=(N(id='existing'),)))
    assert result['errors'] == [
        {'message': 'original validation failure'},
        {'path': f'/{field}/1/{key}',
         'message': 'Unknown evidence IDs; use available evidence IDs supporting this object.',
         'details': {'unknown_evidence_ids': ['wrong']}},
    ]


def test_schema_failure_does_not_add_secondary_evidence_diagnostics():
    repair = ProposalRepair()
    raw = repair.prepare({'patch': {'explanation': 'x', 'edge_updates': [
        {'id': 'existing', 'add_evidence': 'wrong'},
    ]}})
    with pytest.raises(ValidationError) as failure:
        RelationPatch.model_validate(raw)
    result = repair.feedback(failure.value, context())
    assert len(result['errors']) == 1
    assert result['errors'][0]['path'] == '/edge_updates/0/add_evidence'
    assert 'unknown_evidence_ids' not in str(result)


def test_correction_schema_requires_only_operation_specific_fields():
    definitions = PatchSubmission.model_json_schema()['$defs']
    for name, fields in (
        ('MoveCorrection', {'op', 'path', 'from_path'}),
        ('ValueCorrection', {'op', 'path', 'value'}),
        ('RemoveCorrection', {'op', 'path'}),
    ):
        schema = definitions[name]
        assert set(schema['required']) == fields
        assert set(schema['properties']) == fields
        assert schema['additionalProperties'] is False


def test_missing_move_source_has_actionable_feedback_and_can_be_retried():
    repair = ProposalRepair()
    repair.prepare({'patch': {'explanation': 'x', 'new_edges': []}})
    with pytest.raises(CorrectionError) as failure:
        repair.prepare({'corrections': [{'op': 'move', 'path': '/create_edges'}]})
    issue = repair.feedback(failure.value, context())['errors'][0]
    assert issue['path'] == '/corrections/0'
    assert 'from_path: Field required' in issue['message']
    assert set(issue['details']['required_fields']) == {'op', 'path', 'from_path'}
    assert 'value' not in issue['details']['allowed_fields']
    assert repair.revision == 1
    fixed = repair.prepare({'corrections': [
        {'op': 'move', 'from_path': '/new_edges', 'path': '/create_edges'}]})
    assert fixed == {'explanation': 'x', 'create_edges': []}


@pytest.mark.parametrize('correction', [
    {'op': 'move', 'from_path': '/old', 'path': '/new', 'value': None},
    {'op': 'remove', 'path': '/old', 'from_path': '/new'},
    {'op': 'replace', 'path': '/old'},
])
def test_operation_specific_correction_contract_rejects_extra_or_missing_fields(correction):
    repair = ProposalRepair()
    repair.prepare({'patch': {'explanation': 'x', 'old': 1}})
    with pytest.raises(CorrectionError):
        repair.prepare({'corrections': [correction]})
    assert repair.draft == {'explanation': 'x', 'old': 1}
