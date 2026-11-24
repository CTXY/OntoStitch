"""Resume migrated first-ten graphs; reconcile only, then stop for human review.

Each local transaction owns a resumable SQLite checkpoint. An outstanding task
is always replayed against that checkpoint before another signal is scheduled.
Completed node consolidation and relation descriptions are immutable inputs.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import fcntl
from html import escape
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

from dotenv import load_dotenv

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.publication import Preview
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.planning import _signal_targets
from cortex.capabilities.graph_merge.reconciliation.context import relation_signature
from cortex.capabilities.graph_merge.reconciliation.inspection import inspection_signals
from cortex.capabilities.graph_merge.reconciliation.single_graph import reconcile_current_graph
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.state import build_contract_versions, canonical_hash
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.capabilities.graph_merge.evidence import EvidenceItem
from cortex.config import load_config
from cortex.config.models import LLMConfig
from cortex.infrastructure.artifacts.graph_html import render_graph_html
from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.infrastructure.llm.providers import create_chat_model
from cortex.schema import GraphSpace, GraphState, load_root_schema
from cortex.schema.relation_vocabulary import require_concrete_relation
from run_resilient_training import observe, transport_error_details

REPO = Path(__file__).resolve().parents[1]
MIGRATED = REPO / 'output/single_graph_merge_fix_20261121'
ORIGINAL = REPO / 'output/bird_merge_first10_20261121'
DATABASES = ('thrombosis_prediction', 'toxicology')
SLOTS = {DATABASES[0]: [0, 2, 4], DATABASES[1]: [1, 3, 5]}
TERMINAL = {'awaiting_user_review', 'needs_attention'}


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    tmp.replace(path)


def write_text(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(text)
    tmp.replace(path)


def credentials():
    keys = read_key_slots(REPO / 'configs/sql_training.keys')
    pools = [{keys[i].api_key.get_secret_value() for i in SLOTS[db]} for db in DATABASES]
    if pools[0] & pools[1]:
        raise ValueError('Credential pools overlap; workers must use different API keys')
    return keys


def context(graph, evidence, schema):
    ctx = IntegrationContext(graph, GraphState(graph=GraphSpace()), schema, evidence=evidence)
    ctx.sql_dialect = 'sqlite'
    return ctx


def signals(graph, evidence, schema):
    return inspection_signals(context(graph, evidence, schema), Preview(graph, {}, {}, []))


def task_for(check, graph, number):
    return LocalTask(f'signal-{number:03d}-{check["kind"]}', 'reconcile',
                     _signal_targets(check, graph), checks=[check])


def signature(graph, evidence, schema, task):
    return relation_signature(context(graph, evidence, schema), graph, task)


def prepare(root):
    if root.exists():
        raise ValueError('Preserve existing run; use worker to resume it')
    credentials()
    original = read(ORIGINAL / 'plan.json')
    schema = load_root_schema()
    plan = {'created_at': datetime.now().astimezone().isoformat(),
            'model': 'deepseek-v4-pro', 'batch_size': 10, 'max_batches_per_database': 1,
            'stop_after_batch': 'awaiting_user_review', 'publish_latest': False,
            'migration_root': str(MIGRATED), 'original_root': str(ORIGINAL),
            'runtime_contracts': build_contract_versions(schema).to_dict(),
            'disjoint_credentials_verified': True, 'databases': {}}
    for db, nodes_done, relations_done in [(DATABASES[0], 11, 8), (DATABASES[1], 8, 6)]:
        folder = root / db
        folder.mkdir(parents=True)
        for source, dest in [('after.json', 'before.json'), ('after.json', 'current.json'),
                             ('evidence.json', 'evidence.json'), ('migration.json', 'migration.json'),
                             ('relation_checkpoint.json', 'completed_relation_checkpoint.json')]:
            shutil.copyfile(MIGRATED / db / source, folder / dest)
        graph = GraphState.model_validate(read(folder / 'current.json'))
        ev = tuple(EvidenceItem.model_validate(x) for x in read(folder / 'evidence.json'))
        ids = original['databases'][db]['case_ids']
        assert len(ids) == 10
        source_ids = {e.source.source_id for e in ev}
        assert source_ids == {f'bird-{db}-{case}' for case in ids}
        checks = signals(graph, ev, schema)
        plan['databases'][db] = {'case_ids': ids, 'credential_slots': SLOTS[db],
            'node_tasks_reused': nodes_done, 'relation_groups_reused': relations_done,
            'initial_graph_hash': canonical_hash(graph),
            'evidence_hash': canonical_hash([e.model_dump(mode='json') for e in ev]),
            'initial_signal_counts': dict(Counter(c['kind'] for c in checks))}
        write(folder / 'progress.json', {'completed': [], 'coverage': [], 'pending': None})
        write(folder / 'status.json', {'status': 'prepared', 'progress_at': time.time()})
        write(folder / 'signals.json', checks)
        write_text(folder / 'current_graph.html', render_graph_html(graph.graph, schema, title=db + ' · migrated working graph'))
    write(root / 'plan.json', plan)
    render(root)


def model_for(folder, slot):
    load_dotenv(REPO / '.env')
    cfg = load_config(REPO / 'configs/cortex.yaml').llm.resolve('graph_merge')
    key = credentials()[slot].api_key
    settings = cfg.model_copy(update={'api_key': key, 'model': 'deepseek-v4-pro',
        'enable_thinking': None, 'reasoning_effort': None, 'timeout': 600, 'max_retries': 0})
    model = create_chat_model(LLMConfig.model_validate(settings.model_dump()))
    model.streaming = model.stream_usage = True
    model.metadata = {**(getattr(model, 'metadata', None) or {}), 'credential_slot': slot,
                      'anonymous_key_slot': slot}
    observe(model, folder)
    write(folder / 'model_configuration.json', {'model': settings.model,
        'endpoint_host': urlparse(settings.base_url).hostname, 'anonymous_key_slot': slot,
        'timeout_seconds': 600, 'sdk_retries': 0, 'streaming': True,
        'selected_key_matches_client': model.openai_api_key.get_secret_value() == key.get_secret_value()})
    return model


def pending_task(row):
    return LocalTask(row['id'], 'reconcile', set(row['targets']), checks=row['checks'],
                     feedback=row.get('feedback', []))


def save_completion(folder, progress, task, graph, result, evidence, schema, saved):
    """Crash before progress commit is safe: pending SQLite returns without models."""
    task_folder = folder / 'tasks' / task.id
    write(task_folder / 'after.json', result.model_dump(mode='json'))
    write(task_folder / 'accepted_task_state.json', saved['task_state'])
    write(folder / 'current.json', result.model_dump(mode='json'))
    progress['completed'].append({'task_id': task.id, 'checks': task.checks,
        'before_hash': canonical_hash(graph), 'after_hash': canonical_hash(result),
        'changed': result != graph, 'accepted_at': time.time(),
        'independent_assessment': True, 'history': saved['history']})
    progress['coverage'] = sorted(set(progress['coverage']) | {
        signature(result, evidence, schema, task)})
    progress['pending'] = None
    write(folder / 'progress.json', progress)
    write_text(folder / 'current_graph.html', render_graph_html(result.graph, schema, title=folder.name + ' · accepted working graph'))


def execute(root, db):
    plan = read(root / 'plan.json')
    if plan['runtime_contracts'] != build_contract_versions(load_root_schema()).to_dict():
        raise ValueError('Runtime contract changed: explicit migration is required')
    entry = plan['databases'][db]
    assert len(entry['case_ids']) == 10 and plan['max_batches_per_database'] == 1
    folder = root / db
    if read(folder / 'status.json')['status'] == 'awaiting_user_review':
        return
    ev = tuple(EvidenceItem.model_validate(x) for x in read(folder / 'evidence.json'))
    assert canonical_hash([e.model_dump(mode='json') for e in ev]) == entry['evidence_hash']
    schema = load_root_schema()
    used_slots = [entry['credential_slots'][0]]
    budget = {'limit': 220, 'used': 0}
    write(folder / 'runtime.json', {'pid': os.getpid(), 'model': 'deepseek-v4-pro',
          'credential_pool': entry['credential_slots'], 'started_at': time.time()})

    def status(value, **more):
        write(folder / 'status.json', {'status': value, 'pid': os.getpid(), 'progress_at': time.time(),
              'model': 'deepseek-v4-pro', 'model_calls_this_process': budget['used'],
              'used_anonymous_key_slots': used_slots, 'next_batch_started': False, **more})

    try:
        for _ in range(60):
            progress = read(folder / 'progress.json')
            graph = GraphState.model_validate(read(folder / 'current.json'))
            if progress['pending']:
                row = progress['pending']
                # current.json may have been written just before a crash. The
                # task's immutable input always remains its before.json.
                task = pending_task(row)
                task_folder = folder / 'tasks' / task.id
                graph = GraphState.model_validate(read(task_folder / 'before.json'))
            else:
                current_checks = signals(graph, ev, schema)
                write(folder / 'signals.json', current_checks)
                available = []
                for check in current_checks:
                    probe = task_for(check, graph, len(progress['completed']))
                    if signature(graph, ev, schema, probe) not in progress['coverage']:
                        available.append(check)
                if not available:
                    GraphManager(schema).validate_state(graph)
                    for edge in graph.graph.edges:
                        require_concrete_relation(edge.edge_type)
                    write(folder / 'merged_state.json', graph.model_dump(mode='json'))
                    write_text(folder / 'merged_graph.html', render_graph_html(graph.graph, schema,
                        title=db + ' · first 10 cases · awaiting review'))
                    initial = GraphState.model_validate(read(folder / 'before.json'))
                    refs = lambda g: {s for n in g.graph.nodes for s in (*n.sources, *n.common_sources)} | {
                        s for n in g.graph.nodes for a in n.alternatives for s in a.sources} | {
                        s for e in g.graph.edges for s in e.sources}
                    missing = refs(initial) - refs(graph)
                    if missing:
                        (folder / 'merged_state.json').unlink()
                        (folder / 'merged_graph.html').unlink()
                        raise ValueError('Final source coverage audit failed; accepted local checkpoints retained')
                    write(folder / 'audit.json', {'strict_graph_validation': 'passed',
                        'concrete_predicates': 'passed', 'source_coverage': 'passed',
                        'independent_assessment_tasks': len(progress['completed']),
                        'node_tasks_replayed': 0, 'original_relation_groups_replayed': 0,
                        'original_case_graphs_read_at_runtime': 0})
                    status('awaiting_user_review', nodes=len(graph.graph.nodes), edges=len(graph.graph.edges),
                           completed_tasks=len(progress['completed']), case_ids=entry['case_ids'])
                    return
                available.sort(key=lambda c: (c['kind'] != 'binding_contest', c['id']))
                task = task_for(available[0], graph, len(progress['completed']))
                task_folder = folder / 'tasks' / task.id
                write(task_folder / 'before.json', graph.model_dump(mode='json'))
                row = {'id': task.id, 'targets': sorted(task.targets), 'checks': task.checks,
                       'input_signature': signature(graph, ev, schema, task)}
                write(task_folder / 'task.json', row)
                progress['pending'] = row
                write(folder / 'progress.json', progress)
            status('running', task_id=task.id, signal_kinds=[c['kind'] for c in task.checks],
                   completed_tasks=len(progress['completed']), case_ids=entry['case_ids'])
            agent = IncrementalIntegrationAgent(model_for(task_folder, used_slots[-1]), max_calls=16,
                request_max_characters=210000,
                trace_sink=FileSystemGraphMergeTraceSink(task_folder / 'agents.jsonl'))
            agent.call_budget = budget

            def recover(error):
                from cortex.capabilities.graph_merge.llm.transport import _connection_failure
                if not _connection_failure(error) or len(used_slots) >= len(entry['credential_slots']):
                    return None
                slot = entry['credential_slots'][len(used_slots)]
                used_slots.append(slot)
                with (folder / 'transport_recovery.jsonl').open('a') as stream:
                    stream.write(json.dumps({'time': time.time(), 'next_anonymous_key_slot': slot,
                        'errors': transport_error_details(error, [k.api_key.get_secret_value() for k in credentials()])}) + '\n')
                return model_for(task_folder, slot)

            agent.recover_transport = recover
            checkpoint = task_folder / 'workspace.sqlite'
            result = reconcile_current_graph(agent, graph, schema, ev, task, checkpoint)
            saved = WorkspaceStore(checkpoint).load('single-graph:' + task.id)
            assert saved['task_state']['stage'] == 'committed'
            save_completion(folder, progress, task, graph, result, ev, schema, saved)
        raise RuntimeError('Signal iteration safety limit reached; all completed tasks retained')
    except Exception as error:
        details = transport_error_details(error, [k.api_key.get_secret_value() for k in credentials()])
        status('needs_attention', errors=details, checkpoint_preserved=True)
        print(json.dumps({'database': db, 'status': 'needs_attention', 'errors': details}), flush=True)


def worker(root, db):
    with (root / db / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        execute(root, db)


def trace_html(folder):
    items = []
    stats = {'model_requests_started': 0, 'model_requests_completed': 0,
             'model_request_errors': 0, 'peak_input_characters': 0, 'message_counts': set()}
    for path in sorted((folder / 'tasks').glob('*/model_calls.jsonl')):
        task = path.parent.name
        calls = []
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get('event_type')
            if kind == 'model_start':
                stats['model_requests_started'] += 1
                stats['message_counts'].add(len(event.get('messages', [])))
            elif kind == 'model_end':
                stats['model_requests_completed'] += 1
            elif kind == 'model_error':
                stats['model_request_errors'] += 1
            elif kind == 'request_budget':
                stats['peak_input_characters'] = max(stats['peak_input_characters'], event['characters'])
            if event.get('event_type') in {'model_start', 'model_end', 'model_error'}:
                calls.append('<details><summary>' + escape(event.get('event_type', '') + ' · ' + event.get('component', '')) +
                             '</summary><pre>' + escape(json.dumps(event, ensure_ascii=False, indent=2)) + '</pre></details>')
        items.append('<h2>' + escape(task) + '</h2>' + ''.join(calls))
    write_text(folder / 'agent_io.html', '<!doctype html><meta charset="utf-8"><title>Full model input / output</title>' +
        '<style>body{font:15px system-ui;margin:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{padding:8px}</style>' + ''.join(items))
    stats['message_counts'] = sorted(stats['message_counts'])
    return stats


def render(root):
    plan = read(root / 'plan.json')
    panels = []
    for db in DATABASES:
        folder = root / db
        state = read(folder / 'status.json')
        progress = read(folder / 'progress.json')
        entry = plan['databases'][db]
        request_stats = trace_html(folder)
        pending = progress['pending']
        live = {}
        if pending:
            checkpoint = folder / 'tasks' / pending['id'] / 'workspace.sqlite'
            if checkpoint.exists():
                saved = WorkspaceStore(checkpoint).load('single-graph:' + pending['id'])
                if saved:
                    ts = saved['task_state']
                    live = {k: ts.get(k) for k in ('stage', 'round', 'draft_id', 'candidate_draft_id')}
        configuration = None
        paths = sorted((folder / 'tasks').glob('*/model_configuration.json'))
        if paths:
            configuration = read(paths[-1])
        summary = {**state, **request_stats, 'completed_tasks': len(progress['completed']),
            'current_checkpoint': live, 'initial_signals': entry['initial_signal_counts'],
            'node_tasks_reused': entry['node_tasks_reused'], 'relation_groups_reused': entry['relation_groups_reused'],
            'node_tasks_replayed': 0, 'original_relation_groups_replayed': 0,
            'case_ids': entry['case_ids'], 'credential_pool': entry['credential_slots'],
            'model_configuration': configuration}
        links = f'<a href="{db}/current_graph.html?view=business">当前图（处理中）</a> · <a href="{db}/agent_io.html">每步完整输入输出</a> · <a href="{db}/progress.json">任务记录</a>'
        if state['status'] == 'awaiting_user_review':
            links += f' · <a href="{db}/merged_graph.html?view=business">首批最终 merged graph</a> · <a href="{db}/merged_state.json">Graph JSON</a>'
        panels.append('<section><h2>' + db + '</h2><p>' + links + '</p><pre>' + escape(json.dumps(summary, ensure_ascii=False, indent=2)) + '</pre></section>')
    write_text(root / 'index.html', '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30">' +
        '<title>First 10 merge continuation · deepseek-v4-pro</title><style>body{font:15px system-ui;background:#f5f6fa;margin:32px}section{background:white;padding:24px;margin:20px 0;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#1260b0}</style>' +
        '<h1>首批 10 条 · 当前图 checkpoint 续跑</h1><p>deepseek-v4-pro · 两库使用互不重叠的 API KEY 池 · 仅处理剩余 signals · 生成后停止等待 review</p><p>更新：' +
        escape(datetime.now().astimezone().isoformat()) + '</p>' + ''.join(panels))


def monitor(root):
    while True:
        for db in DATABASES:
            folder = root / db
            state = read(folder / 'status.json')
            if state['status'] == 'running':
                try:
                    os.kill(state['pid'], 0)
                except ProcessLookupError:
                    write(folder / 'status.json', {**state, 'status': 'needs_attention',
                        'errors': {'type': 'WorkerExited', 'message': 'Worker exited; pending checkpoint retained'}})
        render(root)
        if all(read(root / db / 'status.json')['status'] in TERMINAL for db in DATABASES):
            return
        time.sleep(20)


def launch(root):
    credentials()
    processes = {}
    env = {**os.environ, 'LANGCHAIN_TRACING_V2': 'false', 'LANGSMITH_TRACING': 'false', 'PYTHONUNBUFFERED': '1'}
    for mode, db in [('worker', DATABASES[0]), ('worker', DATABASES[1]), ('monitor', None)]:
        name = db or 'monitor'
        command = [sys.executable, str(Path(__file__).resolve()), mode, '--root', str(root)]
        if db:
            command.extend(['--database', db])
        with (root / f'{name}.log').open('ab') as log:
            process = subprocess.Popen(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
        processes[name] = process.pid
    write(root / 'background_processes.json', processes)
    print(json.dumps(processes))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'worker', 'monitor', 'render', 'launch'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--database', choices=DATABASES)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.mode == 'worker':
        worker(root, args.database)
    else:
        globals()[args.mode](root)
