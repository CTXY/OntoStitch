"""Small, isolated node Writer/Assessment probes over existing accepted case graphs.

Default mode prepares real fixtures and HTML without constructing a model. --live
runs only the selected local node tasks through the production compiler and gate.
No input graph, production checkpoint or merge process is modified.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.graph_merge.integration.context import PHYSICAL, IntegrationContext, references, remap_edge, remap_node
from cortex.capabilities.graph_merge.integration.contracts import isolate_contracts
from cortex.capabilities.graph_merge.integration.compiler import deduplicate_edges
from cortex.capabilities.graph_merge.integration.incremental import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.integration.task_state import TaskState, build_contract_versions, canonical_hash
from cortex.capabilities.graph_merge.llm.node_context import NodeTaskDialogue, build_node_packet
from cortex.capabilities.graph_merge.llm.node_contract import node_system
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import GraphSpace, GraphState, SourceReference, load_root_schema, validate_graph_state

REPO = Path(__file__).resolve().parents[1]
GROUPS = (
    ('patient_sex', 'thrombosis_prediction', ('1201', '1237'), 'concept.patient_sex'),
    ('examination', 'thrombosis_prediction', ('1164', '1202'), 'event.examination'),
    ('count_distinct_patients', 'thrombosis_prediction', ('1231', '1247'), 'generation_rule.count_distinct_patients'),
    ('carcinogenic_molecule_count', 'toxicology', ('266', '204'), 'metric.carcinogenic_molecule_count'),
    ('molecule', 'toxicology', ('300', '229'), 'concept.molecule'),
)


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_checkpoint(path, run_id):
    """Open the old journal read-only; migration never writes into its SQLite file."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = connection.execute('SELECT revision, payload FROM merge_workspaces WHERE run_id=?', (run_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError(f'Missing smoke checkpoint for {run_id}')
    value = json.loads(row[1])
    if value.get('revision') != row[0]:
        raise ValueError('Checkpoint storage revision mismatch')
    if value.get('task_state'):
        TaskState.from_dict(value['task_state'])
    GraphState.model_validate(value['current'])
    return value


def migrate_fixture(args, prepared, schema):
    """Create a versioned recovery copy, preserving node/relationship Writer work."""
    from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore

    manifest, base, context, packet = prepared
    name, root = manifest['name'], args.output / manifest['name']
    old_root = args.resume_from / name
    old_manifest = read(old_root / 'fixture.json')
    run_id = 'node-smoke:' + name
    if manifest['source_sha256'] != old_manifest['source_sha256']:
        raise ValueError('Source graph fingerprints differ from the original probe')
    if canonical_hash(base) != canonical_hash(GraphState.model_validate(read(old_root / 'before.json'))):
        raise ValueError('Rebuilt fixture differs from its immutable original before graph')
    manifest['previous_attempt'] = {'report': os.path.relpath(args.resume_from / 'index.html', args.output),
        'fixture': str((old_root / 'fixture.json').resolve()), 'status': old_manifest['status'],
        'errors': old_manifest.get('errors', []), 'model_calls': old_manifest.get('trace', {}).get('model_calls', 0)}
    if old_manifest.get('node_assessment_passed'):
        manifest.update(status='skipped_previously_passed', node_assessment_passed=True,
            skipped_reason='Independent node assessment already passed; no Writer or Assessment rerun.')
        write(root / 'fixture.json', manifest)
        return manifest, base, context, packet
    source = old_root / 'workspace.sqlite'
    source_digest = digest(source)
    original = read_checkpoint(source, run_id)
    if digest(source) != source_digest:
        raise ValueError('Source checkpoint changed while reading its migration snapshot')
    restored = TaskState.from_dict(original['task_state'])
    if original.get('contract_versions') != restored.versions.to_dict():
        raise ValueError('Source workspace/task contract versions disagree')
    if restored.base_hash != canonical_hash(base) or canonical_hash(original['current']) != restored.base_hash:
        raise ValueError('Unfinished checkpoint is not bound to the original accepted base')
    if restored.task_id != run_id or restored.phase != 'consolidate':
        raise ValueError('Checkpoint belongs to a different node task')
    versions = build_contract_versions(schema)
    coupled = deepcopy(restored.coupled_relation)
    if coupled:
        if coupled.get('draft_id') != restored.draft_id or coupled.get('base_hash') != restored.base_hash:
            raise ValueError('Saved relation Writer does not address the preserved node draft/base')
        if coupled.get('patch') is not None and canonical_hash(coupled['patch']) != coupled.get('patch_hash'):
            raise ValueError('Saved relation Writer Patch hash mismatch')
        if coupled.get('review') is not None:
            coupled['historical_review'] = {'contract_versions': restored.versions.to_dict(),
                'review': coupled.pop('review'), 'review_candidate_hash': coupled.pop('review_candidate_hash', None),
                'stage': coupled.get('stage')}
            coupled['stage'] = 'assessing'
    migrated = replace(restored, versions=versions, agent_recovery={}, coupled_relation=coupled,
        review=None, stage='assessing' if restored.stage == 'ready_to_commit' else restored.stage,
        recovery=None)
    TaskState.from_dict(migrated.to_dict())
    audit = {'version': 1, 'source_checkpoint': str(source.resolve()), 'source_checkpoint_sha256': source_digest,
        'source_snapshot_hash': canonical_hash(original), 'old_contract_versions': restored.versions.to_dict(),
        'new_contract_versions': versions.to_dict(), 'before_hash': restored.base_hash,
        'preserved_node_draft_id': restored.draft_id, 'preserved_node_draft_hash': canonical_hash(restored.draft),
        'preserved_candidate_hash': restored.candidate_hash,
        'preserved_relation_intermediate_hash': (coupled or {}).get('intermediate_hash'),
        'preserved_relation_patch_hash': (coupled or {}).get('patch_hash'),
        'preserved_relation_candidate_hash': (coupled or {}).get('candidate_hash'),
        'historical_agent_recovery_records': len(restored.agent_recovery),
        'policy_change': 'Old agent recovery archived and cleared; saved relation Patch retained, old relation assessment moved to historical_review and must be independently reassessed.',
        'created_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')}
    destination = root / 'workspace.sqlite'
    if destination.exists():
        existing = read_checkpoint(destination, run_id)
        previous_audit = read(root / 'migration.json')
        if (previous_audit['source_checkpoint_sha256'] != source_digest
                or existing.get('contract_versions') != versions.to_dict()):
            raise ValueError('Prepared migration policy/source changed; choose a new output directory')
        migrated = TaskState.from_dict(existing['task_state'])
        audit = previous_audit
        if migrated.stage == 'committed' and any(entry.get('task_id') == run_id
                and entry.get('assessment') == 'node_assess' for entry in existing.get('history', [])):
            manifest.update(status='skipped_previously_passed', node_assessment_passed=True,
                migration=audit, resume_workspace=str(destination.resolve()),
                skipped_reason='The migrated node task already passed; no completed work rerun.')
            write(root / 'fixture.json', manifest)
            return manifest, base, context, packet
    else:
        write(root / 'historical_checkpoint.json', original)
        write(root / 'historical_agent_recovery.json', restored.agent_recovery)
        copied = deepcopy(original)
        copied.update(contract_versions=versions.to_dict(), task_state=migrated.to_dict(),
            smoke_migration=audit)
        copied.pop('revision', None)
        WorkspaceStore(destination).save(run_id, copied)
        write(root / 'migration.json', audit)
    manifest.update(status='migrated_ready_not_run', migration=audit,
        resume_workspace=str(destination.resolve()), source_checkpoint_unchanged=digest(source) == source_digest)
    write(root / 'task_state.json', migrated.to_dict())
    write(root / 'fixture.json', manifest)
    return manifest, base, context, packet


def resolve_input_root(requested):
    if requested:
        return Path(requested)
    candidates = sorted((REPO / 'output').glob('bird_merge_v*_live_*/integration'))
    if not candidates:
        raise ValueError('No saved SQL-training input found; provide --source-input-root')
    return candidates[-1]


def project_case(state, selected, case_id):
    """Keep exact group contents, required relation/ref closure and physical catalog.

    Other semantic objects are preserved as separate, case-namespaced endpoints;
    this probe does not assert their identity or alter their descriptions.
    """
    nodes = {node.id: node for node in state.graph.nodes}
    if selected not in nodes:
        raise ValueError(f'Missing selected case node: {selected}')
    keep = {selected} | {node.id for node in state.graph.nodes if node.node_type in PHYSICAL}
    edges = {edge.id: edge for edge in state.graph.edges}
    selected_edges = {edge.id for edge in state.graph.edges if edge.from_node_id == selected
        or (nodes[edge.from_node_id].node_type in PHYSICAL and nodes[edge.to_node_id].node_type in PHYSICAL)}
    while True:
        previous = (set(keep), set(selected_edges))
        keep |= {ref for ident in keep if nodes[ident].node_type not in PHYSICAL for ref in references(nodes[ident])}
        # Endpoint nodes remain context, not additional consolidation tasks. Only
        # formula adjacency and explicitly referenced metadata require their edges.
        metadata = {eid for ident in keep for item in nodes[ident].properties.get('relation_metadata', []) for eid in item['edges']}
        selected_edges |= metadata
        selected_edges |= {edge.id for edge in state.graph.edges if edge.from_node_id in keep
            and nodes[edge.from_node_id].node_type == 'metric' and edge.edge_type in {'depends_on', 'grounded_on'}}
        keep |= {ident for eid in selected_edges for ident in (edges[eid].from_node_id, edges[eid].to_node_id)}
        if keep - nodes.keys():
            raise ValueError(f'Case contains unknown references: {sorted(keep - nodes.keys())}')
        if previous == (keep, selected_edges):
            break
    mapping = {ident: f'case{case_id}::{ident}' for ident in keep if nodes[ident].node_type not in PHYSICAL}
    edge_map = {edge.id: f'case{case_id}::{edge.id}' for edge in state.graph.edges
                if edge.id in selected_edges}
    fragment = state.model_copy(update={'graph': state.graph.model_copy(update={
        'nodes': tuple(remap_node(nodes[ident], mapping, edge_map) for ident in sorted(keep)),
        'edges': tuple(remap_edge(edge, mapping, edge_map) for edge in state.graph.edges if edge.id in edge_map),
    })})
    return fragment, mapping[selected]


def make_fixture(args, spec, schema):
    name, database, case_ids, selected = spec
    root = args.output / name
    source_paths, source_hashes, fragments, members = [], {}, [], []
    evidence_rows = {}
    for case_id in case_ids:
        path = args.graph_root / database / 'case_level_graphs' / case_id / 'state.json'
        original = GraphState.model_validate(read(path))
        fragment, member = project_case(original, selected, case_id)
        fragments.append(fragment)
        members.append(member)
        source_paths.append(str(path.resolve()))
        source_hashes[str(path.resolve())] = digest(path)
        saved = read(args.source_input_root / database / 'cases' / case_id / 'input.json')
        expected = f'bird-{database}-{case_id}'
        if saved.get('source_id') != expected:
            raise ValueError(f'Saved evidence source mismatch for {database}/{case_id}')
        evidence_rows[expected] = {key: saved[key] for key in ('question', 'sql', 'evidence') if key in saved}
    node_index, edge_index, contracts = {}, {}, ()
    for index, fragment in enumerate(fragments):
        contracts, predicates = isolate_contracts(contracts, fragment.graph.relation_contracts,
            schema.edge_type_index(), prefix=f'case{case_ids[index]}__')
        for node in fragment.graph.nodes:
            if node.id not in node_index:
                node_index[node.id] = node
        for edge in fragment.graph.edges:
            edge_index[edge.id] = edge.model_copy(update={'edge_type': predicates.get(edge.edge_type, edge.edge_type)})
    deduplicated, edge_aliases = deduplicate_edges(edge_index.values())
    base = GraphState(graph=GraphSpace(nodes=tuple(remap_node(node, {}, edge_aliases) for node in node_index.values()),
        edges=tuple(deduplicated.values()), relation_contracts=contracts))
    validate_graph_state(base, schema)
    pending_constraints = []
    try:
        GraphManager(schema).validate_state(base)
    except GraphRejected as error:
        if any(issue.code != 'DUPLICATE_CONCEPT_GROUNDING' for issue in error.issues):
            raise
        by_edge = {edge.id: edge for edge in base.graph.edges}
        for issue in error.issues:
            edge_id = issue.path.removeprefix('edges.')
            if by_edge[edge_id].from_node_id not in members or issue.details.get('existing_concept_id') not in members:
                raise
        pending_constraints = [issue.model_dump(mode='json') for issue in error.issues]

    sources = {source for node in base.graph.nodes for source in (*node.sources, *node.common_sources,
        *(source for alternative in node.alternatives for source in alternative.sources))}
    sources.update(source for edge in base.graph.edges for source in edge.sources)
    evidence = tuple(EvidenceItem(id='sql-training:' + source.source_id, source=source, case_id=source.case_id,
        content=evidence_rows[source.source_id]) for source in sorted(sources, key=lambda source: source.model_dump_json())
        if source.source_id in evidence_rows)
    context = IntegrationContext(base, GraphState(graph=GraphSpace()), schema, evidence=evidence)
    group = {'owner_id': members[0], 'member_ids': members}
    context.task_before = context.candidate = base
    context.task_identity_groups = [group]
    packet = build_node_packet(context, {'group': group, 'task_id': 'node-smoke:' + name}, before=base)
    system = node_system('writer', packet['group']['node_type'])
    dialogue = NodeTaskDialogue(system, packet)
    messages = [item.model_dump(mode='json') for item in dialogue.messages()]
    for filename, value in [('before.json', base.model_dump(mode='json')), ('writer.packet.json', packet),
                            ('writer.messages.initial.json', messages), ('evidence.json', [row.model_dump(mode='json') for row in evidence])]:
        write(root / filename, value)
    manifest = {'name': name, 'database': database, 'case_ids': list(case_ids), 'node_type': packet['group']['node_type'],
        'original_node_id': selected, 'member_ids': members, 'owner_id': members[0],
        'source_graphs': source_paths, 'source_sha256': source_hashes,
        'evidence_input_root': str(args.source_input_root.resolve()),
        'preparation': 'Exact group contents and outgoing relations preserved; endpoint nodes included with necessary formula/metadata edges, without expanding their other outgoing relations. Semantic IDs/edge IDs namespaced per case; physical IDs shared and exact catalog edges deduplicated; differing predicate definitions isolated.',
        'expected_premerge_constraints': pending_constraints,
        'before_counts': {'nodes': len(base.graph.nodes), 'edges': len(base.graph.edges)},
        'fixture_mechanical_edge_aliases': {old: new for old, new in edge_aliases.items() if old != new},
        'writer_input': {'members': len(packet['group']['members']), 'relations': len(packet['relations']),
            'endpoints': len(packet['endpoint_directory']), 'allowed_evidence_ids': packet['allowed_evidence_ids'],
            'initial_message_count': len(messages), 'system_characters': len(system)},
        'status': 'prepared_not_run', 'node_assessment_passed': None, 'final_graph_published': False}
    write(root / 'fixture.json', manifest)
    return manifest, base, context, packet


def trace_summary(root):
    path = root / 'model_calls.jsonl'
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    starts = [event for event in events if event.get('event_type') == 'model_start']
    ends = [event for event in events if event.get('event_type') == 'model_end']
    contracts, per_call = {}, []
    for event in events:
        episode = event.get('episode_id')
        if event.get('event_type') == 'request_contract':
            contracts[episode] = event
        elif event.get('event_type') == 'model_start':
            contract = contracts.get(episode)
            advertised = contract['tool_names'] if contract is not None else event.get('tool_names') or None
            per_call.append({'phase': event.get('component'),
                'message_count': len(event.get('messages', [])) or (contract or {}).get('message_count', 0),
                'tool_names': advertised, 'advertised_tools': advertised,
                'advertised_tools_source': 'controller_request_contract' if contract is not None else
                    'provider_callback' if advertised is not None else 'unreported',
                'tools_hash': (contract or {}).get('tools_hash'),
                'message_characters': sum(len(str(item.get('content', ''))) for item in event.get('messages', []))})
    observed = [{'phase': event.get('component'), 'names': [call.get('name') for call in
        event.get('message', {}).get('tool_calls', [])]} for event in ends]
    usage = []
    for event in ends:
        message = event.get('message', {})
        usage.append({'usage_metadata': message.get('usage_metadata'),
            'token_usage': message.get('response_metadata', {}).get('token_usage')})
    cache_reports = []
    for row in usage:
        token = row.get('token_usage') or {}
        details = (row.get('usage_metadata') or {}).get('input_token_details') or {}
        token_details = token.get('prompt_tokens_details') or {}
        hit = token.get('prompt_cache_hit_tokens', details.get('cache_read', token_details.get('cached_tokens')))
        miss = token.get('prompt_cache_miss_tokens', details.get('cache_creation'))
        if hit is not None or miss is not None:
            cache_reports.append({'reported_cache_hit_tokens': hit, 'reported_cache_miss_tokens': miss})
    return {'model_calls': len(starts), 'per_call': per_call, 'observed_model_tool_calls': observed,
        'usage': usage, 'provider_cache': cache_reports or 'unknown_not_reported',
        'model_errors': [{'phase': event.get('component'), 'error': event.get('error')} for event in events if event.get('event_type') == 'model_error'],
        'rejected_tool_results': [{'phase': event.get('component'), 'tool': event.get('name'), 'result': event.get('result')}
            for event in events if event.get('event_type') == 'tool_result' and isinstance(event.get('result'), dict)
            and str(event['result'].get('status', '')).startswith('rejected')]}


def permission_evidence(trace):
    phases = {'integration_consolidate', 'integration_node_assess'}
    allowed = {'read_evidence', 'submit_patch', 'Review'}
    node_calls = [row for row in trace['per_call'] if row['phase'] in phases]
    reported = [row for row in node_calls if row['advertised_tools'] is not None]
    observed = [name for row in trace.get('observed_model_tool_calls', []) if row['phase'] in phases for name in row['names']]
    violation = any(set(row['advertised_tools']) - allowed for row in reported) or bool(set(observed) - allowed)
    status = 'violation' if violation else 'verified' if node_calls and len(reported) == len(node_calls) else 'unreported_or_partial'
    return {'status': status, 'advertised_node_requests': len(reported), 'total_node_requests': len(node_calls),
        'observed_tool_calls': observed, 'valid': False if violation else True if status == 'verified' else None,
        'limits': 'Empty provider tool_names without controller request_contract is unreported, not proof of restricted permissions. Separate code permission tests validate the enforced registry.'}


def run_fixture(args, spec, prepared, model, shared_budget):
    from cortex.capabilities.graph_merge.llm.task_agent import IncrementalIntegrationAgent
    from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
    from run_resilient_training import transport_error_details

    manifest, base, context, packet = prepared
    root = args.output / manifest['name']
    agent = IncrementalIntegrationAgent(model, max_calls=args.max_calls,
        trace_sink=FileSystemGraphMergeTraceSink(root / 'trace.jsonl'))
    agent.call_budget = shared_budget
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=args.max_rounds, model_call_budget=shared_budget['limit'])
    engine.ctx, engine.current, engine.lineage = context, base, LineageIndex(context)
    engine._revision, engine.reviewed_sources, engine.history = 0, {}, []
    engine.workspace_path, engine._storage_revision = root / 'workspace.sqlite', None
    engine.identity_decision, engine.open_tasks = {}, []
    engine.pending_relation_work = engine.pending_review = None
    if manifest.get('resume_workspace'):
        saved = read_checkpoint(manifest['resume_workspace'], 'node-smoke:' + manifest['name'])
        engine.current = GraphState.model_validate(saved['current'])
        engine.lineage = LineageIndex.restore(context, saved['lineage'])
        engine.task_state = TaskState.from_dict(saved['task_state'])
        engine.contract_versions = engine.task_state.versions
        if engine.contract_versions != build_contract_versions(context.schema):
            raise ValueError('Prepared migration policy changed; choose a new output and migrate again')
        engine.history = deepcopy(saved.get('history', []))
        engine.open_tasks = deepcopy(saved.get('open_tasks', []))
        engine._revision = saved.get('accepted_revision', 0)
        engine._storage_revision = saved['revision']
        engine.identity_decision = deepcopy(saved.get('identity', {}))
        engine.reviewed_sources = {key: {SourceReference.model_validate(row) for row in rows}
            for key, rows in saved.get('reviewed_sources', {}).items()}
        engine.pending_relation_work = deepcopy(saved.get('pending_relation_work'))
        engine.pending_review = deepcopy(saved.get('pending_review'))
        engine.relation_followups = deepcopy(saved.get('relation_followups', []))
    members = manifest['member_ids']
    context.task_visible_ids = {node.id for node in base.graph.nodes} | {edge.id for edge in base.graph.edges}
    task = LocalTask('node-smoke:' + manifest['name'], 'consolidate', set(members),
        groups=[{'members': members, 'meaning': 'Same named business identity selected from two accepted case graphs.',
            'reason': 'Targeted node consolidation probe; reassess identity if input contains a concrete counterexample.'}])
    started = time.monotonic()
    manifest.update(status='running', anonymous_key_slot=args.slot)
    write(root / 'fixture.json', manifest)
    try:
        engine._run_task(task, 'node-smoke:' + manifest['name'], aliases={member: members[0] for member in members})
        GraphManager(context.schema).validate_state(engine.current)
        manifest['strict_candidate_validation_passed'] = True
        from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
        saved = WorkspaceStore(root / 'workspace.sqlite').load('node-smoke:' + manifest['name'])
        completed = [entry for entry in engine.history if entry.get('task_id') == task.id and entry.get('assessment') == 'node_assess']
        manifest['node_assessment_passed'] = bool(completed) and saved.get('task_state', {}).get('stage') == 'committed'
        manifest['committed_task_evidence'] = {'history': completed, 'checkpoint_stage': saved.get('task_state', {}).get('stage')}
        write(root / 'task_state.json', saved['task_state'])

        manifest['status'] = 'node_passed' if manifest['node_assessment_passed'] else 'needs_attention'
    except Exception as error:
        manifest['status'] = 'failed'
        manifest['node_assessment_passed'] = False
        secret = getattr(model, 'openai_api_key', None)
        manifest['errors'] = transport_error_details(error, secrets=(secret.get_secret_value(),) if hasattr(secret, 'get_secret_value') else ())
    manifest['elapsed_seconds'] = round(time.monotonic() - started, 2)
    manifest['trace'] = trace_summary(root)
    manifest['node_writer_calls_this_attempt'] = sum(row['phase'] == 'integration_consolidate'
        for row in manifest['trace']['per_call'])
    manifest['relation_writer_calls_this_attempt'] = sum(row['phase'] == 'integration_reconcile'
        for row in manifest['trace']['per_call'])
    manifest['history'] = engine.history
    manifest['task_stage'] = getattr(getattr(engine, 'task_state', None), 'stage', None)
    manifest['pending_relation_work'] = getattr(engine, 'pending_relation_work', None)
    manifest['relation_followups'] = getattr(engine, 'relation_followups', [])
    state = engine.current
    write(root / 'after.json', state.model_dump(mode='json'))
    if getattr(engine, 'task_state', None) is not None:
        write(root / 'task_state.json', engine.task_state.to_dict())
    original_edges = [edge for edge in base.graph.edges if edge.from_node_id in members]
    after_edges = {edge.id: edge for edge in state.graph.edges}
    compiled = getattr(context, 'last_compiled_patch', None)
    aliases = getattr(compiled, 'final_edge_aliases', getattr(compiled, 'edge_aliases', {}))
    edge_checks = []
    for edge in original_edges:
        successor = edge.id
        visited = set()
        while successor in aliases and successor not in visited:
            visited.add(successor)
            successor = aliases[successor]
        current = after_edges.get(successor)
        edge_checks.append({'before_id': edge.id, 'after_id': successor, 'exists': current is not None,
            'predicate_preserved': current is not None and current.edge_type == edge.edge_type,
            'sources_retained': current is not None and set(edge.sources) <= set(current.sources),
            'before_scope': edge.properties.get('alternative_ids'),
            'after_scope': current.properties.get('alternative_ids') if current else None})
    manifest['relation_inheritance'] = edge_checks
    manifest['source_graphs_unchanged'] = all(digest(path) == expected for path, expected in manifest['source_sha256'].items())
    manifest['after_counts'] = {'nodes': len(state.graph.nodes), 'edges': len(state.graph.edges)}
    manifest['tool_permission_evidence'] = permission_evidence(manifest['trace'])
    manifest['actual_tool_permissions_valid'] = manifest['tool_permission_evidence']['valid']
    manifest['auxiliary_structure_coordination_calls'] = [row for row in manifest['trace']['per_call']
        if row['phase'] not in {'integration_consolidate', 'integration_node_assess'}]
    write(root / 'fixture.json', manifest)
    return manifest


def render(args, records, budget=None):
    summary = {'created_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
        'mode': 'live' if args.live else 'dry_run', 'scope': 'Selected node Writer/Assessment tasks and necessary local structural coordination auxiliaries; no full graph merge.',
        'budgets': {'per_episode_max_calls': args.max_calls, 'node_revision_rounds': args.max_rounds,
            'whole_probe_model_calls': budget or {'used': 0, 'limit': args.total_call_budget},
            'prior_attempt_model_calls': getattr(args, 'prior_model_calls', 0),
            'cumulative_model_calls': getattr(args, 'prior_model_calls', 0) + (budget or {}).get('used', 0)},
        'groups': records, 'final_graph_published': False}
    if getattr(args, 'resume_from', None):
        summary['previous_report'] = os.path.relpath(args.resume_from / 'index.html', args.output)
        initial = read(args.resume_from / 'summary.json')
        retried = {record['name']: record for record in records}
        overview = []
        for original in initial['groups']:
            retry = retried.get(original['name'])
            final = retry or original
            initial_trace = trace_summary(args.resume_from / original['name'])
            overview.append({'name': original['name'], 'node_type': original['node_type'],
                'final_status': final['status'], 'node_assessment_passed': final.get('node_assessment_passed'),
                'initial': {'status': original['status'], 'model_calls': original.get('trace', {}).get('model_calls', 0),
                    'errors': original.get('errors', []), 'report': summary['previous_report'],
                    'tool_permissions': permission_evidence(initial_trace)},
                'retry': ({'status': retry['status'], 'model_calls': retry.get('trace', {}).get('model_calls', 0),
                    'errors': retry.get('errors', []), 'report': 'index.html',
                    'independent_assessment_evidence': retry.get('committed_task_evidence'),
                    'tool_permissions': retry.get('tool_permission_evidence')} if retry else None),
                'previously_passed_work_rerun': bool(retry) and original.get('node_assessment_passed', False)})
        summary['five_group_final_overview'] = overview
    write(args.output / 'summary.json', summary)
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>节点整合小样本验证</title><style>',
        'body{font:15px/1.7 system-ui;margin:30px auto;max-width:1200px;padding:0 20px;color:#213047}table{border-collapse:collapse;width:100%}td,th{border:1px solid #d3dce7;padding:10px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f8;padding:16px}details{margin:15px 0}a{color:#1b62af}</style>',
        '<h1>节点整合：已有 case-level graphs 小样本验证</h1>',
        '<p>测试当前两个节点 Agent；必要的局部结构协调辅助会单独列出，不启动全量 merge。原始图与生产 checkpoint 保留，节点通过不代表整图关系协调完成。</p>',
        f'<p>模式：{escape(summary["mode"])}；调用预算：{escape(json.dumps(summary["budgets"],ensure_ascii=False))}。<a href="summary.json">完整 JSON</a></p>',
        '<table><tr><th>Group</th><th>数据库 / cases</th><th>类型</th><th>状态</th><th>实际调用</th><th>缓存</th></tr>']
    if summary.get('previous_report'):
        parts.insert(-1, f'<p><a href="{escape(summary["previous_report"],quote=True)}">初次五组测试报告（保留不变）</a>；本页仅续跑失败组，迁移记录中明确列出旧/新契约与保留的 Patch。</p>')
        rows = ['<h2>五组最终状态总览</h2><table><tr><th>Group</th><th>最终状态</th><th>初次状态 / 调用</th><th>本次状态 / 调用</th></tr>']
        for record in summary['five_group_final_overview']:
            original, retry = record['initial'], record['retry']
            rows.append(f'<tr><td>{escape(record["name"])}</td><td>{escape(str(record["final_status"]))}</td>'
                f'<td><a href="{escape(original["report"],quote=True)}">{escape(original["status"])} / {original["model_calls"]}</a></td>'
                f'<td>{escape(retry["status"]) + " / " + str(retry["model_calls"]) if retry else "沿用原通过结果；未重跑"}</td></tr>')
        rows.append('</table><h2>本轮迁移与续跑详情</h2>')
        parts[-1:-1] = rows
    for record in records:
        trace = record.get('trace', {})
        parts.append('<tr>' + ''.join(f'<td>{escape(str(value))}</td>' for value in [record['name'],
            record['database'] + ' / ' + ', '.join(record['case_ids']), record['node_type'], record['status'],
            trace.get('model_calls', 0), trace.get('provider_cache', '未调用')]) + '</tr>')
    parts.append('</table>')
    for record in records:
        name = record['name']
        parts.append(f'<h2>{escape(name)}</h2><p>')
        for filename in ['fixture.json', 'before.json', 'writer.packet.json', 'writer.messages.initial.json', 'evidence.json',
                         'after.json', 'task_state.json', 'model_calls.jsonl', 'migration.json',
                         'historical_checkpoint.json', 'historical_agent_recovery.json']:
            if (args.output / name / filename).exists():
                parts.append(f'<a href="{name}/{filename}">{filename}</a> · ')
        parts.append('</p>')
        for filename, label in [('before.json', '合并前选中成员（完整节点）'), ('after.json', '合并后 owner（如已接受）')]:
            path = args.output / name / filename
            if path.exists():
                state = read(path)
                wanted = set(record['member_ids']) if filename == 'before.json' else {record['owner_id']}
                nodes = [node for node in state['graph']['nodes'] if node['id'] in wanted]
                parts.append(f'<details><summary>{escape(label)}</summary><pre>{escape(json.dumps(nodes,ensure_ascii=False,indent=2))}</pre></details>')
        parts.append(f'<details><summary>输入、工具、关系保留与真实诊断</summary><pre>{escape(json.dumps(record,ensure_ascii=False,indent=2))}</pre></details>')
    parts.append('</html>')
    (args.output / 'index.html').write_text(''.join(parts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=REPO / 'output' / ('node_consolidation_smoke_' + datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S')))
    parser.add_argument('--graph-root', type=Path, default=REPO / 'output/latest_graphs')
    parser.add_argument('--source-input-root', type=Path)
    parser.add_argument('--resume-from', type=Path, help='Migrate failed local checkpoints into a distinct new output, preserving old records.')
    parser.add_argument('--groups', nargs='+', choices=[spec[0] for spec in GROUPS])
    parser.add_argument('--live', action='store_true', help='Explicitly invoke real model calls; default only prepares fixtures.')
    parser.add_argument('--report-only', action='store_true', help='Refresh saved fixture reports and HTML, without preparing inputs or calling a model.')
    parser.add_argument('--config', type=Path, default=REPO / 'configs/cortex.yaml')
    parser.add_argument('--keys', type=Path, default=REPO / 'configs/sql_training.keys')
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--model')
    parser.add_argument('--max-calls', type=int, default=6)
    parser.add_argument('--max-rounds', type=int, default=1)
    parser.add_argument('--total-call-budget', type=int, default=60)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.resume_from:
        args.resume_from = args.resume_from.resolve()
        if args.resume_from == args.output:
            parser.error('Resume migration requires a distinct output directory')
        initial_summary = read(args.resume_from / 'summary.json')
        args.prior_model_calls = initial_summary['budgets'].get('cumulative_model_calls',
            initial_summary['budgets']['whole_probe_model_calls']['used'])
        args.total_call_budget = min(args.total_call_budget, 12, 50 - args.prior_model_calls)
        if args.total_call_budget < 1:
            parser.error('The cumulative 50-call smoke budget is exhausted')
    if args.report_only:
        records = []
        previous = read(args.output / 'summary.json') if (args.output / 'summary.json').exists() else {}
        args.live = previous.get('mode') == 'live'
        args.prior_model_calls = previous.get('budgets', {}).get('prior_attempt_model_calls', getattr(args, 'prior_model_calls', 0))
        if not args.resume_from and previous.get('previous_report'):
            args.resume_from = (args.output / previous['previous_report']).resolve().parent
        for spec in GROUPS:
            path = args.output / spec[0] / 'fixture.json'
            if not path.exists() or (args.groups and spec[0] not in args.groups):
                continue
            record = read(path)
            if record.get('status') not in {'prepared_not_run', 'migrated_ready_not_run', 'skipped_previously_passed'}:
                record['trace'] = trace_summary(path.parent)
                saved_task = read(path.parent / 'task_state.json') if (path.parent / 'task_state.json').exists() else {}
                if record.get('node_assessment_passed') and (path.parent / 'workspace.sqlite').exists():
                    saved_task = read_checkpoint(path.parent / 'workspace.sqlite', 'node-smoke:' + record['name']).get('task_state') or saved_task
                    write(path.parent / 'task_state.json', saved_task)
                record['node_writer_calls_this_attempt'] = sum(row['phase'] == 'integration_consolidate' for row in record['trace']['per_call'])
                record['relation_writer_calls_this_attempt'] = sum(row['phase'] == 'integration_reconcile' for row in record['trace']['per_call'])
                record['new_policy_graph_assessment_calls'] = sum(row['phase'] == 'integration_graph_assess' for row in record['trace']['per_call'])
                record['node_assessment_calls_this_attempt'] = sum(row['phase'] == 'integration_node_assess' for row in record['trace']['per_call'])
                if record.get('node_assessment_passed'):
                    before_state = GraphState.model_validate(read(path.parent / 'before.json'))
                    after_state = GraphState.model_validate(read(path.parent / 'after.json'))
                    final_edges = {edge.id: edge for edge in after_state.graph.edges}
                    transitions, reasons = {}, {}
                    for entry in record.get('history', []):
                        for change in entry.get('changes', []):
                            targets = [row['id'] for row in change.get('after', []) if row['kind'] == 'edge']
                            for origin in change.get('before', []):
                                if origin['kind'] == 'edge' and targets and targets != [origin['id']]:
                                    transitions[origin['id']] = targets
                                    reasons[origin['id']] = change.get('explanation')
                    checks = []
                    for edge in before_state.graph.edges:
                        if edge.from_node_id not in record['member_ids']:
                            continue
                        ids = transitions.get(edge.id, [edge.id])
                        successors = [final_edges[ident] for ident in ids if ident in final_edges]
                        checks.append({'before_id': edge.id, 'after_id': ids[0] if len(ids) == 1 else None,
                            'after_ids': ids, 'exists': len(successors) == len(ids),
                            'explicit_semantic_successor': reasons.get(edge.id),
                            'predicate_preserved': bool(successors) and all(item.edge_type == edge.edge_type for item in successors),
                            'sources_retained': bool(successors) and set(edge.sources) <= {source for item in successors for source in item.sources},
                            'before_scope': edge.properties.get('alternative_ids'),
                            'after_scopes': [item.properties.get('alternative_ids') for item in successors]})
                    record['relation_inheritance'] = checks
                if record.get('migration'):
                    migration = record['migration']
                    coupled = saved_task.get('coupled_relation') or {}
                    record['source_checkpoint_unchanged'] = digest(migration['source_checkpoint']) == migration['source_checkpoint_sha256']
                    record['completed_writer_reuse'] = {
                        'node_writer_preserved_without_rerun': record['node_writer_calls_this_attempt'] == 0
                            and saved_task.get('draft_id') == migration['preserved_node_draft_id'],
                        'relation_writer_preserved_without_rerun': bool(migration.get('preserved_relation_patch_hash'))
                            and record['relation_writer_calls_this_attempt'] == 0
                            and coupled.get('patch_hash') == migration['preserved_relation_patch_hash'],
                        'new_policy_graph_review_observed': record['new_policy_graph_assessment_calls'] > 0
                            and coupled.get('review', {}).get('verdict') == 'accept'
                            and coupled.get('review_candidate_hash') == coupled.get('candidate_hash')}
                record['last_structural_issues'] = saved_task.get('issues', [])
                record['last_semantic_issues'] = saved_task.get('semantic_issues', [])
                coupled = saved_task.get('coupled_relation') or {}
                if coupled:
                    record['coupled_coordination_evidence'] = {key: coupled.get(key) for key in
                        ('stage', 'candidate_hash', 'review_candidate_hash', 'review', 'patch')}
                if record.get('node_assessment_passed'):
                    record['outcome_category'] = 'independent_node_assessment_passed'
                elif any('stream' in str(error.get('message', '')).lower() for error in record.get('errors', [])):
                    record['outcome_category'] = 'transport_interrupted'
                elif 'Alias destination does not survive' in json.dumps(record['last_structural_issues']):
                    record['outcome_category'] = 'compiler_alias_after_accepted_structural_coordination'
                elif record['last_semantic_issues']:
                    record['outcome_category'] = 'independent_assessment_needs_revision'
                else:
                    record['outcome_category'] = 'incomplete_see_exact_diagnostics'

                record['tool_permission_evidence'] = permission_evidence(record['trace'])
                record['actual_tool_permissions_valid'] = record['tool_permission_evidence']['valid']
                record['auxiliary_structure_coordination_calls'] = [row for row in record['trace']['per_call']
                    if row['phase'] not in {'integration_consolidate', 'integration_node_assess'}]
                if record.get('node_assessment_passed'):
                    GraphManager(load_root_schema()).validate_state(GraphState.model_validate(read(path.parent / 'after.json')))
                    record['strict_candidate_validation_passed'] = True
                write(path, record)
            records.append(record)
        render(args, records, previous.get('budgets', {}).get('whole_probe_model_calls'))
        print(json.dumps({'html': str(args.output / 'index.html'), 'groups': len(records), 'mode': 'report_only'}, ensure_ascii=False))
        return
    args.source_input_root = resolve_input_root(args.source_input_root)
    if args.max_calls < 1 or args.max_rounds < 0 or not 1 <= args.total_call_budget <= 60:
        parser.error('Use positive calls, nonnegative rounds and a whole-probe budget of 1–60 calls.')
    schema = load_root_schema()
    selected = [spec for spec in GROUPS if not args.groups or spec[0] in args.groups]
    if args.resume_from and not args.groups:
        selected = [spec for spec in selected if (args.resume_from / spec[0] / 'fixture.json').exists()
            and not read(args.resume_from / spec[0] / 'fixture.json').get('node_assessment_passed')]
    prepared = [make_fixture(args, spec, schema) for spec in selected]
    if args.resume_from:
        prepared = [migrate_fixture(args, item, schema) for item in prepared]
    records = [item[0] for item in prepared]
    render(args, records)
    if args.live:
        from dotenv import load_dotenv
        from cortex.config import load_config
        from run_resilient_training import configuration
        load_dotenv(REPO / '.env')
        if args.model is None:
            args.model = load_config(args.config).llm.model
        args.streaming, args.sdk_retries, args.merge_transport = True, 0, True
        _, model = configuration(args, args.output)
        budget = {'used': 0, 'limit': args.total_call_budget}
        for index, (spec, fixture) in enumerate(zip(selected, prepared)):
            if fixture[0].get('status') == 'skipped_previously_passed':
                continue
            records[index] = run_fixture(args, spec, fixture, model, budget)
            render(args, records, budget)
            print(json.dumps({'group': spec[0], 'status': records[index]['status'], 'model_calls_used': budget['used']},ensure_ascii=False),flush=True)
            if budget['used'] >= budget['limit']:
                break
    print(json.dumps({'html': str(args.output / 'index.html'), 'mode': 'live' if args.live else 'dry_run',
        'groups': len(records), 'statuses': {row['name']: row['status'] for row in records}},ensure_ascii=False))


if __name__ == '__main__':
    main()
