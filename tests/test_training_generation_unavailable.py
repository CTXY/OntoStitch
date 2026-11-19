from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from cortex.capabilities.sql_training.pipeline.execution import evaluate_record
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig, RuntimeValidationConfig
from cortex.capabilities.sql_training.training.replay import ReplayAttempt, run_stable_replay
from cortex.capabilities.sql_training.validation.in_process import (
    InProcessOntologySqlGenerator, OntologyGenerationUnavailable,
)
from cortex.capabilities.sql_training.validation.models import GenerationRequest
from cortex.schema import GraphSpace


def request():
    return GenerationRequest(trace_id='test', question='Count rows', dialect='sqlite',
                             graph_space=GraphSpace(), enable_query_execution=False)


def test_network_failure_is_unavailable_and_does_not_become_a_sql_candidate_failure():
    calls = []
    def invoke(*args):
        calls.append(args)
        raise ConnectionError('synthetic connection outage')
    generator = InProcessOntologySqlGenerator(llm=SimpleNamespace(invoke=invoke), database=None,
                                              config=RuntimeValidationConfig(dialect='sqlite'))
    with pytest.raises(OntologyGenerationUnavailable) as raised:
        generator.generate(request())
    assert len(calls) == 1
    config = SqlTrainingRuntimeConfig()
    failed = evaluate_record(config, {'question': 'Count rows', 'sql': 'SELECT 1'},
        generate_sql=lambda *a, **kw: (None, [str(raised.value)]), trace_id='failed',
        sql_timeout=1, retrieval_graph=GraphSpace())
    assert failed['evaluation_available'] is False
    assert failed['evaluation_status'] == 'evaluation_unavailable'
    def attempt(number):
        evaluation = failed if number == 3 else {'correct': True, 'evaluation_available': True}
        return ReplayAttempt(attempt=number, trace_id=str(number), evaluation=evaluation)
    replay = run_stable_replay(attempt, required_correct_attempts=3)
    assert replay.status.value == 'unavailable' and not replay.correct
    assert len(replay.attempts) == 3


def test_genuine_empty_model_output_remains_a_semantic_generation_failure():
    generator = InProcessOntologySqlGenerator(llm=SimpleNamespace(invoke=lambda *a: AIMessage(content='')),
        database=None, config=RuntimeValidationConfig(dialect='sqlite'))
    result = generator.generate(request())
    assert result.code == 400 and not result.data[0].query
    evaluation = evaluate_record(SqlTrainingRuntimeConfig(), {'question': 'Count', 'sql': 'SELECT 1'},
        generate_sql=lambda *a, **kw: (result, [result.data[0].error_message]), trace_id='empty',
        sql_timeout=1, retrieval_graph=GraphSpace())
    assert evaluation['evaluation_available'] is True
    assert evaluation['evaluation_status'] == 'candidate_generation_failed'


@pytest.mark.parametrize('error_name', ['RemoteProtocolError', 'ReadError', 'WriteTimeout', 'PoolTimeout'])
def test_transport_family_errors_do_not_become_sql_reflections(error_name):
    transport = type('TransportError', (RuntimeError,), {})
    error = type(error_name, (transport,), {})('sensitive upstream detail')
    def invoke(*args):
        raise error
    generator = InProcessOntologySqlGenerator(llm=SimpleNamespace(invoke=invoke), database=None,
                                              config=RuntimeValidationConfig(dialect='sqlite'))
    with pytest.raises(OntologyGenerationUnavailable) as raised:
        generator.generate(request())
    assert raised.value.retryable is True
    assert raised.value.__cause__ is error
    assert 'sensitive upstream detail' not in str(raised.value)


@pytest.mark.parametrize('status,retryable', [(401, False), (403, False), (429, True), (502, True)])
def test_typed_infrastructure_metadata_survives_durable_evaluation(status, retryable):
    import json
    from cortex.capabilities.sql_training.pipeline.execution import GenerationUnavailableReason
    error = RuntimeError('upstream failure')
    error.status_code = status
    unavailable = OntologyGenerationUnavailable(error)
    result = evaluate_record(SqlTrainingRuntimeConfig(), {'question':'Count', 'sql':'SELECT 1'},
        generate_sql=lambda *a, **kw: (None, [GenerationUnavailableReason(unavailable)]),
        trace_id='typed',sql_timeout=1,retrieval_graph=GraphSpace())
    saved = json.loads(json.dumps(result))
    assert saved['evaluation_status'] == 'evaluation_unavailable'
    assert saved['candidate_failure']['kind'] == 'infrastructure'
    assert saved['candidate_failure']['retryable'] is retryable
    assert saved['infrastructure_errors'] == [{'error_type':'RuntimeError','http_status':status,'retryable':retryable}]


def test_explicitly_unfinished_response_is_not_a_complete_sql_candidate():
    response = AIMessage(content="SELECT 1", response_metadata={"finish_reason": None})
    generator = InProcessOntologySqlGenerator(llm=SimpleNamespace(invoke=lambda *a: response),
        database=None,config=RuntimeValidationConfig(dialect='sqlite'))
    with pytest.raises(OntologyGenerationUnavailable) as raised:
        generator.generate(request())
    assert raised.value.error_type == 'UpstreamResponseIncomplete'


def test_unknown_invocation_failure_is_unavailable_and_not_blindly_retried():
    calls = []
    def invoke(*args):
        calls.append(args)
        raise RuntimeError('unexpected provider adapter failure')
    generator = InProcessOntologySqlGenerator(llm=SimpleNamespace(invoke=invoke),database=None,
        config=RuntimeValidationConfig(dialect='sqlite'))
    with pytest.raises(OntologyGenerationUnavailable) as raised:
        generator.generate(request())
    assert len(calls) == 1
    assert raised.value.retryable is False
    assert raised.value.error_type == 'RuntimeError'
