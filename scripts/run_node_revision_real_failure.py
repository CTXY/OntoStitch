"""Two controlled repair paths on the exact toxicology failed node transaction.

The initial draft is historical. Initial Assessment feedback is explicitly a
test input selecting the path; subsequent Writer and independent Assessment
use the real configured deepseek-v4-pro model. No source checkpoint is mutated.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys

from resume_first10_current_graph import read, write, model_for
from run_resilient_training import transport_error_details
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.consolidation.review import Review
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.consolidation.presentation import build_node_packet, NodeTaskDialogue
from cortex.capabilities.graph_merge.consolidation.contract import node_system, normalize_node_result
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.capabilities.sql_training.graph_merge_evidence import MappingEvidenceRepository, SqlTrainingGraphMergeEvidenceResolver
from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.schema import GraphSpace, GraphState, load_root_schema

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / 'output/toxicology_remaining_merge_pro_20261122'
REPORT = REPO / 'output/toxicology_failure_analysis_20261122/analysis.json'


class ControlledPathAgent(IncrementalIntegrationAgent):
    def __init__(self, *args, folder, path, seed, **kwargs):
        super().__init__(*args, **kwargs)
        self.folder, self.path, self.seed = folder, path, seed
        self.seeded_writer = self.seeded_assessment = False

    def invoke(self, phase, context, payload):
        if phase == 'consolidate' and not self.seeded_writer:
            self.seeded_writer = True
            raw = deepcopy(self.seed)
            raw.pop('relation_followups', None)
            context.node_local_raw = context.active_draft = raw
            packet = build_node_packet(context, {**payload, 'phase': phase}, before=context.task_before)
            write(self.folder / 'historical_initial_draft.json', {
                'origin': 'Reused historical real Writer submission, not a new model call',
                'draft': raw, 'packet': packet})
            value, _ = normalize_node_result('writer', raw, payload=packet, base=context.task_before)
            return value
        if phase == 'node_assess' and not self.seeded_assessment:
            self.seeded_assessment = True
            packet = build_node_packet(context, {**payload, 'phase': phase},
                                       before=context.task_before, candidate=context.candidate)
            assert packet['structural_diagnostics']
            required = (
                'Exercise the relation-removal repair proposal: retain the proposed integration of the three members, '
                'remove original internal edge input::input::e238 using relation_removals, and preserve the supported '
                'endpoint-pair/involvement knowledge and its provenance in the integrated node. Verify the proposal '
                'against the complete original content; do not conceal a real identity error.'
                if self.path == 'remove_relation' else
                'Exercise the grouping-correction repair proposal: put input::input::n202 in keep_separate_ids, '
                'retain it unchanged as a standalone Concept, integrate only input::input::n18 and input::input::n198, '
                'and keep the original is_a assertion from the integrated endpoint-pair node to the standalone '
                'involvement node with its description and sources. Use the original manifest IDs only for the two integrated members.')
            review = Review.model_validate({'verdict': 'revise', 'issues': [{
                'refs': [{'kind': 'edge', 'id': 'input::input::e238', 'view': 'candidate'}],
                'basis': [{'kind': 'edge', 'id': 'input::input::e238', 'view': 'before'},
                          {'kind': 'node', 'id': 'input::input::n202', 'view': 'before'}],
                'problem': 'The current candidate turns the original internal is_a relation into a forbidden self-relation.',
                'required_outcome': required}]})
            dialogue = NodeTaskDialogue(node_system('assessment', 'concept'), packet, context=context)
            write(self.folder / 'controlled_initial_assessment.json', {
                'origin': 'Controlled test feedback selecting a repair path; not represented as a model judgment',
                'messages': [message.model_dump(mode='json') for message in dialogue.messages()],
                'output': review.model_dump(mode='json')})
            return review
        return super().invoke(phase, context, payload)


def run(root, path, slot):
    folder = root / path
    if folder.exists():
        raise ValueError('Preserve old test records; use a new output root')
    folder.mkdir(parents=True)
    snapshot = WorkspaceStore(SOURCE / 'merge/batch-0001/workspace.sqlite').load('continue-toxicology-batch-0001')
    base = GraphState.model_validate(snapshot['task_state']['base'])
    seed = read(REPORT)['last_normal_submission']
    sources = tuple(dict.fromkeys([source for node in base.graph.nodes for source in node.sources]
                                 + [source for edge in base.graph.edges for source in edge.sources]))
    resolver = SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(read(SOURCE / 'source_evidence.json')))
    context = IntegrationContext(base, GraphState(graph=GraphSpace()), load_root_schema(), evidence=resolver.resolve(sources))
    context.sql_dialect = 'sqlite'
    agent = ControlledPathAgent(model_for(folder, slot), folder=folder, path=path, seed=seed,
        max_calls=10, request_max_characters=210000,
        trace_sink=FileSystemGraphMergeTraceSink(folder / 'agents.jsonl'))
    agent.call_budget = {'limit': 30, 'used': 0}
    slots = [slot]

    def recover(error):
        from cortex.capabilities.graph_merge.llm.transport import _connection_failure
        if not _connection_failure(error) or len(slots) >= 3:
            return None
        slots.append((slots[-1] + 2) % len(read_key_slots(REPO / 'configs/sql_training.keys')))
        return model_for(folder, slots[-1])

    agent.recover_transport = recover
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=4, model_call_budget=30)
    engine.session.inputs, engine.session.current, engine.session.lineage = context, base, LineageIndex(context)
    engine.session.history = deepcopy(snapshot['history'])
    engine.session.reviewed_sources, engine.session.revision, engine.session.storage_revision = {}, snapshot['accepted_revision'], None
    engine.session.identity_decision, engine.session.open_tasks = {}, []
    engine.workspace_path = folder / 'workspace.sqlite'
    engine.session.pending_relation_work = engine.session.pending_review = None
    engine.session.relation_followups = []
    task_row = snapshot['open_tasks'][0]
    task = LocalTask(task_row['id'], 'consolidate', set(task_row['targets']), groups=task_row['groups'])
    write(folder / 'before.json', base.model_dump(mode='json'))
    write(folder / 'source_scope.json', {'original_checkpoint': str(SOURCE / 'merge/batch-0001/workspace.sqlite'),
        'case_ids': ['217', '234'], 'accepted_prefix_tasks': [h['task_id'] for h in snapshot['history']],
        'feedback_origin': 'controlled fixture', 'real_model': 'deepseek-v4-pro'})
    result = {'path': path, 'status': 'running'}
    try:
        engine._run_task(task, 'real-node-revision:' + path, aliases=task_row['aliases'])
        saved = WorkspaceStore(engine.workspace_path).load('real-node-revision:' + path)
        assert saved['task_state']['stage'] == 'committed'
        assert engine.session.history[:3] == snapshot['history'] and len(engine.session.history) == 4
        nodes = {node.id: node for node in engine.session.current.graph.nodes}
        edges = {edge.id: edge for edge in engine.session.current.graph.edges}
        if path == 'remove_relation' and 'input::input::e238' not in edges:
            assert 'input::input::e238' not in edges
            original = next(edge for edge in base.graph.edges if edge.id == 'input::input::e238')
            assert set(original.sources) <= set(nodes['input::input::n18'].sources)
            assert saved['task_state']['draft'].get('relation_removals')
        else:
            assert nodes['input::input::n202'] == next(node for node in base.graph.nodes if node.id == 'input::input::n202')
            original = next(edge for edge in base.graph.edges if edge.id == 'input::input::e238')
            actual = edges[original.id]
            assert actual.from_node_id == 'input::input::n18' and actual.to_node_id == 'input::input::n202'
            assert actual.properties == original.properties and actual.sources == original.sources
            assert 'input::input::n202' in saved['task_state']['draft'].get('keep_separate_ids', [])
            if path == 'remove_relation':
                result['requires_trajectory_review'] = True
        result.update(status='passed', independent_real_assessment=True,
            accepted_prefix_preserved=True, real_model_calls=agent.call_budget['used'],
            checkpoint_stage=saved['task_state']['stage'], final_draft=saved['task_state']['draft'],
            final_review=saved['task_state']['review'])
    except Exception as error:
        result.update(status='needs_attention', errors=transport_error_details(error,
            [key.api_key.get_secret_value() for key in read_key_slots(REPO / 'configs/sql_training.keys')]))
    write(folder / 'after.json', engine.session.current.model_dump(mode='json'))
    write(folder / 'result.json', result)
    print(json.dumps({'path': path, 'status': result['status'], 'model_calls': agent.call_budget['used']}), flush=True)
    return result


if __name__ == '__main__':
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, root, 'remove_relation', 1), pool.submit(run, root, 'keep_separate', 3)]
        results = [future.result() for future in futures]
    write(root / 'results.json', results)
    if any(result.get('requires_trajectory_review') for result in results):
        from review_node_revision_trajectory import review
        review(root / 'remove_relation')
