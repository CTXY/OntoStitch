import json
import asyncio
import pytest
import run_merged_graph_ea as runner
from run_merged_graph_ea import ARMS, source_case_ids, summarize


def test_paired_statistics_does_not_compare_different_coverage():
    yes = {'evaluation': {'exec': 1}}
    no = {'evaluation': {'exec': 0}, 'error_stage': 'sql_execution', 'error': 'bad SQL'}
    rows = [{'source_id': 'A', 'plain': yes, 'guided': no},
            {'source_id': 'B', 'plain': yes, 'guided': None},
            {'source_id': 'C', 'plain': no, 'guided': yes},
            {'source_id': 'D', 'plain': yes, 'guided': {'error_stage': 'generation', 'error': 'timeout'}}]
    result = runner.paired_statistics(rows)['guided']
    assert result['paired_cases'] == 2
    assert result['baseline_ea'] == result['arm_ea'] == .5
    assert result['gains'] == ['C'] and result['regressions'] == ['A']


def test_holdout_audit_includes_alternative_and_relation_sources():
    graph = {'graph': {'nodes': [{'sources': [{'case_id': 'bird-thrombosis_prediction-1'}],
        'alternatives': [{'sources': [{'source_id': 'bird-thrombosis_prediction-2'}]}]}],
        'edges': [{'sources': [{'source_id': 'bird-thrombosis_prediction-3'}]}]}}
    assert source_case_ids(graph) == {'1', '2', '3'}


def test_ea_denominator_includes_pending_and_sql_errors(tmp_path):
    cases = [{'source_id': str(i)} for i in range(3)]
    folder = tmp_path/'cases/0'; folder.mkdir(parents=True)
    for arm in ARMS:
        (folder/f'{arm}_prediction.json').write_text(json.dumps({'evaluation': {'exec': 1}}))
    folder = tmp_path/'cases/1'; folder.mkdir()
    (folder/'plain_prediction.json').write_text(json.dumps({'error_stage': 'sql_execution', 'error': 'bad SQL'}))
    (folder/'guided_prediction.json').write_text(json.dumps({'error_stage': 'generation', 'error': 'timeout'}))
    _, totals = summarize(cases, tmp_path)
    assert totals['plain']['finished'] == 2 and totals['plain']['errors'] == 1
    assert totals['guided']['finished'] == 1
    assert all(row['ea_fixed_denominator'] == 1/3 for row in totals.values())
    assert all(row['total'] == 3 and row['provisional'] for row in totals.values())


def test_guided_only_regression_does_not_require_or_count_other_arms(tmp_path):
    folder = tmp_path/'cases/one'; folder.mkdir(parents=True)
    (folder/'guided_prediction.json').write_text(json.dumps({'evaluation': {'exec': 1}}))
    rows, totals = summarize([{'source_id': 'one'}], tmp_path, arms=('guided',))
    assert set(totals) == {'guided'} and totals['guided']['correct'] == 1
    assert not totals['guided']['provisional']
    assert 'plain' not in rows[0] and 'retrieved_memory' not in rows[0]


def test_completed_successes_and_ea_failures_are_not_queued_again(tmp_path, monkeypatch):
    folder = tmp_path/'cases/one'; folder.mkdir(parents=True)
    for arm in ARMS:
        (folder/f'{arm}_prediction.json').write_text(json.dumps({'evaluation': {'exec': 0}, 'error': None}))
    assert runner.pending_arms(folder) == ()
    def forbidden(*args, **kwargs):
        raise AssertionError('Completed case must not create another model')
    monkeypatch.setattr(runner, 'create_chat_model', forbidden)
    evaluation = runner.Evaluation.__new__(runner.Evaluation)
    evaluation.root = tmp_path; evaluation.arms = ARMS
    asyncio.run(evaluation.one({'source_id': 'one'}, 0))


def test_only_missing_or_interrupted_arms_need_continuation(tmp_path):
    (tmp_path/'plain_prediction.json').write_text(json.dumps({'evaluation': {'exec': 1}}))
    (tmp_path/'guided_prediction.json').write_text(json.dumps({'error_stage': 'generation', 'error': 'connection error'}))
    assert runner.pending_arms(tmp_path) == ('retrieved_memory', 'guided')


@pytest.mark.parametrize('arm,memory_type', [('plain', 'none'),
    ('retrieved_memory', 'reference_guidance'), ('guided', 'downstream_guidance')])
def test_normalized_online_cases_match_actual_baseline_input_contract(tmp_path, monkeypatch, arm, memory_type):
    from nl2sql_baseline.io import load_qa_records
    class StubWorkflow:
        def __init__(self, settings, **kwargs):
            self.settings = settings
        def evaluate(self, output, **kwargs):
            rows = load_qa_records(self.settings.test_set_path)
            assert len(rows) == 1 and rows[0].sql == 'SELECT 1'
            assert self.settings.memory_type == memory_type
            if memory_type != 'none':
                assert self.settings.guidance_dir.is_dir()
            output.mkdir()
            (output/'predictions.jsonl').write_text(json.dumps({'evaluation': {'exec': 1}}))
            return output, {}
    monkeypatch.setattr(runner, 'Workflow', StubWorkflow)
    case = {'source_id': 'test', 'database_id': 'demo', 'question': 'One?', 'sql': 'SELECT 1',
            'evaluation': {'row_order': 'ignore'}}
    assert runner.sql_arm(case, arm, 'test memory', tmp_path, object(), tmp_path)['evaluation']['exec'] == 1
