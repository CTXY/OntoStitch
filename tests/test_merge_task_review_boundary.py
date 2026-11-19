"""Independent transaction review: semantic scope and every rejection boundary."""
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage

from test_graph_integration import catalog, concept, edge, state
from test_merge_identity_integration_v2 import content, points, prepare, parse_legacy

from cortex.capabilities.graph_merge.integration.batch import ActiveBatch
from cortex.capabilities.graph_merge.integration.incremental import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.patch import MergePatch, compile_patch
from cortex.capabilities.graph_merge.integration.review import Review
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.task_agent import IncrementalIntegrationAgent
from cortex.schema import GraphNode, SourceReference


def engine_for(base, agent, path):
    context, evidence = prepare(base)
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=1)
    engine.ctx, engine.current, engine.lineage = context, base, LineageIndex(context)
    engine._revision, engine.reviewed_sources, engine.history = 0, {}, []
    engine.workspace_path, engine._storage_revision = path, None
    engine.identity_decision = {}
    engine.pending_relation_work = engine.pending_review = None
    return engine, evidence


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
    assert next(n for n in engine.current.graph.nodes if n.id == "metric") == consumer
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


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages, config=None):
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_invalid_model_draft_survives_interruption_and_can_be_completely_withdrawn(tmp_path):
    base = state([*catalog(), concept("A", "a")])
    invalid = {"node_updates": [{"id": "A", "unsupported": "Invalid field", "explanation": "Invalid draft"}],
               "explanation": "Save the exact rejected draft."}
    chat = ScriptedModel([AIMessage(content="", tool_calls=[{"name": "submit_patch", "args": invalid, "id": "bad"}]),
                          RuntimeError("Provider interrupted after validation error")])
    engine, _ = engine_for(base, IncrementalIntegrationAgent(chat), tmp_path / "draft.sqlite")
    task = LocalTask("local", "reconcile", {"A"})
    with pytest.raises(RuntimeError, match="Provider interrupted"):
        engine._run_task(task, "draft")
    saved = WorkspaceStore(engine.workspace_path).load("draft")
    assert saved["task_state"]["draft"] == invalid
    assert saved["task_state"]["stage"] == "validating"
    assert saved["task_state"]["issues"]
    assert engine.current == base and not engine.history

    replacement = {"explanation": "Withdraw the invalid proposal; baseline already meets the task."}
    engine.agent = IncrementalIntegrationAgent(ScriptedModel([AIMessage(content="", tool_calls=[
        {"name": "submit_patch", "args": replacement, "id": "withdraw"}])]))
    engine._run_task(task, "draft")
    assert engine.current == base
    assert len(engine.history) == 1
    saved = WorkspaceStore(engine.workspace_path).load("draft")
    assert saved["task_state"]["draft"] == replacement


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
    lineage, reviewed = deepcopy(engine.lineage.snapshot()), deepcopy(engine.reviewed_sources)
    persist = engine._persist

    def fail_commit(run_id, *args, **kwargs):
        if engine.task_state.stage == "committed":
            raise OSError("Commit persistence failed")
        return persist(run_id, *args, **kwargs)

    engine._persist = fail_commit
    task = LocalTask("local", "reconcile", {"A"})
    with pytest.raises(OSError, match="Commit persistence"):
        engine._run_task(task, "commit")
    assert engine.current == base
    assert engine.lineage.snapshot() == lineage
    assert engine._revision == 0 and not engine.history
    assert engine.reviewed_sources == reviewed
    assert engine.task_state.stage == "ready_to_commit"
    saved = WorkspaceStore(engine.workspace_path).load("commit")
    assert saved["current"] == base.model_dump(mode="json")
    assert saved["task_state"]["stage"] == "ready_to_commit"

    engine._persist = persist
    previous_calls = list(phases)
    engine._run_task(task, "commit")
    assert phases == previous_calls
    assert engine.current != base and engine._revision == 1
    assert len(engine.history) == 1


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
    assert checked and engine.current == base and not engine.history
    saved = WorkspaceStore(engine.workspace_path).load("same-source")
    assert saved["current"] == base.model_dump(mode="json")
    assert saved["task_state"]["stage"] == "revising"
    assert saved["task_state"]["issues"]


def test_source_validation_failure_does_not_expand_fixed_task_scope(tmp_path):
    old = edge("original", "A", "B", "has_feature").model_copy(update={
        "sources": (SourceReference(source_id="edge-only"),)})
    base = state([*catalog(), concept("A", "a"), concept("B", "b")], [old])
    observed = []
    engine = None
    task = LocalTask("local", "reconcile", {"A"})

    class Agent:
        def invoke(self, phase, context, payload):
            assert phase == "reconcile"
            q = next(e.id for e in context.evidence if e.source.source_id == "A")
            raw = {"node_updates": [{"id": "B", "name": "Coupled neighbor", "explanation": "Named local successor."}],
                "create_edges": [{"id": key, "edge_type": "has_feature", "from_node_id": "A", "to_node_id": "B",
                    "properties": {"description": key}, "evidence_ids": [q]} for key in ["new_one", "new_two"]],
                "retirements": [{"before": [{"kind": "edge", "id": "original"}],
                    "after": [{"kind": "edge", "id": "new_one"}, {"kind": "edge", "id": "new_two"}],
                    "explanation": "Multiple successors have not received the old edge's evidence."}],
                "explanation": "A structurally valid proposal with invalid source coverage."}
            with pytest.raises(ValueError, match="SourceReferences"):
                context.compile_task_patch(MergePatch.model_validate(raw))
            observed.append(set(task.targets))
            raise RuntimeError("Stop after checking the rejected proposal")

    engine, _ = engine_for(base, Agent(), tmp_path / "scope-rejection.sqlite")
    with pytest.raises(RuntimeError, match="Stop after"):
        engine._run_task(task, "scope-rejection")
    assert observed == [{"A"}]
    assert engine.current == base and not engine.history


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
    assert len(engine.history) == 1


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
    assert engine.current == base and not engine.history
    assert calls == ['reconcile', 'graph_assess']
    saved = WorkspaceStore(engine.workspace_path).load('unavailable')
    assert saved['task_state']['stage'] == 'blocked'
    with pytest.raises(IntegrationIncomplete, match='Writer was not restarted'):
        engine._run_task(LocalTask('context', 'reconcile', {'A'}), 'unavailable')
    assert calls == ['reconcile', 'graph_assess']
