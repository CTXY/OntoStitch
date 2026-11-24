"""Agent recovery replays JSON checkpoints without touching production runs."""

import gzip
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from test_graph_integration import catalog, concept, state
from test_incremental_agent import Chat

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.proposal_scope import (
    ScopeContextRequired,
)
from cortex.capabilities.graph_merge.llm.recovery import (
    OutputProtocolRecoveryExhausted,
    TaskNoProgress,
)
from cortex.capabilities.graph_merge.llm.task_agent import (
    IncrementalIntegrationAgent,
    failure_feedback,
)
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import SourceReference, load_root_schema


class Interrupted(RuntimeError):
    """A process boundary after the prior reply has been checkpointed."""


class ScriptedChat(Chat):
    def __init__(self, responses):
        super().__init__(responses)
        self.advertised = []

    def bind_tools(self, tools, **kwargs):
        self.advertised.append([getattr(t, 'name', getattr(t, '__name__', None)) for t in tools])
        return super().bind_tools(tools, **kwargs)

    def invoke(self, messages, config=None):
        self.requests.append(deepcopy(messages))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def action(name, args, ident):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': deepcopy(args), 'id': ident}])


def submit(draft, ident='submit'):
    return action('submit_patch', draft, ident)


class Checkpoint:
    """Mimic TaskState callbacks with JSON copies at every process boundary."""

    def __init__(self, payload=None):
        self.initial = deepcopy(payload or {'task_id': 'local', 'task': {'id': 'local', 'phase': 'reconcile'}})
        self.draft = deepcopy(self.initial.get('draft', {'explanation': 'prior checkpoint draft'}))
        self.issues = deepcopy(self.initial.get('issues', []))
        self.saved_drafts, self.saved_errors, self.recoveries = [], [], {}

    def payload(self):
        return {**deepcopy(self.initial), 'draft': deepcopy(self.draft), 'issues': deepcopy(self.issues)}

    def context(self, compiler=None, *, evidence=()):
        ctx = IntegrationContext(state(), state([*catalog(), concept('N', 'a')]),
                                 load_root_schema(), evidence=evidence)
        ctx.candidate = ctx.union_state()
        ctx.task_before = ctx.candidate
        ctx.active_task = SimpleNamespace(id=self.initial.get('task_id', 'local'))
        ctx.active_draft = deepcopy(self.draft)

        def save_draft(raw):
            self.draft = deepcopy(raw)
            self.saved_drafts.append(deepcopy(raw))

        def save_error(error):
            value = {**failure_feedback(error), 'stage': ctx.task_failure_stage}
            self.issues = [value]
            self.saved_errors.append(deepcopy(value))

        ctx.record_task_draft = save_draft
        ctx.record_task_error = save_error
        ctx.compile_task_patch = compiler or (lambda patch: object())
        ctx.load_task_recovery = lambda key: deepcopy(self.recoveries.get(key))
        ctx.record_task_recovery = lambda key, data: self.recoveries.update(
            {key: json.loads(json.dumps(data))})
        return ctx

    def invocation(self, model, compiler=None, *, evidence=()):
        return IncrementalIntegrationAgent(model, max_calls=None).invoke(
            'reconcile', self.context(compiler, evidence=evidence), self.payload())


@pytest.fixture
def real_toxicology():
    path = Path(__file__).parent / 'fixtures/merge_output_protocol_failure/real_requests.json.gz'
    with gzip.open(path, 'rt') as stream:
        return json.load(stream)['toxicology_multi']


def test_real_schema_failure_survives_restart_and_exhausted_restart_makes_no_model_call(real_toxicology):
    fixture = real_toxicology
    draft = next(c['args'] for c in fixture['calls'] if c['args'])
    checkpoint = Checkpoint(fixture['payload'])
    first = ScriptedChat([submit(draft), Interrupted('process exited after failure')])
    with pytest.raises(Interrupted):
        checkpoint.invocation(first)
    assert len(checkpoint.saved_errors) == 1
    second = ScriptedChat([AIMessage(content=json.dumps(draft))])
    with pytest.raises(TaskNoProgress, match='output_schema'):
        checkpoint.invocation(second)
    assert len(second.requests) == 1
    third = ScriptedChat([])
    with pytest.raises(TaskNoProgress, match='output_schema'):
        checkpoint.invocation(third)
    assert not third.requests


def test_compiler_failure_counter_survives_restart():
    checkpoint = Checkpoint()
    draft = {'explanation': 'the same complete Patch'}

    def reject(patch):
        raise ValueError('The same source unit is missing')

    first = ScriptedChat([submit(draft), Interrupted('stopped')])
    with pytest.raises(Interrupted):
        checkpoint.invocation(first, reject)
    second = ScriptedChat([submit(draft, 'new-call-id')])
    with pytest.raises(TaskNoProgress, match='patch_compile'):
        checkpoint.invocation(second, reject)
    assert len(second.requests) == 1


def test_real_protocol_budget_and_submit_only_mode_survive_restart(real_toxicology):
    checkpoint = Checkpoint(real_toxicology['payload'])
    prior = deepcopy(checkpoint.draft)
    response = AIMessage(content='', tool_calls=deepcopy(real_toxicology['calls']))
    first = ScriptedChat([response, response, Interrupted('process exited')])
    with pytest.raises(Interrupted):
        checkpoint.invocation(first)
    assert first.advertised[-1] == ['submit_patch']
    assert not checkpoint.saved_drafts and not checkpoint.saved_errors
    second = ScriptedChat([response])
    with pytest.raises(OutputProtocolRecoveryExhausted):
        checkpoint.invocation(second)
    assert second.advertised == [['submit_patch']]
    assert checkpoint.draft == prior
    third = ScriptedChat([])
    checkpoint.initial['required_context'] = [{
        'request': {'view': 'candidate', 'kind': 'node', 'id': 'new-source'},
        'result': {'view': 'candidate', 'items': [{'id': 'new-source', 'name': 'New explicit facts'}]},
    }]
    with pytest.raises(OutputProtocolRecoveryExhausted):
        checkpoint.invocation(third)
    assert not third.requests and not third.advertised


@pytest.mark.parametrize('read_kind', ['source_units', 'evidence'])
def test_actual_new_read_allows_one_retry_but_repeating_it_does_not_reset_failure(read_kind):
    evidence = (EvidenceItem(id='case-1', case_id='case-1', source=SourceReference(source_id='N'),
                             content={'question': 'Original case', 'sql': 'SELECT a FROM objects'}),)
    checkpoint = Checkpoint()
    ctx = checkpoint.context(evidence=evidence)
    owner = ctx.input_node_map['N']
    read_name, args = (('read_source_units', {'owner_ids': [owner]}) if read_kind == 'source_units'
                       else ('read_evidence', {'ids': ['case-1']}))
    draft = {'explanation': 'same proposal before and after exact read'}
    attempts = []

    def reject(patch):
        attempts.append(patch)
        raise ValueError('The same unresolved content defect')

    model = ScriptedChat([submit(draft, 'first'), action(read_name, args, 'new-facts'),
                          submit(draft, 'second'), action(read_name, args, 'same-facts'),
                          submit(draft, 'third')])
    with pytest.raises(TaskNoProgress, match='patch_compile'):
        checkpoint.invocation(model, reject, evidence=evidence)
    assert len(model.requests) == 5 and len(attempts) == 3


@pytest.mark.parametrize('read_name,args', [
    ('read_evidence', {'ids': ['does-not-exist']}),
    ('read_source_units', {'owner_ids': ['does-not-exist']}),
    ('inspect_graph', {'mode': 'items', 'ids': ['input::N'], 'fields': ['made_up_property']}),
])
def test_failed_actual_read_does_not_grant_an_extra_same_failure(read_name, args):
    checkpoint = Checkpoint()
    draft = {'explanation': 'same rejected draft'}

    def reject(patch):
        raise ValueError('Same compilation rejection')

    model = ScriptedChat([submit(draft, 'first'), action(read_name, args, 'failed-read'),
                          submit(draft, 'second')])
    with pytest.raises(TaskNoProgress, match='patch_compile'):
        checkpoint.invocation(model, reject)
    assert len(model.requests) == 3


def test_compiler_supplied_context_belongs_to_the_following_response_not_current_one():
    checkpoint = Checkpoint()
    draft = {'explanation': 'same complete draft'}
    supplied = {'view': 'candidate', 'nodes': [{'id': 'coupled-owner', 'name': 'New coupled owner'}]}
    model = ScriptedChat([submit(draft, 'first'), submit(draft, 'new-premises'), submit(draft, 'repeated')])

    def reject(patch):
        raise ScopeContextRequired(supplied)

    with pytest.raises(TaskNoProgress, match='patch_compile'):
        checkpoint.invocation(model, reject)
    assert len(model.requests) == 3
    assert 'New coupled owner' in model.requests[1][1].content
    saved = next(iter(checkpoint.recoveries.values()))['tracker']
    assert sorted(saved['failures'].values()) == [1, 2]


def test_supplied_context_and_failure_bucket_survive_process_restart():
    checkpoint = Checkpoint()
    draft = {'explanation': 'same draft'}
    supplied = {'view': 'candidate', 'nodes': [{'id': 'outside', 'name': 'A newly visible fact'}]}

    def reject(patch):
        raise ScopeContextRequired(supplied)

    first = ScriptedChat([submit(draft), Interrupted('process exit')])
    with pytest.raises(Interrupted):
        checkpoint.invocation(first, reject)
    second = ScriptedChat([submit(draft, 'retry-with-new-facts'), submit(draft, 'retry-same-facts')])
    with pytest.raises(TaskNoProgress):
        checkpoint.invocation(second, reject)
    assert len(second.requests) == 2
    assert 'A newly visible fact' in second.requests[0][1].content


def test_plain_json_array_cannot_replace_prior_draft():
    checkpoint = Checkpoint()
    prior = deepcopy(checkpoint.draft)
    model = ScriptedChat([AIMessage(content='[{"explanation":"not a complete object"}]'),
                          AIMessage(content='{"explanation":"complete repaired object"}')])
    result = checkpoint.invocation(model)
    assert result.explanation == 'complete repaired object'
    assert checkpoint.saved_drafts == [{'explanation': 'complete repaired object'}]
    assert not checkpoint.saved_errors
    assert json.loads(model.requests[1][1].content)['draft'] == prior


@pytest.mark.parametrize('problem', ['missing_id', 'duplicate_id', 'unknown_tool'])
def test_tool_identity_errors_are_rejected_before_any_read_or_draft_side_effect(monkeypatch, problem):
    from cortex.capabilities.graph_merge.llm import task_agent
    reads = []

    @tool
    def inspect_graph(ids: list[str]) -> dict:
        """Read an exact fact, recording whether execution happened."""
        reads.append(ids)
        return {'items': []}

    monkeypatch.setattr(task_agent, 'build_read_tools', lambda *a, **k: {'inspect_graph': inspect_graph})
    calls = [{'name': 'inspect_graph', 'args': {'ids': ['N']}, 'id': 'good'}]
    calls.append({'name': 'nonexistent' if problem == 'unknown_tool' else 'inspect_graph',
                  'args': {'ids': ['N']}, 'id': None if problem == 'missing_id' else 'good' if problem == 'duplicate_id' else 'bad'})
    model = ScriptedChat([AIMessage(content='', tool_calls=calls), submit({'explanation': 'unique result'})])
    checkpoint = Checkpoint()
    prior = deepcopy(checkpoint.draft)
    checkpoint.invocation(model)
    assert not reads and not checkpoint.saved_errors
    assert checkpoint.saved_drafts == [{'explanation': 'unique result'}]
    assert json.loads(model.requests[1][1].content)['draft'] == prior


def test_corrupted_saved_dialogue_is_rejected_before_model_call():
    checkpoint = Checkpoint()
    draft = {'explanation': 'same draft'}

    def reject(patch):
        raise ValueError('Needs repair')

    first = ScriptedChat([submit(draft), Interrupted('exit')])
    with pytest.raises(Interrupted):
        checkpoint.invocation(first, reject)
    next(iter(checkpoint.recoveries.values()))['dialogue']['draft']['explanation'] = 'tampered'
    second = ScriptedChat([])
    with pytest.raises(ValueError, match='dialogue'):
        checkpoint.invocation(second, reject)
    assert not second.requests


def test_restored_candidate_read_restores_compiler_visible_ids():
    checkpoint = Checkpoint()
    first_context = checkpoint.context()
    first_context.task_visible_ids = set()
    owner = first_context.input_node_map['N']
    first = ScriptedChat([action('inspect_graph', {'mode': 'items', 'ids': [owner],
                                                  'view': 'candidate'}, 'read-owner'),
                          Interrupted('process exited after the read was persisted')])
    with pytest.raises(Interrupted):
        IncrementalIntegrationAgent(first, max_calls=None).invoke('reconcile', first_context, checkpoint.payload())
    assert owner in first_context.task_visible_ids

    restored_context = checkpoint.context()
    restored_context.task_visible_ids = set()

    def compile_from_visible_facts(patch):
        assert owner in restored_context.task_visible_ids, 'A restored read must remain visible to the compiler'
        return object()

    restored_context.compile_task_patch = compile_from_visible_facts
    second = ScriptedChat([submit({'explanation': 'Use the exact owner read before restart'})])
    IncrementalIntegrationAgent(second, max_calls=None).invoke('reconcile', restored_context, checkpoint.payload())


@pytest.mark.parametrize('kind', ['facts', 'required_context'])
def test_new_explicit_facts_allow_recovery_from_content_no_progress_after_restart(kind):
    checkpoint = Checkpoint()
    draft = {'explanation': 'A complete unchanged draft'}

    def reject(patch):
        raise ValueError('The same unresolved fact-dependent defect')

    first = ScriptedChat([submit(draft, 'first'), submit(draft, 'same-facts')])
    with pytest.raises(TaskNoProgress):
        checkpoint.invocation(first, reject)
    new_fact = {'id': 'new-premise', 'name': 'An explicitly supplied new source fact'}
    checkpoint.initial[kind] = ([new_fact] if kind == 'facts' else [{
        'request': {'view': 'candidate', 'kind': 'node', 'id': new_fact['id']},
        'result': {'view': 'candidate', 'items': [new_fact]},
    }])
    # New premises do not erase the prior failure bucket, but they must grant
    # a new complete response. A subsequent repeat under these facts stops.
    resumed = ScriptedChat([submit(draft, 'new-facts'), submit(draft, 'repeated-new-facts')])
    with pytest.raises(TaskNoProgress):
        checkpoint.invocation(resumed, reject)
    assert len(resumed.requests) == 2


def test_changed_error_narration_and_transport_metadata_do_not_unblock_content_failure():
    checkpoint = Checkpoint()
    draft = {'explanation': 'The same complete draft'}

    def reject(patch):
        raise ValueError('The same real defect')

    first = ScriptedChat([submit(draft, 'first'), submit(draft, 'repeated')])
    with pytest.raises(TaskNoProgress):
        checkpoint.invocation(first, reject)
    checkpoint.issues = [{'error': 'A different explanation of the same known failure'}]
    checkpoint.initial.update(request_id='new-transport-id', call_id='new-call-id', recorded_at='later')
    resumed = ScriptedChat([])
    with pytest.raises(TaskNoProgress):
        checkpoint.invocation(resumed, reject)
    assert not resumed.requests


def test_explicitly_repeating_a_fact_already_read_does_not_unblock_content_failure():
    checkpoint = Checkpoint()
    ctx = checkpoint.context()
    owner = ctx.input_node_map['N']
    draft = {'explanation': 'The same rejected draft'}

    def reject(patch):
        raise ValueError('Same defect after the selected graph read')

    first = ScriptedChat([action('inspect_graph', {'mode': 'items', 'ids': [owner],
                                                  'fields': ['name'], 'view': 'candidate'}, 'read'),
                          submit(draft, 'first'), submit(draft, 'repeated')])
    with pytest.raises(TaskNoProgress):
        checkpoint.invocation(first, reject)
    checkpoint.initial['facts'] = [{'id': owner, 'name': 'N'}]
    resumed = ScriptedChat([])
    with pytest.raises(TaskNoProgress):
        checkpoint.invocation(resumed, reject)
    assert not resumed.requests


@pytest.mark.parametrize('corruption', ['missing_tracker', 'null_tracker', 'empty_record'])
def test_incomplete_recovery_envelope_cannot_silently_reset_failure_counts(corruption):
    checkpoint = Checkpoint()
    draft = {'explanation': 'A known rejected draft'}

    def reject(patch):
        raise ValueError('Known compilation error')

    first = ScriptedChat([submit(draft), Interrupted('exit')])
    with pytest.raises(Interrupted):
        checkpoint.invocation(first, reject)
    key = next(iter(checkpoint.recoveries))
    if corruption == 'missing_tracker':
        checkpoint.recoveries[key].pop('tracker')
    elif corruption == 'null_tracker':
        checkpoint.recoveries[key]['tracker'] = None
    else:
        checkpoint.recoveries[key] = {}
    resumed = ScriptedChat([])
    with pytest.raises((ValueError, TypeError)):
        checkpoint.invocation(resumed, reject)
    assert not resumed.requests


def test_compiler_supplied_visibility_survives_restart_without_replaying_the_failure():
    checkpoint = Checkpoint()
    draft = {'explanation': 'Use the coupled owner already supplied by the compiler'}
    first_ctx = checkpoint.context()
    owner = first_ctx.input_node_map['N']
    first_ctx.task_visible_ids = set()

    def first_compile(patch):
        if owner not in first_ctx.task_visible_ids:
            raise ScopeContextRequired({'view': 'candidate', 'nodes': [
                {'id': owner, 'name': 'N', 'node_type': 'concept'}], 'edges': []})
        return object()

    first_ctx.compile_task_patch = first_compile
    first = ScriptedChat([submit(draft), Interrupted('exit after supplied context')])
    with pytest.raises(Interrupted):
        IncrementalIntegrationAgent(first, max_calls=None).invoke('reconcile', first_ctx, checkpoint.payload())
    resumed_ctx = checkpoint.context()
    resumed_ctx.task_visible_ids = set()
    calls = []

    def resumed_compile(patch):
        calls.append(patch)
        assert owner in resumed_ctx.task_visible_ids
        return object()

    resumed_ctx.compile_task_patch = resumed_compile
    resumed = ScriptedChat([submit(draft)])
    IncrementalIntegrationAgent(resumed, max_calls=None).invoke('reconcile', resumed_ctx, checkpoint.payload())
    assert len(resumed.requests) == len(calls) == 1
