import pytest
from test_graph_integration import catalog, concept, state
from cortex.capabilities.graph_merge.integration.batch import BatchResult
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.incremental import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.incremental import NodeReview
from cortex.schema import GraphState, load_root_schema


def test_writer_revision_retains_valid_buffer_and_counterexample_across_transport_loss(tmp_path):
    original = state([*catalog(), concept('A', 'a')])
    path = tmp_path / 'workspace.sqlite'

    class First:
        calls = 0
        def invoke(self, phase, ctx, payload):
            key = ctx.input_node_map['A']
            if phase == 'node_assess':
                return NodeReview.model_validate({'verdict': 'revise', 'issues': ['Use the supported name.'],
                    'issue_refs': [{'kind': 'node', 'id': key}]})
            self.calls += 1
            if self.calls > 1:
                assert payload['feedback'][0]['issues'] == ['Use the supported name.']
                raise RuntimeError('transport lost while correcting')
            raw = next(n for n in ctx.candidate.graph.nodes if n.id == key).model_dump(mode='json')
            raw['name'] = 'Unaccepted pending name'
            result = BatchResult.model_validate({'edits': [{'operation': 'update_node', 'node': raw}]})
            ctx.output_validator(result)
            return result

    def engine(agent, saved=None):
        ctx = IntegrationContext(state(), original, load_root_schema())
        value = IncrementalGraphIntegrationEngine(agent)
        value.ctx, value.current, value.lineage = ctx, ctx.union_state(), LineageIndex(ctx)
        value._revision, value.history, value.reviewed_sources, value.identity_decision = 0, [], {}, {}
        value.workspace_path, value._storage_revision = path, saved['revision'] if saved else None
        value.pending_review = saved['pending_review'] if saved else None
        return value, LocalTask('content', 'consolidate', {ctx.input_node_map['A']})

    first, task = engine(First())
    before = first.current
    with pytest.raises(RuntimeError, match='transport lost'):
        first._run_task(task, 'revision')
    saved = WorkspaceStore(path).load('revision')
    assert GraphState.model_validate(saved['current']) == before
    assert saved['pending_review']['stage'] == 'revise'
    assert saved['pending_review']['feedback'][0]['issues'] == ['Use the supported name.']

    class Corrected:
        phases = []
        def invoke(self, phase, ctx, payload):
            self.phases.append(phase)
            if phase == 'node_assess':
                return NodeReview(verdict='accept')
            assert payload['feedback'][0]['issues'] == ['Use the supported name.']
            raw = next(n for n in ctx.candidate.graph.nodes if n.node_type == 'concept').model_dump(mode='json')
            assert raw['name'] == 'Unaccepted pending name'
            raw['name'] = 'Supported corrected name'
            result = BatchResult.model_validate({'edits': [{'operation': 'update_node', 'node': raw}]})
            ctx.output_validator(result)
            return result

    model = Corrected()
    resumed, task = engine(model, saved)
    resumed._run_task(task, 'revision')
    assert model.phases == ['consolidate', 'node_assess']
    assert next(n for n in resumed.current.graph.nodes if n.node_type == 'concept').name == 'Supported corrected name'
    assert resumed.pending_review is None and len(resumed.history) == 1
