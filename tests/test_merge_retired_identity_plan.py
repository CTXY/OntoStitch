from types import SimpleNamespace

import pytest
from test_graph_integration import catalog, concept, edge, state

from cortex.capabilities.graph_merge.integration.batch import BatchResult
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.integration.incremental import (
    IncrementalGraphIntegrationEngine,
    identity_task_projection,
)
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.models import Change, Element, IdentityDecision
from cortex.capabilities.graph_merge.llm.incremental import NodeReview
from cortex.schema import load_root_schema


def rewritten_context():
    ctx = IntegrationContext(state(), state([*catalog(), concept("A", "a"), concept("B", "a")]), load_root_schema())
    original = ctx.union_state()
    a, b = (ctx.input_node_map[k] for k in ("A", "B"))
    replacement = next(n for n in original.graph.nodes if n.id == a).model_copy(update={"id": "rewritten"})
    current = original.model_copy(update={"graph": original.graph.model_copy(update={
        "nodes": tuple(n for n in original.graph.nodes if n.id != a) + (replacement,),
    })})
    lineage = LineageIndex(ctx)
    lineage.observe(original, current, [Change(
        before=(Element(kind="node", id=a),), after=(Element(kind="node", id="rewritten"),),
        explanation="An earlier model-authored rewrite preserved this assertion.",
    )])
    return ctx, original, current, lineage, a, b


def test_rewritten_targets_are_review_scope_not_automatic_identity_aliases():
    _, _, current, lineage, a, b = rewritten_context()
    before = lineage.snapshot()
    aliases, targets, feedback = identity_task_projection(current, {a: a, b: a}, lineage)
    assert aliases == {}
    assert targets == {"rewritten", b}
    assert feedback
    assert lineage.snapshot() == before
    assert {n.id for n in current.graph.nodes} >= {"rewritten", b}


def test_missing_source_without_accepted_live_provenance_still_fails_closed():
    _, original, _, _, a, b = rewritten_context()
    current = original.model_copy(update={"graph": original.graph.model_copy(update={
        "nodes": tuple(n for n in original.graph.nodes if n.id != a),
    })})
    with pytest.raises(IntegrationIncomplete, match="retired"):
        identity_task_projection(current, {a: a, b: a}, SimpleNamespace(origin_refs=lambda *_: ()))


def test_ordinary_confirmed_group_keeps_its_existing_projection():
    _, original, _, lineage, a, b = rewritten_context()
    aliases, targets, feedback = identity_task_projection(original, {a: a, b: a}, lineage)
    assert aliases == {a: a, b: a} and targets == {a} and not feedback


def test_later_group_after_accepted_rewrite_requires_explicit_model_merge(monkeypatch):
    import cortex.capabilities.graph_merge.integration.incremental as module
    monkeypatch.setattr(module, "matched_batches", lambda groups, nodes, **kwargs: [[g] for g in groups])
    original = state([*catalog(), concept("A", "a"), concept("B", "a"), concept("C", "b"), concept("D", "b")],
                     [edge("ca", "C", "A", "has_feature")])

    class Agent:
        writes = 0
        reviews = 0
        reviewed_rewritten_group = False

        def invoke(self, phase, ctx, payload):
            a, b, c, d = (ctx.input_node_map[k] for k in ("A", "B", "C", "D"))
            if phase == "identity":
                result = IdentityDecision.model_validate({"groups": [
                    {"members": [c, d], "meaning": "One C identity", "reason": "Same referent and binding."},
                    {"members": [a, b], "meaning": "One A identity", "reason": "Same referent and binding."},
                ]})
            elif phase == "node_assess":
                self.reviews += 1
                return NodeReview(verdict="accept")
            elif phase == "consolidate":
                self.writes += 1
                nodes = {n.id: n for n in ctx.candidate.graph.nodes}
                if self.writes == 1:
                    replacement = nodes[a].model_dump(mode="json")
                    replacement["id"] = "rewritten"
                    result = BatchResult.model_validate({
                        "edits": [{"operation": "delete_node", "node_id": a}, {"operation": "create_node", "node": replacement},
                                  {"operation": "update_edge", "edge": {"id": ctx.input_edge_map["ca"], "to_node_id": "rewritten"}}],
                        "changes": [{"before": [{"kind": "node", "id": a}], "after": [{"kind": "node", "id": "rewritten"}],
                                     "explanation": "Preserve the assertion under the model's chosen current handle."}],
                    })
                else:
                    assert ctx.active_batch.aliases == {}
                    assert {"rewritten", b} <= ctx.active_task.targets
                    assert ctx.active_task.feedback
                    self.reviewed_rewritten_group = True
                    replacement = nodes["rewritten"].model_dump(mode="json")
                    replacement["sources"] = [s.model_dump(mode="json") for s in dict.fromkeys((*nodes["rewritten"].sources, *nodes[b].sources))]
                    replacement["aliases"] = [nodes[b].name]
                    result = BatchResult.model_validate({
                        "edits": [{"operation": "delete_node", "node_id": b}, {"operation": "update_node", "node": replacement}],
                        "changes": [{"before": [{"kind": "node", "id": b}], "after": [{"kind": "node", "id": "rewritten"}],
                                     "explanation": "The model explicitly confirmed the current same referent and retained both sources."}],
                    })
            else:
                assert phase == "reconcile"
                result = BatchResult()
            callback = getattr(ctx, "output_validator", None)
            if callback:
                callback(result)
            return result

    agent = Agent()
    result = IncrementalGraphIntegrationEngine(agent).merge(state(), original, load_root_schema(), run_id="retired-plan")
    assert agent.reviewed_rewritten_group and agent.reviews == 2
    rewritten = next(n for n in result.state.graph.nodes if n.id == "rewritten")
    assert {s.source_id for s in rewritten.sources} == {"A", "B"}
    assert len([n for n in result.state.graph.nodes if n.node_type == "concept"]) == 2
