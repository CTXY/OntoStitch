"""Current graph and deterministic compiler fixtures, independent of test modules."""
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.batch import ActiveBatch
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.runtime.state import TaskState
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.schema import load_root_schema
from graph_merge_fixtures import catalog, concept, edge, state
from cortex.capabilities.graph_merge.evidence import EvidenceItem
from cortex.schema import GraphState, GraphSpace


def prepare(base):
    refs = {s for n in base.graph.nodes for s in n.sources} | {s for e in base.graph.edges for s in e.sources}
    evidence = tuple(EvidenceItem(id=f"Q{index}", source=s, case_id=s.case_id, content={"question": "Accepted source knowledge", "sql": "SELECT a FROM objects"})
                     for index, s in enumerate(sorted(refs, key=lambda s: s.model_dump_json())))
    ctx = IntegrationContext(base, GraphState(graph=GraphSpace()), load_root_schema(), evidence=evidence)
    ctx.task_visible_ids = {n.id for n in base.graph.nodes} | {e.id for e in base.graph.edges}
    ids = {e.source: e.id for e in evidence}
    return ctx, ids


def engine_for(base, agent, path):
    context, evidence = prepare(base)
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=1)
    engine.session.inputs, engine.session.current, engine.session.lineage = context, base, LineageIndex(context)
    engine.session.revision, engine.session.reviewed_sources, engine.session.history = 0, {}, []
    engine.workspace_path, engine.session.storage_revision = path, None
    engine.session.identity_decision = {}
    engine.session.pending_relation_work = engine.session.pending_review = None
    return engine, evidence


def relation_compiler_context():
    original = state([*catalog(), concept("A", "a"), concept("B", "b")],
                     [edge("e", "A", "B", "has_feature")])
    ctx = IntegrationContext(state(), original, load_root_schema())
    base = ctx.union_state()
    a, b, e = ctx.input_node_map["A"], ctx.input_node_map["B"], ctx.input_edge_map["e"]
    ctx.candidate = base
    ctx.active_batch = ActiveBatch(ctx, base, targets={a, b})
    ctx.output_validator = ctx.active_batch.submit
    return ctx, base, a, b, e



def setup_engine(agent, path, saved=None):
    incoming = state([*catalog(), concept("A", "a"), concept("B", "b")],
                     [edge("keep", "A", "B", "has_feature")])
    ctx = IntegrationContext(state(), incoming, load_root_schema())
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=0)
    engine.session.inputs, engine.session.current, engine.session.lineage = ctx, ctx.union_state(), LineageIndex(ctx)
    engine.session.revision, engine.session.reviewed_sources, engine.session.history = 0, {}, []
    engine.workspace_path = path
    engine.session.storage_revision = None
    engine.session.identity_decision = {}
    engine.session.pending_relation_work = None
    if saved:
        engine.session.task_state = TaskState.from_dict(saved['task_state'])
        engine.session.contract_versions = engine.session.task_state.versions
        engine.session.open_tasks = saved['open_tasks']
        engine.session.storage_revision = saved['revision']
    return engine, LocalTask('repair', 'reconcile', {ctx.input_node_map['A'], ctx.input_node_map['B']})
