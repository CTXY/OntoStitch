import pytest
from graph_merge_fixtures import catalog, concept, state
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.state import TaskState
from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.consolidation.review import Review as NodeReview
from cortex.schema import GraphState, load_root_schema


def test_writer_revision_retains_draft_and_finding_across_transport_loss(tmp_path):
    original = state([*catalog(), concept('A', 'a')])
    path = tmp_path / 'workspace.sqlite'

    class First:
        calls = 0
        def invoke(self, phase, ctx, payload):
            key = ctx.input_node_map['A']
            if phase == 'node_assess':
                return NodeReview.model_validate({'verdict': 'revise', 'issues': [{
                    'refs': [{'kind': 'node', 'id': key, 'fields': ['name']}],
                    'problem': 'Use the supported name.',
                    'basis': [{'kind': 'node', 'id': key, 'view': 'before', 'fields': ['name']}],
                    'required_outcome': 'Keep a name supported by the original referent.'}]})
            self.calls += 1
            if self.calls > 1:
                assert payload['issues'][0]['problem'] == 'Use the supported name.'
                raise RuntimeError('transport lost while correcting')
            result = MergePatch.model_validate({'node_updates': [{
                'id': key, 'name': 'Unaccepted pending name', 'explanation': 'Propose a representative name.'}],
                'explanation': 'Rename the current concept.'})
            ctx.output_validator(result)
            return result

    def engine(agent, saved=None):
        ctx = IntegrationContext(state(), original, load_root_schema())
        value = IncrementalGraphIntegrationEngine(agent)
        value.session.inputs, value.session.current, value.session.lineage = ctx, ctx.union_state(), LineageIndex(ctx)
        value.session.revision, value.session.history, value.session.reviewed_sources, value.session.identity_decision = 0, [], {}, {}
        value.workspace_path, value.session.storage_revision = path, saved['revision'] if saved else None
        value.session.pending_review = saved['pending_review'] if saved else None
        if saved:
            value.session.task_state = TaskState.from_dict(saved['task_state'])
            value.session.contract_versions = value.session.task_state.versions
            value.session.open_tasks = saved['open_tasks']
        return value, LocalTask('content', 'consolidate', {ctx.input_node_map['A']})

    first, task = engine(First())
    before = first.session.current
    with pytest.raises(RuntimeError, match='transport lost'):
        first._run_task(task, 'revision')
    saved = WorkspaceStore(path).load('revision')
    assert GraphState.model_validate(saved['current']) == before
    assert saved['task_state']['stage'] == 'revising'
    assert saved['task_state']['issues'][0]['problem'] == 'Use the supported name.'
    assert saved['task_state']['draft']['node_updates'][0]['name'] == 'Unaccepted pending name'

    class Corrected:
        phases = []
        def invoke(self, phase, ctx, payload):
            self.phases.append(phase)
            if phase == 'node_assess':
                return NodeReview(verdict='accept')
            assert payload['issues'][0]['problem'] == 'Use the supported name.'
            current = next(n for n in ctx.candidate.graph.nodes if n.node_type == 'concept')
            assert current.name == 'Unaccepted pending name'
            assert next(n for n in ctx.task_before.graph.nodes if n.id == current.id).name != current.name
            result = MergePatch.model_validate({'node_updates': [{
                'id': current.id, 'name': 'Supported corrected name', 'explanation': 'Use the supported referent name.'}],
                'explanation': 'Replace the unaccepted naming draft.'})
            ctx.output_validator(result)
            return result

    model = Corrected()
    resumed, task = engine(model, saved)
    resumed._run_task(task, 'revision')
    assert model.phases == ['consolidate', 'node_assess']
    assert next(n for n in resumed.session.current.graph.nodes if n.node_type == 'concept').name == 'Supported corrected name'
    assert resumed.session.pending_review is None and len(resumed.session.history) == 1
