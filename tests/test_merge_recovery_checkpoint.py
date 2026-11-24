"""Draft/error bindings and response recovery remain sound across restarts."""
from copy import deepcopy
from dataclasses import replace

import pytest
from graph_merge_fixtures import catalog, concept, state
from graph_merge_runtime_fixtures import engine_for

from cortex.capabilities.graph_merge.runtime.errors import IntegrationIncomplete
from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch
from cortex.capabilities.graph_merge.consolidation.review import Review
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.state import (
    ContractVersions,
    TaskState,
)
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.schema import GraphNode, GraphSpace, GraphState


def graph(name='base'):
    return GraphState(graph=GraphSpace(nodes=(GraphNode(id='N', node_type='concept', name=name),)))


def drafted():
    versions = ContractVersions(schema='schema', tools='tools', prompts='prompts', review_policy='policy')
    return TaskState.capture(graph(), task_id='task', phase='reconcile', versions=versions).with_draft(
        {'explanation': 'first complete submission'})


def payload(task):
    return {'current': task.base, 'lineage': {}, 'history': [{'task_id': 'already-accepted'}],
            'open_tasks': [], 'contract_versions': task.versions.to_dict(), 'task_state': task.to_dict()}


def test_identical_draft_retains_exact_errors_binding_and_required_context():
    original = drafted().with_issues([{'code': 'schema_invalid', 'field': 'unsupported'}], stage='validating')
    original = replace(original, required_context=({'request': {'id': 'source'}, 'result': {'text': 'Exact premise'}},))
    same = original.with_draft(deepcopy(original.draft))
    assert same is original
    assert same.issues_draft_id == same.draft_id
    assert same.required_context == original.required_context


def test_new_draft_retains_semantic_facts_and_old_candidate_provenance():
    old = drafted().with_candidate(graph('candidate-1'))
    old = replace(old, semantic_issues=({'problem': 'Preserve both branches'},),
                  semantic_issue_basis=({'candidate_hash': old.candidate_hash, 'records': [{'id': 'N'}]},),
                  required_context=({'request': {'id': 'N'}, 'result': {'name': 'Exact old fact'}},))
    old = old.with_issues([{'code': 'compile_rejected'}], stage='validating')
    new = old.with_draft({'explanation': 'new complete submission'})
    assert not new.issues and new.issues_draft_id is None
    assert new.semantic_issues == old.semantic_issues and new.semantic_issue_basis == old.semantic_issue_basis
    assert new.required_context == old.required_context
    assert new.candidate_draft_id == old.draft_id != new.draft_id
    assert new.last_valid_candidate == old.last_valid_candidate
    assert TaskState.from_dict(new.to_dict()) == new
    with pytest.raises(ValueError, match='current candidate'):
        replace(new, stage='assessing').with_review({'verdict': 'accept', 'issues': []}, candidate_hash=new.candidate_hash)


def test_candidate_and_issue_cross_draft_bindings_fail_closed_on_load():
    compiled = drafted().with_candidate(graph('candidate'))
    raw = compiled.to_dict()
    raw['candidate_draft_id'] = 'a-different-draft'
    with pytest.raises(ValueError, match='current task draft'):
        TaskState.from_dict(raw)
    raw = compiled.with_issues([{'error': 'A concrete compile failure'}]).to_dict()
    raw['issues_draft_id'] = 'a-different-draft'
    with pytest.raises(ValueError, match='different draft'):
        TaskState.from_dict(raw)


def test_legacy_candidate_can_be_loaded_but_not_reviewed_before_recompilation():
    task = drafted().with_candidate(graph('candidate'))
    raw = task.to_dict()
    for key in ('candidate_draft_id', 'issues_draft_id', 'agent_recovery'):
        raw.pop(key)
    legacy = TaskState.from_dict(raw)
    assert legacy.candidate_draft_id is None
    with pytest.raises(ValueError, match='current candidate'):
        legacy.with_review({'verdict': 'accept', 'issues': []}, candidate_hash=legacy.candidate_hash)
    verified = legacy.with_candidate(graph('candidate'))
    assert verified.candidate_draft_id == task.draft_id
    assert verified.with_review({'verdict': 'accept', 'issues': []}, candidate_hash=verified.candidate_hash).stage == 'ready_to_commit'


def test_recovery_rounds_and_visible_facts_survive_workspace_restart_detached(tmp_path):
    task = drafted().with_issues([{'code': 'schema_invalid'}], stage='validating')
    recovery = {'protocol_failures': 1, 'last_failure': {'code': 'multiple_result_calls'},
                'reads': [{'view': 'before', 'items': [{'id': 'N', 'name': 'Necessary exact fact'}]}]}
    task = task.with_agent_recovery('writer:task', recovery)
    recovery['reads'].clear()
    store = WorkspaceStore(tmp_path / 'copy.sqlite')
    store.save('run', payload(task))
    restored = TaskState.from_dict(store.load('run')['task_state'])
    assert restored.agent_recovery['writer:task']['reads']
    assert restored.agent_recovery['writer:task']['protocol_failures'] == 1
    assert restored.issues == task.issues and restored.issues_draft_id == task.draft_id
    assert store.load('run')['history'] == [{'task_id': 'already-accepted'}]
    assert restored.with_agent_recovery('writer:task', None).agent_recovery == {}


def test_task_runner_recovery_callbacks_preserve_errors_after_interrupt_and_restart(tmp_path):
    base = state([*catalog(), concept('A', 'a')])
    path = tmp_path / 'recovery.sqlite'
    checked = []

    class Interrupt:
        def invoke(self, phase, context, packet):
            context.record_task_draft({'explanation': 'pending complete draft'})
            context.record_task_error(ValueError('Actual field error'))
            before_id = context.task_draft_id
            context.record_task_draft({'explanation': 'pending complete draft'})
            assert context.task_draft_errors and context.task_draft_errors_draft_id == before_id
            context.record_task_recovery('writer:local', {'protocol_failures': 1, 'reads': [{'fact': 'retained'}]})
            raise RuntimeError('Power lost before another model response')

    engine, _ = engine_for(base, Interrupt(), path)
    task = LocalTask('local', 'reconcile', {'A'})
    with pytest.raises(RuntimeError, match='Power lost'):
        engine._run_task(task, 'resume')
    saved = WorkspaceStore(path).load('resume')
    assert not saved['history'] and saved['current'] == base.model_dump(mode='json')

    class Resume:
        def invoke(self, phase, context, packet):
            checked.append(phase)
            if phase == 'reconcile':
                data = context.load_task_recovery('writer:local')
                assert data['protocol_failures'] == 1 and data['reads']
                data['reads'].clear()
                assert context.load_task_recovery('writer:local')['reads']
                assert context.task_draft_errors and packet['issues_draft_id'] == packet['draft_id']
                return MergePatch(explanation='Withdraw pending edits; accepted baseline is sufficient')
            return Review(verdict='accept')

    restarted, _ = engine_for(base, Resume(), path)
    restarted.session.task_state = TaskState.from_dict(saved['task_state'])
    restarted.session.contract_versions = restarted.session.task_state.versions
    restarted.session.storage_revision = saved['revision']
    restarted._run_task(task, 'resume')
    assert checked == ['reconcile']
    assert restarted.session.current == base and len(restarted.session.history) == 1


def test_ready_legacy_candidate_is_recompiled_before_review_reuse(tmp_path, monkeypatch):
    import cortex.capabilities.graph_merge.runtime.task as task_runner

    base = state([*catalog(), concept('A', 'a')])
    path = tmp_path / 'ready.sqlite'

    class Agent:
        def invoke(self, phase, context, packet):
            return (MergePatch(node_updates=({'id': 'A', 'description': 'New wording', 'explanation': 'Clarify'},),
                               explanation='Clarify meaning') if phase == 'reconcile' else Review(verdict='accept'))

    engine, _ = engine_for(base, Agent(), path)
    real_persist = engine._persist

    def interrupt_commit(run_id):
        if engine.session.task_state.stage == 'committed':
            raise RuntimeError('Interrupted before accepted commit')
        return real_persist(run_id)

    engine._persist = interrupt_commit
    task = LocalTask('local', 'reconcile', {'A'})
    with pytest.raises(RuntimeError, match='Interrupted'):
        engine._run_task(task, 'legacy')
    saved = WorkspaceStore(path).load('legacy')
    raw = deepcopy(saved['task_state'])
    raw.pop('candidate_draft_id')
    engine.session.task_state = TaskState.from_dict(raw)
    assert engine.session.task_state.candidate_draft_id is None
    engine._persist = real_persist
    compiled = []
    original_compile = task_runner.compile_patch

    def compile_once(*args, **kwargs):
        compiled.append(True)
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(task_runner, 'compile_patch', compile_once)
    engine._run_task(task, 'legacy')
    assert compiled == [True]
    assert len(engine.session.history) == 1
    stored = WorkspaceStore(path).load('legacy')['task_state']
    assert stored['candidate_draft_id'] == stored['draft_id']


def test_legacy_ready_review_cannot_commit_a_candidate_that_does_not_recompile(tmp_path):
    from cortex.capabilities.graph_merge.runtime.state import canonical_hash

    base = state([*catalog(), concept('A', 'a')])

    class NoModel:
        def invoke(self, *args):
            raise AssertionError('Legacy ready candidate must recompile before any model call')

    engine, _ = engine_for(base, NoModel(), tmp_path / 'wrong-candidate.sqlite')
    from cortex.capabilities.graph_merge.runtime.contracts import build_contract_versions

    versions = build_contract_versions(engine.session.inputs.schema)
    # A legacy record's hashes can all be internally consistent while its
    # saved graph is not the output of its saved draft. Recompilation is proof.
    wrong = base.model_copy(update={'graph': base.graph.model_copy(update={'nodes': tuple(
        node.model_copy(update={'description': 'Not produced by the no-op draft'}) if node.id == 'A' else node
        for node in base.graph.nodes)})})
    task = TaskState.capture(base, task_id='local', phase='reconcile', versions=versions)
    task = task.with_draft({'explanation': 'No modifications'}).with_candidate(wrong)
    task = task.with_review({'verdict': 'accept', 'issues': []}, candidate_hash=canonical_hash(wrong))
    raw = task.to_dict()
    raw.pop('candidate_draft_id')
    engine.session.task_state = TaskState.from_dict(raw)
    engine.session.contract_versions = versions
    with pytest.raises(ValueError, match='no longer compiles'):
        engine._run_task(LocalTask('local', 'reconcile', {'A'}), 'wrong')
    assert engine.session.current == base and not engine.session.history


def test_assessment_repetition_gate_survives_restart_but_new_presented_facts_reset_it(tmp_path):
    base = state([*catalog(), concept('A', 'a')])
    path = tmp_path / 'assessment.sqlite'
    task = LocalTask('local', 'reconcile', {'A'})
    patch = MergePatch.model_validate({'node_updates': [{'id': 'A', 'description': 'Insufficient repair',
        'explanation': 'Keep the same compiled graph for the repetition check'}], 'explanation': 'One local edit'})
    review = Review.model_validate({'verdict': 'revise', 'issues': [{
        'refs': [{'kind': 'node', 'id': 'A', 'fields': ['description']}],
        'basis': [{'kind': 'node', 'id': 'A', 'view': 'before', 'fields': ['description']}],
        'problem': 'The candidate still needs an adequate clarification',
        'required_outcome': 'Correct the local definition'}]})
    actual_reviews = []

    def attempt(fact):
        class Agent:
            def invoke(self, phase, context, packet):
                if phase == 'reconcile':
                    return patch
                actual_reviews.append(fact)
                context.task_relevant_facts = [{'name': 'read_evidence', 'args': {'ids': ['case']},
                    'result': {'items': [{'id': 'case', 'content': {'actual_fact': fact}}]}}]
                return review
        engine, _ = engine_for(base, Agent(), path)
        engine.max_rounds = 0
        if path.exists():
            saved = WorkspaceStore(path).load('assessment')
            if saved:
                engine.session.task_state = TaskState.from_dict(saved['task_state'])
                if fact != 'same fact':
                    engine.session.task_state = replace(engine.session.task_state, required_context=({
                        'request': {'view': 'input', 'id': 'new-evidence'},
                        'result': {'facts': {'actual_fact': fact}},
                        'requested_for_candidate_hash': engine.session.task_state.candidate_hash},))
                engine.session.contract_versions = engine.session.task_state.versions
                engine.session.storage_revision = saved['revision']
        try:
            engine._run_task(task, 'assessment')
        except IntegrationIncomplete as error:
            return str(error), WorkspaceStore(path).load('assessment')
        raise AssertionError('A revise review must not commit the task')

    # The first repeat also introduces the pinned prior-review basis. Once
    # that actual presentation is stable, a later process sees the same gate.
    attempt('same fact')
    attempt('same fact')
    before_cached_attempt = len(actual_reviews)
    error, stopped = attempt('same fact')
    assert 'supplied facts' in error
    assert len(actual_reviews) == before_cached_attempt
    assert stopped['task_state']['agent_recovery']['assessment_gate']['cache_reused']
    assert stopped['task_state']['agent_recovery']['assessment_gate']['occurrences'] == 2
    error, retried = attempt('new relevant fact')
    assert 'supplied facts' not in error
    assert retried['task_state']['agent_recovery']['assessment_gate']['occurrences'] == 1
    assert len(actual_reviews) == before_cached_attempt + 1
    assert retried['current'] == base.model_dump(mode='json') and not retried['history']


def node_review_issue(verdict='revise'):
    return Review.model_validate({'verdict': verdict, 'issues': [{
        'refs': [{'kind': 'node', 'id': 'A', 'fields': ['description']}],
        'basis': [{'kind': 'node', 'id': 'A', 'view': 'before', 'fields': ['description']}],
        'problem': 'A local clarification is still needed', 'required_outcome': 'Correct this description'}]})


def two_review_packets(*args, **kwargs):
    return iter([{'nodes': [{'id': 'A', 'description': 'Original fact'}],
                  'candidate_nodes': [{'id': 'A', 'description': 'Candidate fact'}],
                  'relations': [], 'candidate_relations': [], 'review_scope': index} for index in range(2)])


def test_node_review_collects_all_actual_slice_facts_without_leaking_previous_reads(monkeypatch):
    from types import SimpleNamespace

    import cortex.capabilities.graph_merge.consolidation.assessment as node_review

    monkeypatch.setattr(node_review, 'review_packets', two_review_packets)
    context = SimpleNamespace(task_relevant_facts=[{'fact': 'stale prior role observation'}],
        task_required_context=({'result': {'fact': 'mandatory'}, 'requested_for_candidate_hash': 'old-candidate'},))
    called = []

    class Agent:
        def invoke(self, phase, ctx, packet):
            assert ctx.task_relevant_facts == ()
            called.append(packet['task_id'])
            if len(called) == 1:
                ctx.task_relevant_facts = [{'name': 'read_evidence', 'result': {'fact': 'Only first slice'}}]
            return Review(verdict='accept')

    candidate = graph()
    node_review.assess_nodes(Agent(), context, candidate, LocalTask('review', 'node_assess', {'N'}),
                            origins=None, before=candidate)
    packets = context.task_assessment_premises
    assert len(packets) == 2 and packets[0]['reads'] and packets[1]['reads'] == ()
    assert packets[0]['payload']['required_context'] == packets[1]['payload']['required_context']
    assert packets[1]['payload']['required_context'][0]['requested_for_candidate_hash'] == 'old-candidate'


def test_node_review_early_identity_revisit_retains_only_obtained_packets(monkeypatch):
    from types import SimpleNamespace

    import cortex.capabilities.graph_merge.consolidation.assessment as node_review

    monkeypatch.setattr(node_review, 'review_packets', two_review_packets)
    context = SimpleNamespace()

    class Agent:
        def invoke(self, phase, ctx, packet):
            ctx.task_relevant_facts = [{'name': 'read_evidence', 'result': {'fact': 'Identity counterexample'}}]
            return node_review_issue('revisit_identity')

    candidate = graph()
    review = node_review.assess_nodes(Agent(), context, candidate,
        LocalTask('review', 'node_assess', {'N'}), origins=None, before=candidate)
    assert review.verdict == 'revisit_identity'
    assert len(context.task_assessment_premises) == 1
    assert context.task_assessment_premises[0]['reads'][0]['result']['fact'] == 'Identity counterexample'


def test_node_assessment_context_retry_supplies_version_bound_facts_to_all_slices(tmp_path, monkeypatch):
    import cortex.capabilities.graph_merge.consolidation.assessment as node_review

    monkeypatch.setattr(node_review, 'review_packets', two_review_packets)
    base = state([*catalog(), concept('A', 'a')])
    calls = []

    class Agent:
        def invoke(self, phase, context, packet):
            calls.append((phase, deepcopy(packet)))
            if phase == 'consolidate':
                return MergePatch.model_validate({'node_updates': [{'id': 'A', 'description': 'Clarified',
                    'explanation': 'Clarify this local meaning'}], 'explanation': 'One local edit'})
            if not packet.get('required_context') and packet['task_id'].endswith('-0'):
                return node_review_issue()
            if not packet.get('required_context'):
                return Review.model_validate({'verdict': 'needs_context', 'issues': [{
                    'refs': [{'kind': 'node', 'id': 'A', 'fields': ['properties.targets']}],
                    'problem': 'Need exact matching groups', 'required_outcome': 'Read current targets'}]})
            return Review(verdict='accept')

    engine, _ = engine_for(base, Agent(), tmp_path / 'context-retry.sqlite')
    engine.max_rounds = 0
    engine._run_task(LocalTask('local', 'consolidate', {'A'}), 'context-retry')
    assert [phase for phase, _ in calls] == ['consolidate', *['node_assess'] * 4]
    final_packets = engine.execution.context.task_assessment_premises
    assert len(final_packets) == 2 and len(engine.session.history) == 1
    for actual in final_packets:
        packet = actual['payload']
        required = packet['required_context']
        assert all(item['requested_for_candidate_hash'] for item in required)
        assert any(item['result']['items'][0].get('properties', {}).get('targets') for item in required)
        assert packet['earlier_findings'][0]['problem'] == 'A local clarification is still needed'
        assert packet['semantic_issue_basis'][0]['candidate_hash']
        assert packet['semantic_issue_basis'][0]['records']


@pytest.mark.parametrize('changed_slice', [0, 1])
def test_node_assessment_gate_uses_facts_from_every_slice_after_restart(tmp_path, monkeypatch, changed_slice):
    import cortex.capabilities.graph_merge.consolidation.assessment as node_review

    base = state([*catalog(), concept('A', 'a')])
    path = tmp_path / 'multi-slice.sqlite'
    task = LocalTask('local', 'consolidate', {'A'})
    patch = MergePatch.model_validate({'node_updates': [{'id': 'A', 'description': 'Still insufficient',
        'explanation': 'Compile the same candidate'}], 'explanation': 'One local edit'})
    actual_reviews = []

    def attempt(fact):
        def supplied_packets(*args, **kwargs):
            packets = list(two_review_packets())
            if fact != 'same':
                # New knowledge must be in the forthcoming model input before
                # cache lookup, not invented only inside a future fake invoke.
                packets[changed_slice]['evidence'] = [{'id': 'new-evidence', 'content': {'actual_fact': fact}}]
            return iter(packets)
        monkeypatch.setattr(node_review, 'review_packets', supplied_packets)
        class Agent:
            def invoke(self, phase, context, packet):
                if phase == 'consolidate':
                    return patch
                actual_reviews.append(packet['task_id'])
                index = int(packet['task_id'].rsplit('-', 1)[-1])
                context.task_relevant_facts = [{'name': 'read_evidence', 'args': {'ids': [str(index)]},
                    'result': {'items': [{'id': str(index), 'content': fact if index == changed_slice else 'stable'}]}}]
                return node_review_issue()
        engine, _ = engine_for(base, Agent(), path)
        engine.max_rounds = 0
        saved = WorkspaceStore(path).load('nodes') if path.exists() else None
        if saved:
            engine.session.task_state = TaskState.from_dict(saved['task_state'])
            engine.session.contract_versions = engine.session.task_state.versions
            engine.session.storage_revision = saved['revision']
        with pytest.raises(IntegrationIncomplete) as error:
            engine._run_task(task, 'nodes')
        return str(error.value), WorkspaceStore(path).load('nodes')

    attempt('same')
    attempt('same')
    before_cached_attempt = len(actual_reviews)
    error, stopped = attempt('same')
    assert 'supplied facts' in error
    assert len(actual_reviews) == before_cached_attempt
    assert stopped['task_state']['agent_recovery']['assessment_gate']['cache_reused']
    assert stopped['task_state']['agent_recovery']['assessment_gate']['occurrences'] == 2
    error, retried = attempt('new actual fact')
    assert 'supplied facts' not in error
    assert retried['task_state']['agent_recovery']['assessment_gate']['occurrences'] == 1
    assert len(actual_reviews) == before_cached_attempt + 2
    assert not retried['history'] and retried['current'] == base.model_dump(mode='json')


def test_saved_candidate_recovers_legacy_scope_without_rerunning_writer(tmp_path, monkeypatch):
    import cortex.capabilities.graph_merge.runtime.task as task_runner
    from cortex.capabilities.graph_merge.graph.scope import (
        ScopeContextRequired,
    )
    from cortex.capabilities.graph_merge.runtime.contracts import build_contract_versions
    from cortex.capabilities.graph_merge.runtime.state import canonical_hash

    base = state([*catalog(), concept('A', 'a'), concept('B', 'b')])
    class NoWriter:
        def invoke(self, *args):
            raise AssertionError('An already compiled draft must not rerun its Writer')
    engine, _ = engine_for(base, NoWriter(), tmp_path / 'legacy-scope.sqlite')
    versions = build_contract_versions(engine.session.inputs.schema)
    pending = TaskState.capture(base, task_id='local', phase='reconcile', versions=versions)
    pending = pending.with_draft({'explanation': 'No edits needed'}).with_candidate(base)
    pending = replace(pending, candidate_draft_id=None)
    engine.session.task_state, engine.session.contract_versions = pending, versions
    compile_original = task_runner.compile_patch
    observed = []
    def compile_with_legacy_visibility(context, *args, **kwargs):
        observed.append(set(context.task_visible_ids))
        if 'B' not in context.task_visible_ids:
            raise ScopeContextRequired({'view': 'candidate', 'nodes': [
                {'id': 'B', 'name': 'B', 'node_type': 'concept'}], 'edges': []})
        return compile_original(context, *args, **kwargs)
    monkeypatch.setattr(task_runner, 'compile_patch', compile_with_legacy_visibility)
    engine._run_task(LocalTask('local', 'reconcile', {'A'}), 'legacy-scope')
    assert len(observed) == 2 and 'B' not in observed[0] and 'B' in observed[1]
    assert canonical_hash(engine.session.current) == pending.candidate_hash
    saved = WorkspaceStore(engine.workspace_path).load('legacy-scope')['task_state']
    assert saved['required_context'][0]['result']['items'][0]['id'] == 'B'
    assert saved['candidate_draft_id'] == pending.draft_id
