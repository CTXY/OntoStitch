"""Relation descriptions enter the production engine without an Assessment."""
from graph_merge_runtime_fixtures import engine_for
from test_relation_merger import fixture, Hook
from cortex.capabilities.graph_merge.runtime.relation_stage import run_relation_merger
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore


def test_engine_stage_commits_graph_mapping_and_checkpoint_together(tmp_path):
    graph, _ = fixture()
    hook = Hook()
    hook.relation_merger_contract = True
    hook.relation_merger_batch_size = 10
    engine, _ = engine_for(graph, hook, tmp_path / 'merger.sqlite')
    engine.session.open_tasks, engine.session.identity_decision = [], {}
    run_relation_merger(engine, 'relations')
    assert len(hook.calls) == 1
    assert len(engine.session.current.graph.edges) == len(graph.graph.edges) - 1
    assert engine.session.history[-1]['phase'] == 'relation_merge'
    assert engine.session.history[-1]['assessment'] == 'not_required'
    saved = WorkspaceStore(engine.workspace_path).load('relations')
    assert saved['current'] == engine.session.current.model_dump(mode='json')
    assert saved['relation_merger_checkpoint']['stage'] == 'completed'
    run_relation_merger(engine, 'relations')
    assert len(hook.calls) == 1


def test_migrated_group_meaning_requires_no_extra_reconciliation_agent(tmp_path):
    graph,_=fixture(metadata=True);hook=Hook()
    hook.relation_merger_contract=True;hook.relation_merger_batch_size=10
    engine,_=engine_for(graph,hook,tmp_path/'migrated.sqlite')
    engine.session.open_tasks,engine.session.identity_decision=[],{}
    run_relation_merger(engine,'migrated')
    assert len(hook.calls)==1
    assert len(engine.session.current.graph.edges)==len(graph.graph.edges)-1
    assert 'Both original roles remain meaningful.' in next(n for n in engine.session.current.graph.nodes if n.id=='A').properties['definition']
    assert all(item['phase']!='reconcile' for item in engine.session.history)
