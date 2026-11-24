"""Durable incremental continuation of a reviewed first-ten ontology graph."""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
from html import escape
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from audit_incremental_merge import audit_result
from resume_first10_current_graph import REPO, read, write, write_text, model_for as default_model_for
from merge_model_runtime import configure_merge_model
from run_resilient_training import transport_error_details

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.batch import alias_view
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from graph_merge_archive import migrate_relation_notes
from cortex.capabilities.graph_merge.runtime.state import build_contract_versions, canonical_hash
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.capabilities.sql_training.graph_merge_evidence import MappingEvidenceRepository, SqlTrainingGraphMergeEvidenceResolver
from cortex.capabilities.sql_training.merge_evaluation import compose_case_graphs, _augment_existing_physical_graph
from cortex.infrastructure.artifacts.graph_html import render_graph_html
from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.schema import GraphState, load_root_schema

BASE = REPO / 'output/bird_first10_current_graph_pro_20261121_v2'
QA = REPO / 'output/bird_merge_v4_live_20261119_214104/integration'
CREDENTIAL_POOLS = {'thrombosis_prediction': [0, 2, 4], 'toxicology': [1, 3, 5]}
LOCAL_REQUEST_MAX_CHARACTERS = 210000
NODE_CONTEXT_MAX_CHARACTERS = 110000


def model_for(folder, slot):
    return configure_merge_model(default_model_for(folder, slot), folder)


class OperatorAgent(IncrementalIntegrationAgent):
    """Allow larger complete Identity packets without widening local reviews."""

    def __init__(self, *args, identity_request_max_characters=None, **kwargs):
        super().__init__(*args, **kwargs)
        selected = (self.request_max_characters if identity_request_max_characters is None
                    else identity_request_max_characters)
        if type(selected) is not int or selected < self.request_max_characters:
            raise ValueError('Identity request limit must be an integer at least as large as the local request limit')
        self.identity_request_max_characters = selected

    def invoke(self, phase, context, payload):
        original = self.request_max_characters
        if phase == 'identity':
            self.request_max_characters = self.identity_request_max_characters
        try:
            return super().invoke(phase, context, payload)
        finally:
            # Network/schema exceptions must not leak the Identity allowance
            # into subsequent node, relation or Assessment requests.
            self.request_max_characters = original


def runtime_limits(root):
    path = root / 'runtime_limits.json'
    configured = read(path) if path.exists() else {}
    if not isinstance(configured, dict):
        raise ValueError('runtime_limits.json must contain an object')
    local = configured.get('local_request_max_characters', LOCAL_REQUEST_MAX_CHARACTERS)
    if type(local) is not int or local < LOCAL_REQUEST_MAX_CHARACTERS:
        raise ValueError('Local request limit must be an integer at least 210000 characters')
    selected = configured.get('identity_request_max_characters', local)
    if type(selected) is not int or selected < LOCAL_REQUEST_MAX_CHARACTERS:
        raise ValueError('Identity request limit must be an integer at least 210000 characters')
    if selected < local:
        raise ValueError('Identity request limit must be at least the local request budget')
    return {'identity_request_max_characters': selected,
            'local_request_max_characters': local,
            'node_context_max_characters': NODE_CONTEXT_MAX_CHARACTERS}


def exception_chain(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend((getattr(current, '__cause__', None), getattr(current, '__context__', None)))


def http_status(error):
    status = getattr(error, 'status_code', None)
    return getattr(getattr(error, 'response', None), 'status_code', None) if status is None else status


def credential_failure(error):
    """Rotate rejected credentials, never retry an unrelated permission denial."""
    for current in exception_chain(error):
        status = http_status(current)
        if status == 401:
            return True
        if status == 403:
            detail = str(current) + ' ' + json.dumps(getattr(current, 'body', None), ensure_ascii=False, default=str)
            detail = detail.lower().replace('_', ' ').replace('-', ' ')
            if any(marker in detail for marker in ('insufficient balance', 'balance is insufficient',
                   'not enough balance', 'billing error', '余额不足')):
                return True
    return False


def transport_failure(error):
    # A denied API request does not become transient merely because an older
    # socket exception remains attached as context by the SDK.
    if any(http_status(current) == 403 for current in exception_chain(error)):
        return False
    from cortex.capabilities.graph_merge.llm.transport import _connection_failure
    return _connection_failure(error)


def credential_pool(root, plan):
    path = root / 'runtime_credentials.json'
    configured = read(path) if path.exists() else {}
    if not isinstance(configured, dict):
        raise ValueError('runtime_credentials.json must contain an object')
    selected = configured.get('credential_pool', plan['credential_pool'])
    count = len(read_key_slots(REPO / 'configs/sql_training.keys'))
    if (not isinstance(selected, list) or not selected
            or any(type(slot) is not int or slot < 0 or slot >= count for slot in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('Credential pool must contain distinct valid configured key-slot indices')
    return list(selected)


def next_credential_slot(pool, current, unavailable):
    """Stay inside this worker's pool; stop once every credential is rejected."""
    start = pool.index(current)
    return next((pool[(start + offset) % len(pool)] for offset in range(1, len(pool) + 1)
                 if pool[(start + offset) % len(pool)] not in unavailable), None)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def errors(error):
    return transport_error_details(error, [k.api_key.get_secret_value() for k in read_key_slots(REPO / 'configs/sql_training.keys')])


def prepare(root, database):
    if root.exists():
        raise ValueError('Existing output must be resumed, never overwritten')
    source = BASE / database
    if read(source / 'status.json')['status'] != 'awaiting_user_review':
        raise ValueError('Reviewed first batch has not completed')
    assert all(read(source / 'audit.json')[k] == 'passed' for k in
               ('strict_graph_validation', 'concrete_predicates', 'source_coverage'))
    schema = load_root_schema()
    baseline = GraphState.model_validate(read(source / 'merged_state.json'))
    GraphManager(schema).validate_state(baseline)
    base_ids = read(BASE / 'plan.json')['databases'][database]['case_ids']
    manifest = read(REPO / 'output/latest_graphs/manifest.json')['databases'][database]
    remaining = sorted(set(manifest['cases']) - set(base_ids), key=int)
    evidence = {}
    for item in read(source / 'evidence.json'):
        ident, content = item['source']['source_id'], item['content']
        if ident in evidence and evidence[ident] != content:
            raise ValueError('Inconsistent evidence bodies for one source')
        evidence[ident] = content
    sources, notes = {}, {}
    for case in remaining:
        graph_path = REPO / f'output/latest_graphs/{database}/case_level_graphs/{case}/state.json'
        qa_path = QA / database / 'cases' / case / 'input.json'
        if digest(graph_path) != manifest['cases'][case]['sha256']:
            raise ValueError(f'Accepted source graph changed: {case}')
        graph, migration = migrate_relation_notes(read(graph_path))
        GraphManager(schema).validate_state(graph)
        row = read(qa_path)
        assert row['source_id'] == f'bird-{database}-{case}'
        evidence[row['source_id']] = {k: row[k] for k in ('question', 'sql', 'evidence')}
        sources[case] = {'graph_path': str(graph_path), 'graph_sha256': digest(graph_path),
            'qa_path': str(qa_path), 'qa_sha256': digest(qa_path),
            'normalized_graph_hash': canonical_hash(graph)}
        if migration:
            notes[case] = migration
    assert set(evidence) == {f'bird-{database}-{c}' for c in [*base_ids, *remaining]}
    root.mkdir(parents=True)
    shutil.copyfile(source / 'merged_state.json', root / 'approved_base.json')
    shutil.copyfile(source / 'audit.json', root / 'approved_base_audit.json')
    write(root / 'source_evidence.json', evidence)
    write(root / 'source_migrations.json', notes)
    plan = {'created_at': time.time(), 'database': database, 'model': 'deepseek-v4-pro',
        'batch_size': 10, 'base_case_ids': base_ids, 'remaining_case_ids': remaining,
        'batches': [remaining[i:i + 10] for i in range(0, len(remaining), 10)],
        'excluded_cases': manifest.get('excluded_cases', {}),
        'credential_pool': CREDENTIAL_POOLS[database],
        'approved_base_source': str(source / 'merged_state.json'),
        'approved_base_sha256': digest(root / 'approved_base.json'),
        'sources': sources, 'evidence_hash': canonical_hash(evidence),
        'contracts': build_contract_versions(schema).to_dict(),
        'publish_latest': False, 'other_databases_started': False}
    write(root / 'plan.json', plan)
    write(root / 'status.json', {'status': 'prepared', 'merged_case_ids': base_ids, 'progress_at': time.time()})
    render(root)


def compose_batch(root, plan, case_ids, current, folder):
    """Persist exact batch inputs once; recovery is bound to their fingerprints."""
    if (folder / 'inputs.json').exists():
        inputs = read(folder / 'inputs.json')
        if inputs['case_ids'] != case_ids or inputs['parent_hash'] != canonical_hash(current):
            raise ValueError('Saved batch does not belong to this accepted prefix')
        before = GraphState.model_validate(read(folder / 'before.json'))
        incoming = GraphState.model_validate(read(folder / 'incoming.json'))
        if canonical_hash([before.model_dump(mode='json'), incoming.model_dump(mode='json')]) != inputs['fingerprint']:
            raise ValueError('Batch input checkpoint changed')
        return before, incoming
    cases = []
    for case in case_ids:
        entry = plan['sources'][case]
        if digest(entry['graph_path']) != entry['graph_sha256'] or digest(entry['qa_path']) != entry['qa_sha256']:
            raise ValueError('Source changed after continuation plan was prepared')
        graph, _ = migrate_relation_notes(read(entry['graph_path']))
        assert canonical_hash(graph) == entry['normalized_graph_hash']
        cases.append((f'bird-{plan["database"]}-{case}', graph.graph))
    composition = compose_case_graphs(tuple(cases), current)
    before = _augment_existing_physical_graph(current, composition.catalog)
    # New cases may describe an already-installed catalog fact under a new
    # edge ID. Fold exact physical assertions with their sources before strict
    # validation; no business identity or description consolidation runs here.
    before, catalog_edge_aliases = alias_view(before, {})
    GraphManager(load_root_schema()).validate_state(before)
    incoming = composition.state
    write(folder / 'before.json', before.model_dump(mode='json'))
    write(folder / 'incoming.json', incoming.model_dump(mode='json'))
    write(folder / 'case_input_mappings.json', composition.mappings)
    write(folder / 'catalog_edge_aliases.json', {old: new for old, new in catalog_edge_aliases.items() if old != new})
    write(folder / 'inputs.json', {'case_ids': case_ids, 'parent_hash': canonical_hash(current),
        'fingerprint': canonical_hash([before.model_dump(mode='json'), incoming.model_dump(mode='json')])})
    return before, incoming


def run(root):
    plan = read(root / 'plan.json')
    limits = runtime_limits(root)
    if (root / 'relation_id_protocol.json').exists():
        from cortex.capabilities.graph_merge.llm.relation_merger_ids import adapter_hash, install
        protocol = read(root / 'relation_id_protocol.json')
        if protocol['adapter_hash'] != adapter_hash():
            raise ValueError('Relation local-ID adapter changed; retain checkpoints and review migration')
        install()
    schema = load_root_schema()
    if plan['contracts'] != build_contract_versions(schema).to_dict():
        raise ValueError('Runtime contracts changed; explicit checkpoint migration is required')
    if digest(root / 'approved_base.json') != plan['approved_base_sha256']:
        raise ValueError('Reviewed baseline changed')
    evidence = read(root / 'source_evidence.json')
    assert canonical_hash(evidence) == plan['evidence_hash']
    resolver = SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(evidence))
    current = GraphState.model_validate(read(root / 'approved_base.json'))
    merged = list(plan['base_case_ids'])
    pool = credential_pool(root, plan)
    selected, unavailable = [pool[0]], set()

    def status(state, **data):
        write(root / 'status.json', {'status': state, 'pid': os.getpid(), 'model': 'deepseek-v4-pro',
            'progress_at': time.time(), 'merged_case_ids': merged, 'merged_cases': len(merged),
            'total_cases': len(plan['sources']) + len(plan['base_case_ids']),
            'used_anonymous_key_slots': selected, 'credential_pool': pool,
            'unavailable_credential_slots': sorted(unavailable), 'runtime_limits': limits,
            'model_generation': read(root / 'runtime_model.json') if (root / 'runtime_model.json').exists() else {'thinking_mode': 'provider_default'}, **data})

    try:
        for index, case_ids in enumerate(plan['batches'], 1):
            folder = root / 'merge' / f'batch-{index:04d}'
            folder.mkdir(parents=True, exist_ok=True)
            before, incoming = compose_batch(root, plan, case_ids, current, folder)
            if (folder / 'completed.json').exists():
                accepted = read(folder / 'completed.json')
                if accepted['state_sha256'] != digest(folder / 'state.json'):
                    raise ValueError('Completed batch snapshot changed')
                assert read(folder / 'audit.json')['status'] == 'passed'
                current = GraphState.model_validate(read(folder / 'state.json'))
                merged.extend(case_ids)
                continue
            for window in range(3):
                agent = OperatorAgent(model_for(folder, selected[-1]), max_calls=16,
                    request_max_characters=limits['local_request_max_characters'],
                    identity_request_max_characters=limits['identity_request_max_characters'],
                    trace_sink=FileSystemGraphMergeTraceSink(folder / 'agents.jsonl'))

                def recover(error):
                    rejected_key = credential_failure(error)
                    if not rejected_key and not transport_failure(error):
                        return None
                    failed_slot = selected[-1]
                    if rejected_key:
                        unavailable.add(failed_slot)
                    slot = next_credential_slot(pool, failed_slot, unavailable)
                    with (folder / 'transport_recovery.jsonl').open('a') as stream:
                        stream.write(json.dumps({'time': time.time(), 'failed_anonymous_key_slot': failed_slot,
                                                'next_anonymous_key_slot': slot,
                                                'reason': 'credential_rejected' if rejected_key else 'transport_failure',
                                                'unavailable_credential_slots': sorted(unavailable),
                                                'errors': errors(error)}) + '\n')
                    if slot is None:
                        status('needs_attention', batch=index, batch_case_ids=case_ids,
                               failure_reason='all_credentials_unavailable', errors=errors(error),
                               completed_checkpoints_preserved=True)
                        return None
                    selected.append(slot)
                    status('running', batch=index, batch_case_ids=case_ids,
                           checkpoint_resume=bool(getattr(engine, 'resume_snapshot', None)))
                    return model_for(folder, slot)

                agent.recover_transport = recover
                engine = IncrementalGraphIntegrationEngine(agent, evidence_resolver=resolver,
                    max_rounds=5, model_call_budget=400, sql_dialect='sqlite',
                    node_context_max_characters=limits['node_context_max_characters'])
                engine.workspace_path = folder / 'workspace.sqlite'
                engine.identity_trace_path = folder / 'agents.jsonl'
                run_id = f'continue-{plan["database"]}-batch-{index:04d}'
                if engine.workspace_path.exists():
                    engine.resume_snapshot = WorkspaceStore(engine.workspace_path).load(run_id)
                status('running', batch=index, batch_case_ids=case_ids,
                       checkpoint_resume=bool(getattr(engine, 'resume_snapshot', None)))
                try:
                    result = engine.merge(before, incoming, schema, run_id=run_id)
                    break
                except Exception as error:
                    write(folder / 'last_error.json', {'time': time.time(), 'errors': errors(error)})
                    if credential_failure(error) or not transport_failure(error) or window == 2:
                        raise
                    status('recovering_transport', batch=index, batch_case_ids=case_ids,
                           retry_window=window + 1, errors=errors(error))
                    time.sleep(30 * (window + 1))
                    slot = next_credential_slot(pool, selected[-1], unavailable)
                    if slot is None:
                        raise
                    selected.append(slot)
            write(folder / 'result.json', result.model_dump(mode='json'))
            audit = audit_result(before, incoming, result, schema)
            write(folder / 'audit.json', audit)
            if audit['status'] != 'passed':
                raise ValueError('Batch source/reference audit failed; checkpoint retained')
            GraphManager(schema).validate_state(result.state)
            current = result.state
            write(folder / 'state.json', current.model_dump(mode='json'))
            write_text(folder / 'graph.html', render_graph_html(current.graph, schema,
                       title=plan['database'] + f' · {len(merged) + len(case_ids)} accepted cases'))
            write(folder / 'completed.json', {'case_ids': case_ids, 'state_sha256': digest(folder / 'state.json'),
                  'completed_at': time.time(), 'history_entries': len(engine.history)})
            merged.extend(case_ids)
            write(root / 'merged_checkpoint_state.json', current.model_dump(mode='json'))
            write_text(root / 'merged_preview.html', render_graph_html(current.graph, schema,
                       title=plan['database'] + f' · {len(merged)} accepted cases'))
            status('running', batch=index, batch_completed=True)
        assert set(merged) == set(plan['base_case_ids']) | set(plan['remaining_case_ids'])
        write(root / 'merged_state.json', current.model_dump(mode='json'))
        write_text(root / 'merged_graph.html', render_graph_html(current.graph, schema, title=plan['database'] + ' · completed merged graph'))
        status('complete', nodes=len(current.graph.nodes), edges=len(current.graph.edges),
               completed_batches=len(plan['batches']), node_types=dict(Counter(n.node_type for n in current.graph.nodes)))
    except Exception as error:
        status('needs_attention', errors=errors(error), completed_checkpoints_preserved=True,
               failure_reason='all_credentials_unavailable' if set(pool) <= unavailable else 'merge_stopped')
        print(json.dumps({'status': 'needs_attention', 'errors': errors(error)}), flush=True)


def worker(root):
    with (root / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            run(root)
        except Exception as error:
            write(root / 'status.json', {'status': 'needs_attention', 'errors': errors(error),
                                        'pid': os.getpid(), 'progress_at': time.time()})


def render(root):
    plan, state = read(root / 'plan.json'), read(root / 'status.json')
    panels = []
    for index, cases in enumerate(plan['batches'], 1):
        folder = root / 'merge' / f'batch-{index:04d}'
        summary = {'case_ids': cases, 'status': 'pending', 'model_requests': 0, 'peak_input_characters': 0}
        if folder.exists():
            summary['status'] = 'completed' if (folder / 'completed.json').exists() else 'prepared'
            if summary['status'] != 'completed' and state.get('batch') == index:
                summary['status'] = state['status']
            calls = []
            path = folder / 'model_calls.jsonl'
            if path.exists():
                for line in path.read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get('event_type') == 'model_start':
                        summary['model_requests'] += 1
                    if event.get('event_type') == 'request_budget':
                        summary['peak_input_characters'] = max(summary['peak_input_characters'], event['characters'])
                    if event.get('event_type') in ('model_start', 'model_end', 'model_error'):
                        calls.append('<details><summary>' + escape(event['event_type'] + ' · ' + event['component']) +
                                     '</summary><pre>' + escape(json.dumps(event, ensure_ascii=False, indent=2)) + '</pre></details>')
                write_text(folder / 'agent_io.html', '<!doctype html><meta charset="utf-8"><style>body{font:15px system-ui;margin:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{padding:8px}</style>' + ''.join(calls))
            if (folder / 'workspace.sqlite').exists():
                saved = WorkspaceStore(folder / 'workspace.sqlite').load(f'continue-{plan["database"]}-batch-{index:04d}')
                if saved:
                    ts = saved.get('task_state') or {}
                    summary.update(accepted_tasks=len(saved['history']), active_task=ts.get('task_id'),
                                   checkpoint_stage=ts.get('stage'), checkpoint_final=saved.get('final', False))
                    receipts = saved.get('signal_receipts', [])
                    summary['per_signal_receipts'] = {
                        'accepted_records': len(receipts),
                        'distinct_checks': len({row['check_key'] for row in receipts}),
                        'integrity': 'validated_with_accepted_history',
                        'latest': [{'signal_id': row['check'].get('id'),
                                    'kind': row['check']['kind'],
                                    'accepted_revision': row['accepted_revision'],
                                    'premise_hash': row['premise_hash'],
                                    'task_id': row['task_id']}
                                   for row in receipts[-10:]],
                        'note': 'Original task history is retained; per-check receipts start with independently accepted checks under the current policy.'}
                    summary['active_signal_ids'] = [check.get('id') for task in saved.get('open_tasks', [])
                        if task.get('id') == ts.get('task_id') for check in task.get('checks', [])]
            if (folder / 'last_error.json').exists():
                label = 'last_error' if state['status'] == 'needs_attention' else 'previous_error_retained'
                summary[label] = read(folder / 'last_error.json')
            requests = sorted((folder / 'wire').glob('*_request.json'))
            if requests:
                request = read(requests[-1])
                wire_id = requests[-1].name.split('_')[0]
                progress = folder / 'wire' / (wire_id + '_progress.json')
                summary['latest_request'] = {'time': request.get('time'),
                    'model': request.get('body', {}).get('model'),
                    'anonymous_key_slot': request.get('credential_slot'),
                    'request_bytes': request.get('request_bytes')}
                if progress.exists():
                    snapshot = read(progress)
                    summary['latest_request']['stream'] = {key: snapshot.get(key) for key in
                        ('bytes', 'chunks', 'last_byte_time', 'completed', 'model_complete')}
        relative = f'merge/batch-{index:04d}'
        links = []
        for name, label in [('graph.html', '本批 merged graph'), ('audit.json', '校验'), ('agent_io.html', '完整输入输出')]:
            if (folder / name).exists():
                links.append(f'<a href="{relative}/{name}">{label}</a>')
        panels.append(f'<section><h2>Batch {index} · {len(cases)} cases</h2><p>' + ' · '.join(links) + '</p><pre>' + escape(json.dumps(summary, ensure_ascii=False, indent=2)) + '</pre></section>')
    graph = 'merged_graph.html' if state['status'] == 'complete' else 'merged_preview.html'
    link = f'<p><a href="{graph}?view=business">当前已接受的 merged graph</a></p>' if (root / graph).exists() else ''
    if (root / 'review_assets.json').exists():
        assets = read(root / 'review_assets.json')
        link += '<p>' + ' · '.join('<a href="' + escape(url, quote=True) + '">' + escape(label) + '</a>'
                                   for label, url in assets.items()) + '</p>'
    database = escape(plan['database'])
    write_text(root / 'index.html', '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>' + database + ' incremental merge</title><style>body{font:15px system-ui;margin:32px;background:#f5f6fa}section{background:white;border-radius:12px;padding:24px;margin:20px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>' +
        '<h1>' + database + ' · 首批审核后继续合并</h1><p>' + escape(plan['model']) +
        ' · 每批 ' + str(plan['batch_size']) + ' 条 · 复用首批 ' + str(len(plan['base_case_ids'])) +
        ' 条 · 剩余 ' + str(len(plan['remaining_case_ids'])) + ' 条</p>' + link +
        '<pre>' + escape(json.dumps(state, ensure_ascii=False, indent=2)) + '</pre>' + ''.join(panels))


def monitor(root):
    while True:
        state = read(root / 'status.json')
        if state['status'] in ('running', 'recovering_transport'):
            try:
                os.kill(state['pid'], 0)
            except ProcessLookupError:
                write(root / 'status.json', {**state, 'status': 'needs_attention',
                    'errors': [{'type': 'WorkerExited', 'message': 'Pending checkpoint retained'}]})
        render(root)
        if read(root / 'status.json')['status'] in ('complete', 'needs_attention'):
            return
        time.sleep(20)


def launch(root):
    previous = read(root / 'status.json')
    if previous['status'] in ('running', 'recovering_transport'):
        try:
            os.kill(previous['pid'], 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError('An existing worker is still running; do not launch a duplicate')
    write(root / 'previous_launch_status.json', previous)
    write(root / 'status.json', {**previous, 'status': 'resuming', 'progress_at': time.time()})
    env = {**os.environ, 'LANGCHAIN_TRACING_V2': 'false', 'LANGSMITH_TRACING': 'false', 'PYTHONUNBUFFERED': '1'}
    runtime_source = root / 'runtime_source.json'
    if runtime_source.exists():
        source = Path(read(runtime_source)['src'])
        if not (source / 'cortex').is_dir():
            raise ValueError('The pinned runtime source is unavailable; retain the checkpoint')
        env['PYTHONPATH'] = str(source) + os.pathsep + env.get('PYTHONPATH', '')
    pids = {}
    for mode in ('worker', 'monitor'):
        with (root / f'{mode}.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), mode, '--root', str(root)],
                cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True)
        pids[mode] = child.pid
    write(root / 'background_processes.json', pids)
    print(json.dumps(pids))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'worker', 'monitor', 'render', 'launch'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--database', choices=tuple(CREDENTIAL_POOLS), default='toxicology')
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args.root.resolve(), args.database)
    else:
        globals()[args.mode](args.root.resolve())
