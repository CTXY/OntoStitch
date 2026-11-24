"""Checkpointed reconciliation entry point requiring only G and its evidence."""
from pathlib import Path
import json,os
from cortex.schema import GraphState,GraphSpace
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.runtime.state import TaskState,build_contract_versions,canonical_hash,validate_checkpoint_contract
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore


def reconcile_current_graph(agent,graph,schema,evidence,task,workspace):
    """Resume one local task, including a pending Assessment, without case inputs.

    Accepted work returns without calling either model. Policy changes require an
    explicit migration; original/retired source graphs are never reconstructed.
    """
    workspace=Path(workspace);run_id='single-graph:'+task.id
    saved=WorkspaceStore(workspace).load(run_id) if workspace.exists() else None
    binding=canonical_hash({'id':task.id,'phase':task.phase,'targets':sorted(task.targets),'checks':task.checks})
    binding_path=workspace.with_suffix('.task_binding.json')
    if binding_path.exists():
        if json.loads(binding_path.read_text())['binding']!=binding:
            raise ValueError('Task scope or required checks changed; use a new task checkpoint')
    else:
        if saved:raise ValueError('Existing workspace needs an explicit single-graph task migration')
        binding_path.parent.mkdir(parents=True,exist_ok=True)
        temporary=binding_path.with_name(binding_path.name+'.'+str(os.getpid())+'.tmp')
        temporary.write_text(json.dumps({'binding':binding,'task_id':task.id}))
        temporary.replace(binding_path)
    versions=build_contract_versions(schema)
    baseline=graph
    if saved:
        validate_checkpoint_contract(saved,versions)
        restored=TaskState.from_dict(saved['task_state'])
        accepted=GraphState.model_validate(saved['current'])
        if canonical_hash(graph) not in {canonical_hash(accepted),restored.base_hash}:
            raise ValueError('Current graph does not match this task checkpoint')
        baseline=GraphState.model_validate(restored.base)
    ctx=IntegrationContext(baseline,GraphState(graph=GraphSpace()),schema,evidence=tuple(evidence))
    from cortex.capabilities.graph_merge.engine import input_fingerprint
    if saved and saved['input_fingerprint']!=input_fingerprint(ctx):
        raise ValueError('Current graph or evidence differs from the checkpoint')
    if saved and restored.stage=='committed':return accepted
    engine=IncrementalGraphIntegrationEngine(agent,max_rounds=3)
    engine.ctx,engine.current,engine.lineage=ctx,graph,LineageIndex(ctx)
    engine.history,engine.reviewed_sources,engine._revision=[],{},0
    engine.identity_decision,engine.open_tasks={},[]
    engine.workspace_path,engine._storage_revision=workspace,None
    engine.pending_relation_work=engine.pending_review=None
    engine.contract_versions=versions
    if saved:
        engine.current=accepted
        engine.lineage=LineageIndex.restore(ctx,saved['lineage'])
        engine.history=saved['history'];engine._revision=saved['accepted_revision']
        engine._storage_revision=saved['revision'];engine.task_state=restored
    engine._run_task(task,run_id)
    return engine.current
