"""Durable parallel case extraction followed by serial, checkpointed graph merge.

Inputs are dataset/split files, not domain-specific rules. Credentials are read
inside workers; only slot numbers/hosts enter manifests. Accepted snapshots alone
are published. Both worker and supervisor can restart from disk.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

from dotenv import load_dotenv

from cortex.application.sql_training import QASqlTrainingRecord, extract_training
from cortex.bootstrap import build_sql_extraction
from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.graph.snapshots import GraphStore
from cortex.capabilities.sql_training.pipeline.train import TrainPipeline
from cortex.config import load_config
from cortex.infrastructure.artifacts.graph_html import render_graph_html
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.schema import GraphSpace, GraphState, load_root_schema


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temp.replace(path)


def read(path):
    return json.loads(Path(path).read_text())


def transient(reason):
    return any(word in str(reason) for word in (
        'ConnectionError', 'Timeout', 'timed out', 'RateLimitError',
        'Error code: 408', 'Error code: 409', 'Error code: 429',
        'Error code: 500', 'Error code: 502', 'Error code: 503', 'Error code: 504',
        'Upstream', 'Assessment output invalid',
    ))


def configuration(args, workspace):
    load_dotenv('.env')
    cfg = load_config(args.config)
    slots = read_key_slots(args.keys)
    train = cfg.services.sql_training.model_copy(update={
        'case_max_workers': None, 'max_rounds': 5, 'max_iterations': 5,
        'agent_max_steps': None, 'agent_episode_timeout': None,
        'required_consecutive_correct_replays': 3,
    })
    cfg = cfg.model_copy(update={
        'services': cfg.services.model_copy(update={'sql_training': train}),
        'llm': cfg.llm.model_copy(update={'model': args.model,
            'api_key': slots[args.slot].api_key, 'timeout': 3600, 'max_retries': 8}),
    })
    model = create_chat_model(cfg.llm, profile='sql_training')
    model.streaming = model.stream_usage = False
    return cfg, model


def observe(model, folder):
    """Count wire attempts, including SDK retries; never record auth headers."""
    lock, starts = Lock(), {}
    wire = folder / 'wire'
    wire.mkdir(exist_ok=True)
    offset = max((int(p.stem.split('_')[0]) for p in wire.glob('*_request.json')), default=0)
    counter = [offset]

    def request(req):
        with lock:
            counter[0] += 1
            ident = counter[0]
            starts[ident] = time.monotonic()
        req.extensions['audit_id'] = ident
        body = json.loads(req.content)
        write(wire / f'{ident:06}_request.json', {'host': urlparse(str(req.url)).hostname,
              'time': time.time(), 'body': body})

    def response(resp):
        ident = resp.request.extensions.get('audit_id', 0)
        resp.read()
        try:
            body = resp.json()
        except ValueError:
            body = {'non_json_response': True, 'characters': len(resp.content)}
        write(wire / f'{ident:06}_response.json', {'status': resp.status_code,
            'seconds': time.monotonic() - starts.get(ident, time.monotonic()), 'body': body})

    model.root_client._client.event_hooks['request'] = [request]
    model.root_client._client.event_hooks['response'] = [response]


def case_worker(args):
    folder = args.output / 'cases' / args.case
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / 'state.json').exists():
        GraphState.model_validate(read(folder / 'state.json'))
        write(folder / 'status.json', {'status': 'passed', 'slot': args.slot, 'updated': time.time()})
        return
    record = QASqlTrainingRecord.model_validate(read(folder / 'input.json'))
    cfg, model = configuration(args, folder / 'workspace')
    observe(model, folder)
    schema = load_root_schema()
    engine = build_sql_extraction(cfg, workspace=folder / 'workspace', llm=model)
    retry = 0
    while True:
        roots = list((folder / 'workspace/train_runs').glob('*/cases/*/case.json'))
        write(folder / 'status.json', {'status': 'running', 'slot': args.slot,
              'resume': bool(roots), 'recovery_attempt': retry, 'updated': time.time()})
        if not roots:
            result = extract_training(engine, (record,), schema, GraphState(graph=GraphSpace()))
            write(folder / 'extraction_result.json', {**result.model_dump(mode='json'), 'status': result.status.value})
            roots = list((folder / 'workspace/train_runs').glob('*/cases/*/case.json'))
            case = result.evaluation_report.cases[0].model_dump(mode='json')
            passed = case['passed']
            reason = case.get('message') or ''
        else:
            pipeline = TrainPipeline(engine.runtime_config, model=engine.llm,
                validation_client_factory=engine.validation_client_factory,
                validation_client=engine.validation_client, database=engine.database, schema=schema)
            case = pipeline.resume_case(roots[0].parent)
            passed = case.get('case_status') == 'passed'
            reason = case.get('reason') or ''
        write(folder / 'attempts' / f'{retry:04}.json', case)
        write(folder / 'case_result.json', case)
        if passed:
            root = roots[0].parent
            store = GraphStore(root / 'ontology', workspace=Workspace(folder / 'workspace'), schema=schema)
            # Checkpoint output carries the exact accepted revision, never newest file.
            revision = case.get('accepted_revision')
            if not revision:
                native = read(Path(case['result_path']))
                revision = native['accepted_revision']
            snapshot = store.read(revision)
            state = GraphState(graph=snapshot.graph)
            write(folder / 'state.json', state.model_dump(mode='json'))
            (folder / 'graph.html').write_text(render_graph_html(state.graph, schema, title=record.source_id))
            write(folder / 'status.json', {'status': 'passed', 'source_id': record.source_id,
                'slot': args.slot, 'semantic_nodes': len(snapshot.semantic_assets), 'updated': time.time()})
            return
        if not transient(reason):
            write(folder / 'status.json', {'status': ('needs_attention' if case.get('completion_reason') == 'operational_failure' else 'needs_review'), 'source_id': record.source_id,
                'reason': reason or case.get('completion_reason'), 'slot': args.slot, 'updated': time.time()})
            return
        retry += 1
        write(folder / 'status.json', {'status': 'retrying', 'source_id': record.source_id,
            'reason': reason, 'slot': args.slot, 'recovery_attempt': retry, 'updated': time.time()})
        time.sleep(min(300, 15 * 2 ** min(retry, 5)))


def merge_worker(args):
    from audit_incremental_merge import audit_result

    from cortex.capabilities.graph_merge.integration.incremental import (
        IncrementalGraphIntegrationEngine,
    )
    from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
    from cortex.capabilities.graph_merge.llm.incremental import (
        IncrementalIntegrationAgent,
    )
    from cortex.capabilities.sql_training.graph_merge_evidence import (
        MappingEvidenceRepository,
        SqlTrainingGraphMergeEvidenceResolver,
    )
    from cortex.infrastructure.artifacts.graph_merge_trace import (
        FileSystemGraphMergeTraceSink,
    )

    _cfg, model = configuration(args, args.output / 'merge')
    schema = load_root_schema()
    current = GraphState(graph=GraphSpace())
    manifest = read(args.output / 'manifest.json')
    evidence = {}
    for ident in manifest['question_ids']:
        row = read(args.output / 'cases' / str(ident) / 'input.json')
        evidence[row['source_id']] = {k: row[k] for k in ('question', 'sql', 'evidence')}
    resolver = SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(evidence))
    merged, omitted = [], []
    for ident in manifest['question_ids']:
        case = args.output / 'cases' / str(ident)
        if not (case / 'state.json').exists():
            omitted.append(ident)
            continue
        out = args.output / 'merge' / str(ident)
        out.mkdir(parents=True, exist_ok=True)
        incoming = GraphState.model_validate(read(case / 'state.json'))
        digest = hashlib.sha256(json.dumps([current.model_dump(mode='json'), incoming.model_dump(mode='json')], sort_keys=True).encode()).hexdigest()
        if (out / 'input_fingerprint.json').exists() and read(out / 'input_fingerprint.json')['sha256'] != digest:
            raise ValueError('Merge prefix/input changed; use a new merge output, never reuse an incompatible step')
        write(out / 'input_fingerprint.json', {'sha256': digest})
        if (out / 'state.json').exists():
            current = GraphState.model_validate(read(out / 'state.json'))
            merged.append(ident)
            continue
        observe(model, out)
        while True:
            agent = IncrementalIntegrationAgent(model, max_calls=None,
                trace_sink=FileSystemGraphMergeTraceSink(out / 'agents.jsonl'))
            engine = IncrementalGraphIntegrationEngine(agent, evidence_resolver=resolver,
                max_rounds=5, model_call_budget=None, sql_dialect='sqlite')
            engine.workspace_path = out / 'workspace.sqlite'
            if engine.workspace_path.exists():
                engine.resume_snapshot = WorkspaceStore(engine.workspace_path).load(f'train-merge-{ident}')
            write(args.output / 'merge_status.json', {'status': 'running', 'question_id': ident,
                'merged': merged, 'omitted': omitted, 'updated': time.time()})
            try:
                result = engine.merge(current, incoming, schema, run_id=f'train-merge-{ident}')
                audit = audit_result(current, incoming, result, schema)
                write(out / 'result.json', result.model_dump(mode='json'))
                write(out / 'audit.json', audit)
                if audit['status'] != 'passed':
                    raise ValueError('Source/reference preservation audit did not pass')
                current = result.state
                write(out / 'state.json', current.model_dump(mode='json'))
                merged.append(ident)
                write(args.output / 'merged_checkpoint_state.json', current.model_dump(mode='json'))
                (args.output / 'merged_preview.html').write_text(render_graph_html(current.graph, schema, title='Partial merge checkpoint'))
                break
            except Exception as error:
                # Retry transport loss from the last accepted workspace only.
                write(out / 'last_error.json', {'type': type(error).__name__, 'error': str(error)})
                if not transient(error):
                    raise
                time.sleep(30)
    write(args.output / 'merged_state.json', current.model_dump(mode='json'))
    (args.output / 'merged_graph.html').write_text(render_graph_html(current.graph, schema, title='Merged training ontology'))
    write(args.output / 'merge_status.json', {'status': 'complete' if not omitted else 'partial',
          'merged': merged, 'omitted': omitted, 'nodes': len(current.graph.nodes),
          'edges': len(current.graph.edges), 'updated': time.time()})


def supervise(args):
    slots = read_key_slots(args.keys)
    manifest = read(args.output / 'manifest.json')
    waiting = [str(x) for x in manifest['question_ids'] if not (args.output / 'cases' / str(x) / 'status.json').exists()
               or read(args.output / 'cases' / str(x) / 'status.json')['status'] not in ('passed', 'needs_review')]
    active = {}
    logfiles = []
    while waiting or active:
        for slot in range(len(slots)):
            if slot in active or not waiting:
                continue
            ident = waiting.pop(0)
            log = (args.output / 'cases' / ident / 'worker.log').open('a')
            logfiles.append(log)
            command = [sys.executable, str(Path(__file__).resolve()), '--mode', 'case', '--case', ident,
                '--slot', str(slot), '--output', str(args.output), '--config', str(args.config),
                '--keys', str(args.keys), '--model', args.model]
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy())
            active[slot] = (ident, process)
        for slot, (ident, process) in list(active.items()):
            if process.poll() is None:
                continue
            if process.returncode:
                write(args.output / 'cases' / ident / 'status.json', {'status': 'needs_attention', 'exit_code': process.returncode, 'slot': slot})
            del active[slot]
        states = {str(i): read(args.output / 'cases' / str(i) / 'status.json') if (args.output / 'cases' / str(i) / 'status.json').exists() else {'status': 'queued'} for i in manifest['question_ids']}
        write(args.output / 'progress.json', {'phase': 'extraction', 'cases': states, 'active': {s: v[0] for s,v in active.items()}, 'updated': time.time()})
        time.sleep(5)
    if any(row['status'] == 'needs_attention' for row in states.values()):
        write(args.output / 'progress.json', {'phase': 'needs_attention', 'cases': states, 'updated': time.time()})
        return
    args.slot = 0
    write(args.output / 'progress.json', {'phase': 'merge', 'updated': time.time()})
    merge_worker(args)
    write(args.output / 'progress.json', {'phase': 'finished', 'updated': time.time()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['supervise', 'case', 'merge'], default='supervise')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--keys', type=Path, required=True)
    parser.add_argument('--model', default='deepseek-v4-pro')
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--case')
    args = parser.parse_args()
    args.output, args.config, args.keys = args.output.resolve(), args.config.resolve(), args.keys.resolve()
    os.environ['LANGSMITH_TRACING'] = os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    if args.mode == 'supervise':
        with (args.output / '.supervisor.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            supervise(args)
    else:
        {'case': case_worker, 'merge': merge_worker}[args.mode](args)


if __name__ == '__main__':
    main()
