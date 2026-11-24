"""Independent transaction review: semantic scope and every rejection boundary."""
from graph_merge_runtime_fixtures import engine_for as engine_for
from graph_merge_chat import ScriptedModel as ScriptedModel
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage

from graph_merge_fixtures import catalog, concept, edge, state
from test_merge_identity_integration_v2 import content, points, prepare, parse_legacy

from cortex.capabilities.graph_merge.graph.batch import ActiveBatch
from cortex.capabilities.graph_merge.runtime.errors import IntegrationIncomplete
from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch, compile_patch
from cortex.capabilities.graph_merge.consolidation.review import Review
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.schema import GraphNode, SourceReference


def test_real_task_review_includes_unchanged_consumer_of_new_conflict(tmp_path):
    a, b = concept("A", "a"), concept("B", "a")
    consumer = GraphNode(id="metric", node_type="metric", name="Amount", properties={
        "expression": {"expr": "SUM(${value})", "refs": {"value": "A"}}}, sources=(SourceReference(source_id="metric"),))
    base = state([*catalog(), a, b, consumer], [edge("depends", "metric", "A", "depends_on")])
    checked = set()
    patch = None

    class Agent:
        def invoke(self, phase, context, payload):
            if phase == "consolidate":
                return patch
            assert phase == "node_assess"
            checked.update(payload["reviewed_node_ids"])
            return Review(verdict="accept")

    engine, evidence = engine_for(base, Agent(), tmp_path / "scope.sqlite")
    patch = parse_legacy({"integrate_groups": [{"owner_id": "A", "member_ids": ["A", "B"],
        "alternatives": [{"alternative_id": key, "content": content(n, definition=key),
                           "evidence_ids": [evidence[n.sources[0]]]} for key, n in [("left", a), ("right", b)]],
        "conflict_points": points(["left", "right"]),
        "branch_dispositions": [{"source_node_id": n.id, "source_alternative_id": None,
            "target_alternative_ids": [key], "explanation": "Preserve this input meaning."}
            for key, n in [("left", a), ("right", b)]], "explanation": "Retain complete competing meanings."}],
        "explanation": "Integrate the confirmed group."}, base)
    task = LocalTask("content", "consolidate", {"A", "B"}, groups=[{"members": ["A", "B"],
        "meaning": "One accepted attribute", "reason": "Confirmed business identity."}])
    engine._run_task(task, "scope", aliases={"A": "A", "B": "A"})
    assert next(n for n in engine.session.current.graph.nodes if n.id == "metric") == consumer
    assert "metric" in checked, "Unchanged dependent content must be assessed against its changed operand"


def test_direct_compile_rejection_restores_candidate_batch_and_identity_members():
    a, b = concept("A", "a"), concept("B", "a")
    base = state([*catalog(), a, b])
    context, _ = prepare(base)
    context.candidate = base
    context.active_batch = previous = ActiveBatch(context, base, targets={"A", "B"})
    patch = parse_legacy({"integrate_groups": [{"owner_id": "A", "member_ids": ["A", "B"],
        "content": content(a), "explanation": "Confirmed same identity."}],
        "node_updates": [{"id": "a", "name": "Illegal physical rewrite", "explanation": "Invalid edit."}],
        "explanation": "The invalid local edit must undo the whole compilation."}, base)
    with pytest.raises(ValueError, match="Physical catalogue"):
        compile_patch(context, base, patch, targets={"A", "B"}, aliases={"A": "A", "B": "A"})
    assert context.candidate is base
    assert context.active_batch is previous
    assert {"A", "B"} <= {n.id for n in context.candidate.graph.nodes}


def test_invalid_atomic_edit_feedback_survives_restart_without_poisoning_draft(tmp_path):
    base = state([*catalog(), concept("A", "a")])
    invalid={"put_changes":[{"change_id":"bad","kind":"node_update","node_id":"A",
        "set":{"unsupported":"Invalid field"},"reason":"Reject unknown fields atomically."}]}
    chat = ScriptedModel([AIMessage(content="", tool_calls=[{"name": "edit_draft", "args": invalid, "id": "bad"}]),
                          RuntimeError("Provider interrupted after validation error")])
    engine, _ = engine_for(base, IncrementalIntegrationAgent(chat), tmp_path / "draft.sqlite")
    task = LocalTask("local", "reconcile", {"A"}, checks=[{"id": "required", "kind": "node_relation_followup",
        "node_ids": ["A"], "mode": "required", "observed": "Verify the existing representation is preserved"}])
    with pytest.raises(RuntimeError, match="Provider interrupted"):
        engine._run_task(task, "draft")
    saved = WorkspaceStore(engine.workspace_path).load("draft")
    assert saved["task_state"]["draft"]["version"] == 0
    recovery = next(row for key, row in saved["task_state"]["agent_recovery"].items() if key.startswith("simple:"))
    assert "unsupported" in str(recovery["runtime"]["feedback"]["latest_receipt"])
    assert recovery["runtime"]["draft"]["version"] == 0
    assert engine.session.current == base and not engine.session.history

    review={"explanation":"The unchanged local graph preserves the required representation and all original knowledge.","feedback":[]}
    import json
    engine.agent=IncrementalIntegrationAgent(ScriptedModel([
        AIMessage(content="",tool_calls=[{"name":"edit_draft","args":{},"id":"submit"}]),
        AIMessage(content=json.dumps(review))]))
    engine._run_task(task, "draft")
    assert engine.session.current == base and len(engine.session.history) == 1
    saved = WorkspaceStore(engine.workspace_path).load("draft")
    assert not saved["task_state"]["draft"].get("changes")
    assert saved["task_state"]["stage"] == "committed"


def test_failed_commit_persistence_does_not_advance_accepted_memory_and_can_resume(tmp_path):
    base = state([*catalog(), concept("A", "a")])
    phases = []

    class Agent:
        def invoke(self, phase, context, payload):
            phases.append(phase)
            return (MergePatch.model_validate({"node_updates": [{"id": "A", "description": "Reviewed new description.",
                "explanation": "Clarify the accepted local meaning."}], "explanation": "Clarify this node."})
                if phase == "reconcile" else Review(verdict="accept"))

    engine, _ = engine_for(base, Agent(), tmp_path / "commit.sqlite")
    lineage, reviewed = deepcopy(engine.session.lineage.snapshot()), deepcopy(engine.session.reviewed_sources)
    persist = engine._persist

    def fail_commit(run_id, *args, **kwargs):
        if engine.session.task_state.stage == "committed":
            raise OSError("Commit persistence failed")
        return persist(run_id, *args, **kwargs)

    engine._persist = fail_commit
    task = LocalTask("local", "reconcile", {"A"})
    with pytest.raises(OSError, match="Commit persistence"):
        engine._run_task(task, "commit")
    assert engine.session.current == base
    assert engine.session.lineage.snapshot() == lineage
    assert engine.session.revision == 0 and not engine.session.history
    assert engine.session.reviewed_sources == reviewed
    assert engine.session.task_state.stage == "ready_to_commit"
    saved = WorkspaceStore(engine.workspace_path).load("commit")
    assert saved["current"] == base.model_dump(mode="json")
    assert saved["task_state"]["stage"] == "ready_to_commit"

    engine._persist = persist
    previous_calls = list(phases)
    engine._run_task(task, "commit")
    assert phases == previous_calls
    assert engine.session.current != base and engine.session.revision == 1
    assert len(engine.session.history) == 1


def test_same_source_coverage_cannot_bypass_independent_knowledge_loss_review(tmp_path):
    shared = SourceReference(source_id="same-case")
    a = concept("A", "a").model_copy(update={"sources": (shared,), "properties": {
        "targets": [["a"]], "definition": "Accepted alpha knowledge."}})
    b = concept("B", "a").model_copy(update={"sources": (shared,), "properties": {
        "targets": [["a"]], "definition": "Accepted complementary beta knowledge."}})
    base = state([*catalog(), a, b])
    checked = []

    class Agent:
        def invoke(self, phase, context, payload):
            if phase == "consolidate":
                return parse_legacy({"integrate_groups": [{"owner_id": "A", "member_ids": ["A", "B"],
                    "content": content(a), "explanation": "This intentionally incomplete integration has the same source set."}],
                    "explanation": "Exercise knowledge preservation independently of provenance coverage."}, base)
            assert phase == "node_assess"
            checked.append(payload)
            original = next(n for n in payload["nodes"] if n["id"] == "B")
            assert original["properties"]["definition"] == b.properties["definition"]
            return Review.model_validate({"verdict": "revise", "issues": [{
                "refs": [{"kind": "node", "id": "A", "fields": ["properties.definition"]}],
                "basis": [{"kind": "node", "id": "B", "view": "input", "fields": ["properties.definition"]}],
                "problem": "The complementary accepted beta meaning was omitted despite identical source references.",
                "required_outcome": "Preserve the complete complementary beta knowledge."}]})

    engine, _ = engine_for(base, Agent(), tmp_path / "same-source.sqlite")
    engine.max_rounds = 0
    task = LocalTask("content", "consolidate", {"A", "B"}, groups=[{"members": ["A", "B"],
        "meaning": "One referent", "reason": "Confirmed identity, distinct complementary knowledge."}])
    with pytest.raises(IntegrationIncomplete, match="assessment"):
        engine._run_task(task, "same-source", aliases={"A": "A", "B": "A"})
    assert checked and engine.session.current == base and not engine.session.history
    saved = WorkspaceStore(engine.workspace_path).load("same-source")
    assert saved["current"] == base.model_dump(mode="json")
    assert saved["task_state"]["stage"] == "revising"
    assert saved["task_state"]["issues"]


def test_missing_assessment_context_retries_review_without_rewriting(tmp_path):
    base = state([*catalog(), concept('A', 'a')])
    calls = []

    class Agent:
        def invoke(self, phase, context, payload):
            calls.append(phase)
            if phase == 'reconcile':
                return MergePatch.model_validate({'node_updates': [{'id': 'A', 'description': 'Updated local description',
                    'explanation': 'Exercise the independent assessment.'}], 'explanation': 'One local edit.'})
            if calls.count('graph_assess') == 1:
                return Review.model_validate({'verdict': 'needs_context', 'issues': [{
                    'refs': [{'kind': 'node', 'id': 'A', 'fields': ['properties.targets']}],
                    'problem': 'Need the exact matching groups for this decision.',
                    'required_outcome': 'Supply the current targets without changing the graph.'}]})
            assert payload['required_context'][0]['result']['items'][0]['properties']['targets']
            return Review(verdict='accept')

    engine, _ = engine_for(base, Agent(), tmp_path / 'context.sqlite')
    engine.max_rounds = 0
    engine._run_task(LocalTask('context', 'reconcile', {'A'}), 'context')
    assert calls == ['reconcile', 'graph_assess', 'graph_assess']
    assert len(engine.session.history) == 1


def test_unavailable_assessment_context_blocks_without_changing_accepted_graph(tmp_path):
    base = state([*catalog(), concept('A', 'a')])
    calls = []

    class Agent:
        def invoke(self, phase, context, payload):
            calls.append(phase)
            if phase == 'reconcile':
                return MergePatch.model_validate({'node_updates': [{'id': 'A', 'description': 'Candidate only',
                    'explanation': 'Not accepted yet.'}], 'explanation': 'One candidate.'})
            return Review.model_validate({'verdict': 'needs_context', 'issues': [{
                'refs': [{'kind': 'node', 'id': 'unknown'}], 'problem': 'A missing prerequisite.',
                'required_outcome': 'Read the missing object.'}]})

    engine, _ = engine_for(base, Agent(), tmp_path / 'unavailable.sqlite')
    with pytest.raises(IntegrationIncomplete, match='unavailable'):
        engine._run_task(LocalTask('context', 'reconcile', {'A'}), 'unavailable')
    assert engine.session.current == base and not engine.session.history
    assert calls == ['reconcile', 'graph_assess']
    saved = WorkspaceStore(engine.workspace_path).load('unavailable')
    assert saved['task_state']['stage'] == 'blocked'
    with pytest.raises(IntegrationIncomplete, match='Writer was not restarted'):
        engine._run_task(LocalTask('context', 'reconcile', {'A'}), 'unavailable')
    assert calls == ['reconcile', 'graph_assess']
