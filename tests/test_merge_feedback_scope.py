from graph_merge_fixtures import catalog, concept, edge, state

from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.models import LocalTask
from cortex.capabilities.graph_merge.consolidation.review import Review as NodeReview
from cortex.schema import load_root_schema


def test_review_named_node_gets_incident_edge_scope_without_opening_its_neighbours():
    original = state(
        [*catalog(), concept("A", "a"), concept("B", "b"), concept("C", "c"), concept("D", "c")],
        [edge("ab", "A", "B", "has_feature"), edge("bc", "B", "C", "has_feature"),
         edge("cd", "C", "D", "has_feature")],
    )
    ctx = IntegrationContext(state(), original, load_root_schema())
    a, b, c, d = (ctx.input_node_map[k] for k in ("A", "B", "C", "D"))
    bc, cd = (ctx.input_edge_map[k] for k in ("bc", "cd"))

    class Agent:
        reviews = 0
        writes = 0

        def invoke(self, phase, context, payload):
            if phase == "node_assess":
                self.reviews += 1
                if self.reviews == 1:
                    return NodeReview.model_validate({
                        "verdict": "revise", "issues": [{
                            "refs": [{"kind": "node", "id": b}],
                            "problem": "The local change needs a coupled B assertion repair.",
                            "basis": [{"kind": "node", "id": a}, {"kind": "node", "id": b, "view": "before"}],
                            "required_outcome": "Preserve the directly related B assertion meaning."}],
                    })
                return NodeReview(verdict="accept")
            assert phase == "consolidate"
            self.writes += 1
            if self.writes == 1:
                assert bc not in context.active_batch.allowed
                result = MergePatch.model_validate({'node_updates': [{
                    'id': a, 'name': 'Representative A', 'explanation': 'Normalize the representative referent.'}],
                    'explanation': 'A local naming change.'})
            else:
                assert context.active_task.targets == {a}
                assert not {c, d} & context.active_task.targets
                result = MergePatch.model_validate({
                    'node_updates': [{'id': a, 'name': 'Representative A', 'explanation': 'Retain the valid naming change.'}],
                    'edge_updates': [{'id': bc, 'properties': {'description': 'The reviewed direct business attribute.'},
                                      'explanation': 'Repair the reviewer-named coupled assertion.'}],
                    'explanation': 'Replace the complete local draft with the coupled repair.'})
            from cortex.capabilities.graph_merge.graph.scope import ScopeContextRequired
            try:
                context.output_validator(result)
            except ScopeContextRequired as error:
                assert self.writes == 2
                assert error.context['nodes'] or error.context['edges']
                assert context.active_task.targets == {a}
                context.output_validator(result)
            if self.writes == 2:
                assert {a, bc} <= context.active_batch.allowed
                assert not {c, d, cd} & context.active_batch.allowed
            return result

    agent = Agent()
    engine = IncrementalGraphIntegrationEngine(agent)
    engine.session.inputs, engine.session.current, engine.session.lineage = ctx, ctx.union_state(), LineageIndex(ctx)
    engine.session.revision, engine.session.reviewed_sources, engine.session.history = 0, {}, []
    engine._run_task(LocalTask("scope", "consolidate", {a}), "unit")
    assert agent.writes == agent.reviews == 2
    assert engine.session.history[0]["delta"]["edges"]["updated"] == [bc]
    assert next(n for n in engine.session.current.graph.nodes if n.id == a).name == 'Representative A'
