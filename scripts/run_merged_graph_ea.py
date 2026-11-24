"""Checkpointed held-out EA: full schema, retrieved ontology memory, online guidance.

Gold SQL enters only Workflow's evaluator and the existing simulated-user agent.
Raw memory is captured before interaction; neither graph nor accepted merge is edited.
"""
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO/'src'), str(REPO/'baseline/src'), str(REPO/'scripts')]

import argparse
import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from html import escape
import json
import os
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from cortex.bootstrap.online_context import build_online_context_service, build_simulated_user_agent
from cortex.config import load_config
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.infrastructure.online_context import DatabaseDescriptionLoader, FileSystemGraphStateLoader
from cortex.infrastructure.workflow.locking import workflow_writer
from cortex.schema import export_inline_graph
from nl2sql_baseline.config import load_settings
from nl2sql_baseline.generation import SqlGenerator
from nl2sql_baseline.workflow import Workflow
from run_bird_dev_online_guided_baseline import _online_case, _guidance_case
from run_graph_guided_test import CallAudit, audit_http, write, read

ARMS = ('plain', 'retrieved_memory', 'guided')


def prediction_finished(prediction):
    return prediction is not None and not (prediction.get('error_stage') == 'generation' and prediction.get('error'))


def pending_arms(folder, arms=ARMS):
    return tuple(arm for arm in arms if not prediction_finished(
        read(folder/f'{arm}_prediction.json') if (folder/f'{arm}_prediction.json').exists() else None))


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def source_case_ids(graph):
    """Include alternative and edge sources, not just top-level node sources."""
    found = set()
    def visit(value):
        if isinstance(value, dict):
            for source in value.get('sources', []):
                for field in ('case_id', 'source_id'):
                    text = str(source.get(field) or '')
                    if text.startswith('bird-thrombosis_prediction-'):
                        found.add(text.rsplit('-', 1)[-1])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(graph)
    return found


def summarize(cases, root, arms=ARMS):
    rows = []
    for case in cases:
        folder = root/'cases'/str(case['source_id'])
        result = {'source_id': case['source_id']}
        for arm in arms:
            path = folder/f'{arm}_prediction.json'
            result[arm] = read(path) if path.exists() else None
        path = folder/'online.json'
        online = read(path) if path.exists() else {}
        result['online_status'] = online.get('status')
        result['online_error'] = online.get('error')
        rows.append(result)
    totals = {}
    for arm in arms:
        completed = [r[arm] for r in rows if prediction_finished(r[arm])]
        correct = sum(r.get('evaluation', {}).get('exec') == 1 for r in completed)
        totals[arm] = {'total': len(cases), 'finished': len(completed), 'correct': correct,
                       'ea_fixed_denominator': correct / len(cases),
                       'errors': sum(bool(r.get('error')) for r in completed),
                       'provisional': len(completed) != len(cases)}
    return rows, totals


def paired_statistics(rows):
    """Compare the same finished cases, excluding interrupted generation only."""
    def correct(prediction):
        return prediction.get('evaluation', {}).get('exec') == 1
    result = {}
    for arm in ('retrieved_memory', 'guided'):
        pairs = [row for row in rows if prediction_finished(row.get('plain')) and prediction_finished(row.get(arm))]
        n = len(pairs)
        base = sum(correct(row['plain']) for row in pairs)
        target = sum(correct(row[arm]) for row in pairs)
        result[arm] = {'paired_cases': n, 'baseline_correct': base, 'arm_correct': target,
            'baseline_ea': base/n if n else None, 'arm_ea': target/n if n else None,
            'gains': [row['source_id'] for row in pairs if not correct(row['plain']) and correct(row[arm])],
            'regressions': [row['source_id'] for row in pairs if correct(row['plain']) and not correct(row[arm])],
            'provisional': True}
    return result


def sql_arm(case, arm, text, folder, model, database_root):
    """Resume generation interruptions only; never retry an EA mismatch."""
    saved = folder/f'{arm}_prediction.json'
    attempts = []
    if saved.exists():
        previous = read(saved)
        if previous.get('error_stage') != 'generation' or not previous.get('error'):
            return previous
        attempts = previous.get('sql_attempts') or []
        if len(attempts) >= 3 or any(not a.get('error') for a in attempts):
            return previous
        write(folder/'generation_history'/f'{arm}_{digest(saved)}.json', previous)
    cases_path = folder/'baseline_cases.json'
    # The standalone baseline fixes row-set equality globally and deliberately
    # rejects per-record evaluation controls from normalized online inputs.
    write(cases_path, [{key: value for key, value in case.items() if key != 'evaluation'}])
    cfg_path = folder/f'{arm}.yaml'
    if not cfg_path.exists():
        import yaml
        cfg_path.write_text(yaml.safe_dump({
            'dataset': {'type': 'bird'}, 'test_set_path': str(cases_path),
            'database': {'root': str(database_root)}, 'schema_linking': {'type': 'full_schema'},
            'memory': {'type': 'none'},
            'llm': {'model': 'deepseek-v4-pro', 'api_key': 'injected-model', 'timeout': 900},
            'evaluation': {'top_k': 5, 'sql_timeout': 30}}, sort_keys=False))
    settings = load_settings(cfg_path, env_file=REPO/'.env')
    if arm != 'plain':
        if text is None:
            raise ValueError('Memory unavailable; do not silently run a plain arm under a memory label')
        memory_dir = folder/(arm+'_input')
        write(memory_dir/f"{case['source_id']}.json", {
            'source_id': case['source_id'], 'question': case['question'],
            'downstream_guidance': {'agent_handoff': {'guidance_content': text}}})
        settings = replace(settings, memory_type='downstream_guidance' if arm == 'guided' else 'reference_guidance',
                           guidance_dir=memory_dir)
    generator = SqlGenerator(model='deepseek-v4-pro', api_key='injected-model', base_url=None,
                             temperature=0, timeout=900, llm=model)
    output = folder/f'{arm}_attempt_{len(list(folder.glob(arm+"_attempt_*")))+1}'
    kwargs = {'resume_sql_attempts': {str(case['source_id']): attempts}} if attempts else {}
    run, _ = Workflow(settings, generator=generator).evaluate(output=output, **kwargs)
    prediction = json.loads((run/'predictions.jsonl').read_text().splitlines()[0])
    write(saved, prediction)
    return prediction


class Evaluation:
    def __init__(self, root):
        self.root = root.resolve()
        self.protocol = read(root/'protocol.json')
        self.arms = tuple(self.protocol.get('arms', ARMS))
        if not self.arms or any(arm not in ARMS for arm in self.arms):
            raise ValueError('Select at least one recognized evaluation arm')
        self.cases = [json.loads(line) for line in (root/'test_cases.jsonl').read_text().splitlines()]
        assert digest(root/'merged_state.json') == self.protocol['graph_sha256']
        assert digest(root/'test_cases.jsonl') == self.protocol['cases_sha256']
        assert all(digest(REPO/p) == h for p, h in self.protocol['runtime_manifest'].items())
        self.graph = FileSystemGraphStateLoader().load(root/'merged_state.json')
        self.cfg = load_config(root/'config.yaml')
        self.slots = read_key_slots(self.protocol['keys_file'])
        self.context = DatabaseDescriptionLoader().load(Path(self.protocol['database_root'])/
            'thrombosis_prediction/database_description')
        self.current = {}
        self.status = 'running'
        self.imported = read(root/'imported_results.json') if (root/'imported_results.json').exists() else {}

    def publish(self):
        rows, totals = summarize(self.cases, self.root, self.arms)
        for row in rows:
            imported = self.imported.get(row['source_id'], {})
            row['result_sources'] = {
                arm: imported.get(arm, {'kind': 'current_runtime', 'path': str(self.root/'cases'/row['source_id']/f'{arm}_prediction.json')})
                for arm in self.arms if row.get(arm) is not None}
            if row['online_status'] is None and imported.get('online_summary'):
                row['online_status'] = imported['online_summary']['status']
                row['online_status_source'] = imported['online_summary']['path']
        record = {'updated_at': now(), 'status': self.status, 'pid': os.getpid(),
            'total': len(rows), 'arms': totals, 'active': self.current,
            'paired_comparisons': paired_statistics(rows) if 'plain' in self.arms else {},
            'online_status_counts': dict(Counter(r['online_status'] or 'pending' for r in rows))}
        if self.protocol.get('continuation'):
            record['continuation'] = self.protocol['continuation']
        write(self.root/'status.json', record)
        write(self.root/'summary.json', record)
        path = self.root/'predictions.jsonl.tmp'
        path.write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows))
        path.replace(self.root/'predictions.jsonl')
        body = '<h1>thrombosis_prediction · Online / EA</h1><p>'+escape(now())+'</p>'
        if self.protocol.get('mode') == 'guided_error_regression':
            body = '<h1>thrombosis_prediction · Guided error-case rerun</h1><p>'+escape(now())+'</p>'
            body += '<p><strong>Only previously incorrect guided cases are rerun. This is a selected development regression, not a full held-out EA estimate. The original full run remains paused and unchanged. No additional definition filter is used; final retrieval caps are 30 semantic / 50 physical nodes.</strong></p>'
        if self.protocol.get('continuation'):
            body += '<p><strong>Continuation: completed SQL predictions are preserved. All seven guided rerun results, including the four remaining failures, are imported uniformly; cases 1155, 1156 and 1160 now pass. Only unfinished arms are generated. New online requests use 30 semantic / 50 physical caps and no added definition filter. Historical predictions retain their original runtime provenance; this combined report is not a fresh uniform-runtime blind evaluation.</strong></p>'
            body += '<p><a href="imported_results.json">Per-arm source records</a> · <a href="continuation_plan.json">Preserved results and remaining work</a></p>'
        body += '<p>Frozen final graph: 81 training cases; held-out evaluation only. Full-schema SQL generator shared across arms. Retrieved memory is captured before simulated-user interaction; guidance uses the existing simulated user. EA ignores row order and duplicates. All fixed test cases remain in the denominator; live numbers are provisional.</p>'
        body += '<p><a href="protocol.json">Protocol</a> · <a href="summary.json">Summary</a> · <a href="predictions.jsonl">Predictions</a></p>'
        body += '<table><tr><th>Arm</th><th>Finished</th><th>Correct</th><th>EA (all cases)</th></tr>'
        for arm, row in totals.items():
            body += f'<tr><td>{arm}</td><td>{row["finished"]}/{row["total"]}</td><td>{row["correct"]}</td><td>{row["ea_fixed_denominator"]:.2%}</td></tr>'
        body += '</table><h2>Same-case comparisons (provisional)</h2><pre>'+escape(json.dumps(record['paired_comparisons'], ensure_ascii=False, indent=2))+'</pre><pre>'+escape(json.dumps(record, ensure_ascii=False, indent=2))+'</pre>'
        for case in self.cases:
            sid = str(case['source_id']); folder = self.root/'cases'/sid
            row = next(row for row in rows if row['source_id'] == sid)
            outcomes = ', '.join(arm+': '+('✓' if row.get(arm,{}).get('evaluation',{}).get('exec') == 1 else '✗')
                if prediction_finished(row.get(arm)) else arm+': pending' for arm in self.arms)
            body += '<details><summary>'+sid+' · '+escape(outcomes)+' · '+escape(case['question'])+'</summary>'
            for label in ('online.json', 'retrieved_memory.json', 'stage_traces.json', *[a+'_prediction.json' for a in self.arms], 'calls.jsonl'):
                if (folder/label).exists():
                    body += '<p><a href="cases/'+sid+'/'+label+'">'+label+'</a></p>'
            imported = self.imported.get(sid, {})
            for arm in self.arms:
                if arm in imported:
                    body += '<p>Imported '+arm+': <a href="'+escape(os.path.relpath(imported[arm]['path'], self.root), quote=True)+'">original result and run</a></p>'
            body += '</details>'
        html = '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>Thrombosis Online EA</title><style>body{font:16px/1.6 system-ui;max-width:1100px;margin:30px auto;padding:15px}td,th{border:1px solid #ddd;padding:8px}table{border-collapse:collapse}pre{white-space:pre-wrap}details{padding:10px;border-bottom:1px solid #ddd}</style>'+body
        tmp = self.root/'index.html.tmp'; tmp.write_text(html); tmp.replace(self.root/'index.html')

    async def one(self, case, key_slot):
        sid = str(case['source_id']); folder = self.root/'cases'/sid
        folder.mkdir(parents=True, exist_ok=True)
        needed = pending_arms(folder, self.arms)
        if not needed:
            return
        llm = self.cfg.llm.model_copy(update={'api_key': self.slots[key_slot].api_key})
        cfg = self.cfg.model_copy(update={'llm': llm, 'services': self.cfg.services.model_copy(update={
            'online_context': self.cfg.services.online_context.model_copy(update={'checkpoint_path': folder/'online.sqlite'}),
            'retrieve_db': self.cfg.services.retrieve_db.model_copy(update={'workspace': folder/'retrieval'})})})
        model = create_chat_model(cfg.llm, profile='online_context')
        model.callbacks = [CallAudit(folder)]; audit_http(model, folder/'wire')
        def guard(request):
            body = json.loads(request.content)
            assert body.get('model') == 'deepseek-v4-pro'
            assert body.get('thinking', {}).get('type') == 'disabled'
            assert body.get('reasoning_effort') == 'none'
            assert request.headers['authorization'] == 'Bearer '+self.slots[key_slot].api_key.get_secret_value()
        async def async_guard(request): guard(request)
        model.root_client._client.event_hooks['request'].insert(0, guard)
        model.root_async_client._client.event_hooks['request'].insert(0, async_guard)
        traces = []
        def trace(stage, payload):
            traces.append({'stage': stage, 'payload': payload})
            self.current[sid] = {'stage': stage, 'key_slot': key_slot}
        def capture(result):
            write(folder/'retrieval_result.json', result.public_payload())
            memory = export_inline_graph(result.retrieved_graph_state) if result.retrieved_graph_state else {'nodes': [], 'edges': []}
            # This callback executes before Discovery/Filter and simulated-user answers.
            from cortex.capabilities.online_context.discovery.evidence import _analysis_graph_payload
            presentation = _analysis_graph_payload(result.retrieved_graph_state) if result.retrieved_graph_state else {'graph': memory}
            text = json.dumps(presentation, ensure_ascii=False, separators=(',', ':'))
            write(folder/'retrieved_memory.json', {'phase': 'pre_interaction_retrieval',
                'memory': memory, 'text': text, 'presentation': 'existing online analysis view; source bookkeeping and redundant physical catalog edges omitted; full graph retained separately',
                'sha256': sha256(text.encode()).hexdigest()})
        try:
            self.current[sid] = {'stage': 'online', 'key_slot': key_slot}; self.publish()
            if 'plain' in needed:
                self.current[sid]['stage'] = 'plain_sql'
                await asyncio.to_thread(sql_arm, case, 'plain', None, folder, model, Path(self.protocol['database_root']))
            if not {'retrieved_memory', 'guided'} & set(needed):
                return
            cached = folder/'online.json'; record = read(cached) if cached.exists() else None
            if not record or record.get('status') == 'error':
                worker = SimpleNamespace(service=build_online_context_service(config=cfg, llm=model,
                    embeddings=None, trace_sink=trace, on_retrieved=capture),
                    simulator=build_simulated_user_agent(config=cfg, llm=model), traces=traces)
                run_id = sha256((self.protocol['graph_sha256']+'|'+sid).encode()).hexdigest()
                self.current[sid] = {'stage': 'online', 'key_slot': key_slot}
                record = await _online_case(worker, graph_state=self.graph, column_context=self.context,
                                          case=case, run_id=run_id)
                write(folder/f'online_attempt_{len(list(folder.glob("online_attempt_*.json")))+1}.json', record)
                write(cached, record)
            if 'retrieved_memory' in needed and (folder/'retrieved_memory.json').exists():
                self.current[sid] = {'stage': 'retrieved_memory_sql', 'key_slot': key_slot}
                await asyncio.to_thread(sql_arm, case, 'retrieved_memory', read(folder/'retrieved_memory.json')['text'],
                                        folder, model, Path(self.protocol['database_root']))
            if 'guided' in needed and record.get('guidance_content'):
                write(folder/'guidance.json', _guidance_case(case, record))
                self.current[sid] = {'stage': 'guided_sql', 'key_slot': key_slot}
                await asyncio.to_thread(sql_arm, case, 'guided', record['guidance_content'],
                                        folder, model, Path(self.protocol['database_root']))
        except Exception as error:
            from cortex.infrastructure.workflow.diagnostics import classify_workflow_error
            write(folder/'failure.json', classify_workflow_error(error))
        finally:
            write(folder/'stage_traces.json', traces)
            self.current.pop(sid, None); self.publish()
            await model.root_async_client.close(); model.root_client.close()

    async def run(self):
        queue = asyncio.Queue()
        for case in self.cases:
            if pending_arms(self.root/'cases'/str(case['source_id']), self.arms):
                await queue.put(case)
        async def worker(slot):
            while not queue.empty():
                await self.one(queue.get_nowait(), slot)
        async def heartbeat():
            while True:
                self.publish(); await asyncio.sleep(20)
        task = asyncio.create_task(heartbeat())
        try:
            await asyncio.gather(*(worker(slot) for slot in self.protocol['key_slots']))
            _, totals = summarize(self.cases, self.root, self.arms)
            self.status = 'complete' if all(not r['provisional'] for r in totals.values()) else 'needs_attention'
            self.publish()
        finally:
            task.cancel()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume-paused', action='store_true', help='Explicitly acknowledge a user-paused run; use only after fixes are validated.')
    args = parser.parse_args()
    if (args.output/'user_pause.json').exists() and not args.resume_paused:
        parser.error('Full evaluation is user-paused; preserve results and validate fixes before explicit resumption.')
    load_dotenv(REPO/'.env', override=False)
    os.environ['LANGSMITH_TRACING'] = os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    with workflow_writer(args.output/'worker.lock'):
        asyncio.run(Evaluation(args.output).run())
