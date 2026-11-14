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
    message = f'{type(reason).__name__}: {reason}' if isinstance(reason, BaseException) else str(reason)
    return any(word in message for word in (
        'ConnectionError', 'Timeout', 'timed out', 'RateLimitError',
        'Broken pipe', 'Connection reset', 'RemoteProtocolError',
        'incomplete chunked read', 'peer closed connection',
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
    model.streaming = model.stream_usage = getattr(args, 'streaming', False)
    return cfg, model


def observe(model, folder):
    """Count wire attempts, including SDK retries; never record auth headers."""
    lock, starts = Lock(), {}
    wire = folder / 'wire'
    wire.mkdir(parents=True, exist_ok=True)
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
        if 'text/event-stream' in resp.headers.get('content-type', ''):
            upstream = resp.stream
            # Respect the SDK's actual HTTP client namespace (some gateways
            # bundle an httpx-compatible fork); its stream type is checked by
            # isinstance inside Response.read/close.
            stream_base = next(t for t in type(upstream).__mro__ if t.__name__ == 'SyncByteStream')

            class RecordingStream(stream_base):
                def __iter__(self):
                    with (wire / f'{ident:06}_stream.sse').open('wb') as output:
                        for chunk in upstream:
                            output.write(chunk)
                            output.flush()
                            yield chunk

                def close(self):
                    upstream.close()

            resp.stream = RecordingStream()
        resp.read()
        try:
            body = resp.json()
        except ValueError:
            if 'text/event-stream' in resp.headers.get('content-type', '') or resp.text.lstrip().startswith('data:'):
                from cortex.infrastructure.llm.stream_audit import (
                    completion_from_sse,
                    normalize_sse_frames,
                )

                (wire / f'{ident:06}_stream.sse').write_text(resp.text)
                try:
                    body = completion_from_sse(resp.text)
                except (ValueError, TypeError, KeyError) as error:
                    body = {'stream_decode_error': type(error).__name__, 'characters': len(resp.content)}
                # The body is already buffered for audit. Normalize only
                # malformed SSE framing in this SDK read buffer, never graph
                # data or the raw saved response. Some gateways prepend a
                # heartbeat JSON line without its required event separator.
                resp._content = normalize_sse_frames(resp.text).encode()
            else:
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
    from cortex.capabilities.sql_training.merge_evaluation import (
        _combined_incoming_state,
    )
    from cortex.infrastructure.artifacts.graph_merge_trace import (
        FileSystemGraphMergeTraceSink,
    )

    _cfg, model = configuration(args, args.output / 'merge')
    schema = load_root_schema()
    manifest = read(args.output / 'manifest.json')
    base = manifest.get('merge_base_state')
    if base and manifest.get('merge_base_sha256') != hashlib.sha256((args.output / base).read_bytes()).hexdigest():
        raise ValueError('Accepted merge base fingerprint mismatch')
    current = GraphState.model_validate(read(args.output / base)) if base else GraphState(graph=GraphSpace())
    evidence = {}
    for ident in manifest['question_ids']:
        row = read(args.output / 'cases' / str(ident) / 'input.json')
        evidence[row['source_id']] = {k: row[k] for k in ('question', 'sql', 'evidence')}
    resolver = SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(evidence))
    merged, omitted = list(manifest.get('merge_base_case_ids', [])), []
    remaining = []
    for ident in manifest['question_ids']:
        if ident in merged:
            continue
        case = args.output / 'cases' / str(ident)
        if not (case / 'state.json').exists():
            omitted.append(ident)
            continue
        remaining.append(ident)
    size = manifest.get('merge_batch_size', getattr(args, 'merge_batch_size', 10))
    size = size or max(1, len(remaining))
    batches = [remaining[i:i + size] for i in range(0, len(remaining), size)]
    write(args.output / 'merge_plan.json', {'base_cases': merged, 'batches': batches, 'omitted': omitted})
    for index, batch in enumerate(batches, 1):
        ident = f'batch-{index:04}'
        cases = tuple((read(args.output / 'cases' / str(k) / 'input.json')['source_id'],
            GraphState.model_validate(read(args.output / 'cases' / str(k) / 'state.json')).graph) for k in batch)
        out = args.output / 'merge' / str(ident)
        out.mkdir(parents=True, exist_ok=True)
        incoming = _combined_incoming_state(cases, current)
        node_offset = edge_offset = 0
        mappings = {}
        for source, graph in cases:
            mappings[source] = {
                'nodes': {n.id: incoming.graph.nodes[node_offset + i].id for i, n in enumerate(graph.nodes)},
                'edges': {e.id: incoming.graph.edges[edge_offset + i].id for i, e in enumerate(graph.edges)},
            }
            node_offset += len(graph.nodes)
            edge_offset += len(graph.edges)
        digest = hashlib.sha256(json.dumps([current.model_dump(mode='json'), incoming.model_dump(mode='json')], sort_keys=True).encode()).hexdigest()
        if (out / 'input_fingerprint.json').exists() and read(out / 'input_fingerprint.json')['sha256'] != digest:
            raise ValueError('Merge prefix/input changed; use a new merge output, never reuse an incompatible step')
        write(out / 'input_fingerprint.json', {'sha256': digest})
        write(out / 'incoming_state.json', incoming.model_dump(mode='json'))
        write(out / 'case_input_mappings.json', mappings)
        if (out / 'state.json').exists():
            current = GraphState.model_validate(read(out / 'state.json'))
            merged.extend(batch)
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
                'question_ids': batch, 'merged': merged, 'omitted': omitted,
                'slot': args.slot, 'updated': time.time()})
            try:
                result = engine.merge(current, incoming, schema, run_id=f'train-merge-{ident}')
                audit = audit_result(current, incoming, result, schema)
                write(out / 'result.json', result.model_dump(mode='json'))
                write(out / 'audit.json', audit)
                if audit['status'] != 'passed':
                    raise ValueError('Source/reference preservation audit did not pass')
                current = result.state
                write(out / 'state.json', current.model_dump(mode='json'))
                merged.extend(batch)
                write(args.output / 'merged_checkpoint_state.json', current.model_dump(mode='json'))
                (args.output / 'merged_preview.html').write_text(render_graph_html(current.graph, schema, title='Partial merge checkpoint'))
                break
            except Exception as error:
                # Retry transport loss from the last accepted workspace only.
                write(out / 'last_error.json', {'type': type(error).__name__, 'error': str(error)})
                if not transient(error):
                    raise
                time.sleep(30)
                args.slot = (args.slot + 1) % len(read_key_slots(args.keys))
                _cfg, model = configuration(args, args.output / 'merge')
                observe(model, out)
    write(args.output / 'merged_state.json', current.model_dump(mode='json'))
    (args.output / 'merged_graph.html').write_text(render_graph_html(current.graph, schema, title='Merged training ontology'))
    write(args.output / 'merge_status.json', {'status': 'complete' if not omitted else 'partial',
          'merged': merged, 'omitted': omitted, 'nodes': len(current.graph.nodes),
          'edges': len(current.graph.edges), 'updated': time.time()})


class AdoptedWorker:
    """Observe a surviving child without spawning a duplicate writer."""
    def __init__(self, pid, folder):
        self.pid, self.folder = pid, folder

    def poll(self):
        status = read(self.folder / 'status.json').get('status') if (self.folder / 'status.json').exists() else None
        if status in ('passed', 'needs_review', 'needs_attention'):
            return 0  # Completed adopted children may remain as unreaped OS entries.
        try:
            os.kill(self.pid, 0)
            info = subprocess.run(['ps', '-o', 'stat=', '-p', str(self.pid)], check=False, capture_output=True, text=True)
            return 1 if info.stdout.strip().startswith('Z') else None
        except ProcessLookupError:
            return 1


def stalled_request(folder, timeout=3660):
    """Bound total wire silence too: socket-idle timeouts can reset on keepalives."""
    requests = sorted((folder / 'wire').glob('*_request.json'))
    if not requests:
        return None
    latest = requests[-1]
    recovered = folder / 'transport_recoveries.jsonl'
    if recovered.exists() and latest.name in {json.loads(line)['request'] for line in recovered.read_text().splitlines() if line.strip()}:
        return None
    response = latest.with_name(latest.name.replace('_request', '_response'))
    if not response.exists() and time.time() - latest.stat().st_mtime > timeout:
        return latest.name
    return None


def stalled_merge(output):
    status = output / 'merge_status.json'
    if not status.exists() or not (output / 'supervisor.pid').exists():
        return None
    ident = read(status).get('question_id')
    if ident is None:
        return None
    folder = output / 'merge' / str(ident)
    request = stalled_request(folder)
    # An unanswered request copied from an earlier attempt is not a live call.
    if request and (folder / 'wire' / request).stat().st_mtime >= (output / 'supervisor.pid').stat().st_mtime:
        return folder, request
    return None


def watch(args):
    """Recover a silent merge process; deterministic failures remain visible."""
    while True:
        progress = read(args.output / 'progress.json')
        if progress['phase'] in ('finished', 'needs_attention', 'superseded'):
            return
        stale = stalled_merge(args.output) if progress['phase'] == 'merge' else None
        if stale:
            pid = int((args.output / 'supervisor.pid').read_text())
            command = subprocess.run(['ps', '-o', 'args=', '-p', str(pid)],
                capture_output=True, text=True, check=False).stdout
            if 'run_resilient_training.py' not in command or str(args.output) not in command:
                raise RuntimeError('Cannot verify the active merge process identity')
            folder, request = stale
            with (folder / 'transport_recoveries.jsonl').open('a') as log:
                log.write(json.dumps({'request': request, 'reason': 'Silent merge transport deadline', 'time': time.time()}) + '\n')
            os.kill(pid, 15)
            # Wait for the writer lock to be released; never kill another PID.
            for _ in range(30):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                    capture_output=True, text=True, check=False).stdout.strip()
                if not state or state.startswith('Z'):
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
                    break
                time.sleep(1)
            else:
                raise RuntimeError('Merge process did not exit after transport recovery')
            write(args.output / 'progress.json', {'phase': 'recovering', 'updated': time.time()})
            slot = (read(args.output / 'merge_status.json').get('slot', 0) + 1) % len(read_key_slots(args.keys))
            with (args.output / 'supervisor.log').open('a') as log:
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                    '--output', str(args.output), '--config', str(args.config),
                    '--keys', str(args.keys), '--model', args.model, '--slot', str(slot),
                    '--merge-batch-size', str(args.merge_batch_size),
                    *(['--streaming'] if args.streaming else [])],
                    stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy(), start_new_session=True)
            write(args.output / 'supervisor.pid', child.pid)
        time.sleep(5)


def supervise(args):
    slots = read_key_slots(args.keys)
    manifest = read(args.output / 'manifest.json')
    waiting = [str(x) for x in manifest['question_ids'] if not (args.output / 'cases' / str(x) / 'status.json').exists()
               or read(args.output / 'cases' / str(x) / 'status.json')['status'] not in ('passed', 'needs_review')]
    active = {}
    registry = args.output / 'active_workers.json'
    if registry.exists():
        for slot, item in read(registry).items():
            worker = AdoptedWorker(item['pid'], args.output / 'cases' / item['case'])
            if worker.poll() is None:
                active[int(slot)] = (item['case'], worker)
                if item['case'] in waiting:
                    waiting.remove(item['case'])
    logfiles = []
    watchdog_retries = set()
    states = {str(i): read(args.output / 'cases' / str(i) / 'status.json') if (args.output / 'cases' / str(i) / 'status.json').exists() else {'status': 'queued'} for i in manifest['question_ids']}
    while waiting or active:
        for slot in range(len(slots)):
            if slot in active or not waiting:
                continue
            ident = waiting.pop(0)
            log = (args.output / 'cases' / ident / 'worker.log').open('a')
            logfiles.append(log)
            command = [sys.executable, str(Path(__file__).resolve()), '--mode', 'case', '--case', ident,
                '--slot', str(slot), '--output', str(args.output), '--config', str(args.config),
                '--keys', str(args.keys), '--model', args.model,
                *(['--streaming'] if getattr(args, 'streaming', False) else [])]
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy())
            active[slot] = (ident, process)
        for slot, (ident, process) in list(active.items()):
            folder = args.output / 'cases' / ident
            stale = stalled_request(folder)
            if stale and slot not in watchdog_retries:
                with (folder / 'transport_recoveries.jsonl').open('a') as log:
                    log.write(json.dumps({'request': stale, 'reason': 'No complete response within wire deadline', 'time': time.time()}) + '\n')
                try:
                    os.kill(process.pid, 15)
                except ProcessLookupError:
                    pass
                watchdog_retries.add(slot)
            exit_code = process.poll()
            if exit_code is None:
                continue
            if slot in watchdog_retries:
                watchdog_retries.remove(slot)
                waiting.insert(0, ident)
                write(folder / 'status.json', {'status': 'retrying', 'slot': slot, 'reason': 'Resuming after stalled transport', 'updated': time.time()})
            elif exit_code and not ((folder / 'status.json').exists() and read(folder / 'status.json').get('data_review_hold')):
                write(folder / 'status.json', {'status': 'needs_attention', 'exit_code': exit_code, 'slot': slot})
            del active[slot]
        states = {str(i): read(args.output / 'cases' / str(i) / 'status.json') if (args.output / 'cases' / str(i) / 'status.json').exists() else {'status': 'queued'} for i in manifest['question_ids']}
        write(registry, {str(s): {'case': v[0], 'pid': v[1].pid} for s, v in active.items()})
        write(args.output / 'progress.json', {'phase': 'extraction', 'cases': states, 'active': {s: v[0] for s,v in active.items()}, 'updated': time.time()})
        time.sleep(5)
    if any(row['status'] == 'needs_attention' for row in states.values()):
        write(args.output / 'progress.json', {'phase': 'needs_attention', 'cases': states, 'updated': time.time()})
        return
    args.slot = getattr(args, 'slot', 0) % len(slots)
    write(args.output / 'progress.json', {'phase': 'merge', 'updated': time.time()})
    try:
        merge_worker(args)
    except Exception as error:
        write(args.output / 'progress.json', {'phase': 'needs_attention', 'stage': 'merge',
            'error_type': type(error).__name__, 'log': 'supervisor.log', 'updated': time.time()})
        raise
    write(args.output / 'progress.json', {'phase': 'finished', 'updated': time.time()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['supervise', 'case', 'merge', 'watch'], default='supervise')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--keys', type=Path, required=True)
    parser.add_argument('--model', default='deepseek-v4-pro')
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--case')
    parser.add_argument('--merge-batch-size', type=int, default=10,
        help='Case graphs per merge invocation; 0 combines all remaining cases')
    parser.add_argument('--streaming', action='store_true', help='Use streamed completions to keep long gateway responses active; preserve SSE/usage in wire audit')
    args = parser.parse_args()
    if args.merge_batch_size < 0:
        parser.error('--merge-batch-size must be nonnegative')
    args.output, args.config, args.keys = args.output.resolve(), args.config.resolve(), args.keys.resolve()
    os.environ['LANGSMITH_TRACING'] = os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    if args.mode == 'supervise':
        with (args.output / '.supervisor.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            write(args.output / 'supervisor.pid', os.getpid())
            supervise(args)
    elif args.mode == 'watch':
        with (args.output / '.watch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            watch(args)
    else:
        {'case': case_worker, 'merge': merge_worker}[args.mode](args)


if __name__ == '__main__':
    main()
