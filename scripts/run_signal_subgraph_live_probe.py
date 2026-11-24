"""One frozen real local task per database, independent of active merge jobs."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from html import escape
from pathlib import Path

from regress_signal_subgraph_inputs import (
    DEFAULT_OUTPUT,
    page,
    read,
    request,
    setup,
    write,
)
from resume_first10_current_graph import model_for
from run_resilient_training import transport_error_details

from cortex.capabilities.graph_merge.engine import (
    IncrementalGraphIntegrationEngine,
)
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.infrastructure.artifacts.graph_merge_trace import (
    FileSystemGraphMergeTraceSink,
)
from cortex.infrastructure.llm.key_slots import read_key_slots


def export(folder):
    path = folder / 'model_calls.jsonl'
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    starts = {event['call_id']: event for event in events if event.get('event_type') == 'model_start'}
    turns = []
    for event in events:
        if event.get('event_type') not in {'model_end', 'model_error'}:
            continue
        start = starts.get(event['call_id'], {})
        turns.append({'component': start.get('component'), 'input_messages': start.get('messages'),
                      'input_tool_names': start.get('tool_names'), 'output': event})
    public_requests = []
    for wire_path in sorted((folder / 'wire').glob('*_request.json')):
        body = read(wire_path).get('body', {})
        public_requests.append({'messages': body.get('messages', []), 'tools': body.get('tools', [])})
    write(folder / 'public_requests.json', public_requests)
    write(folder / 'agent_io.json', {'turns': turns, 'actual_public_requests': public_requests, 'feedback_events': [row for row in events
        if row.get('event_type') in {'tool_result', 'assessment_result', 'draft_compile_result', 'compile_result', 'draft_compiled'}]})
    result = read(folder / 'result.json') if (folder / 'result.json').exists() else {'status': 'running'}
    html = '<h1>Real controlled probe · ' + escape(folder.parent.name) + '</h1><p><a href="../index.html">Input scope regression</a></p><pre>' + escape(json.dumps(result, ensure_ascii=False, indent=2)) + '</pre>'
    for index, turn in enumerate(turns, 1):
        html += '<details><summary>Step ' + str(index) + ' · ' + escape(str(turn['component'])) + '</summary><h3>Complete input</h3><pre>' + escape(json.dumps(turn['input_messages'], ensure_ascii=False, indent=2)) + '</pre><h3>Tool names</h3><pre>' + escape(json.dumps(turn['input_tool_names'])) + '</pre><h3>Complete output</h3><pre>' + escape(json.dumps(turn['output'], ensure_ascii=False, indent=2)) + '</pre></details>'
    for index, actual in enumerate(public_requests, 1):
        html += '<details><summary>Actual request ' + str(index) + ' · full public tools and messages</summary><pre>' + escape(json.dumps(actual, ensure_ascii=False, indent=2)) + '</pre></details>'
    budgets = [row for row in events if row.get('event_type') == 'request_budget']
    html += '<h2>Request sizes</h2><pre>' + escape(json.dumps(budgets, ensure_ascii=False, indent=2)) + '</pre>'
    if (folder / 'agent_io.json').exists():
        html += '<p><a href="agent_io.json">Structured complete inputs/outputs</a> · <a href="before.json">Fixed graph</a> · <a href="after.json">Accepted result</a></p>'
    (folder / 'index.html').write_text(page(html))


def verify_completed_probe(folder, fixture):
    """Recompute saved receipt facts without models or checkpoint writes."""
    import sqlite3
    from types import SimpleNamespace

    from cortex.capabilities.graph_merge.reconciliation.receipts import is_accepted
    from cortex.capabilities.graph_merge.runtime.state import ContractVersions
    from cortex.schema import GraphState
    with sqlite3.connect((folder / 'workspace.sqlite').resolve().as_uri() + '?mode=ro', uri=True) as connection:
        saved = json.loads(connection.execute('select payload from merge_workspaces').fetchone()[0])
    context, _ = setup(fixture)
    engine = SimpleNamespace(ctx=context, current=GraphState.model_validate(saved['current']),
        contract_versions=ContractVersions.from_dict(saved['contract_versions']), signal_receipts=saved['signal_receipts'])
    reusable = [is_accepted(engine, row['check']) for row in saved['signal_receipts']]
    inputs = read(folder / 'public_requests.json')
    roles = {}
    for body in inputs:
        role = 'Writer' if any(t.get('function', {}).get('name') == 'edit_draft' for t in body['tools']) else 'Assessment'
        roles.setdefault(role, []).append(body)
    stable = {role: all(row['messages'][:2] == rows[0]['messages'][:2] for row in rows) for role, rows in roles.items()}
    assert all(reusable) and all(stable.values()) and all(len(body['messages']) == 3 for body in inputs)
    write(folder / 'tested_contract_versions.json', saved['contract_versions'])
    result = read(folder / 'result.json')
    result.update(same_policy_saved_receipts_reusable=reusable, whole_accepted_graph_unchanged=saved['current'] == fixture['base'],
        tested_contract_versions_file='tested_contract_versions.json', actual_accepted_draft=saved['task_state']['draft'],
        actual_independent_assessment=saved['task_state']['review'], three_messages_each_request=True, fixed_prefix_stable_within_each_role=stable)
    write(folder / 'result.json', result)
    return result


def run(output, short, slot, resume=None):
    folder = resume or output / short / ('live_' + datetime.now().strftime('%H%M%S'))
    if resume is None:
        folder.mkdir(parents=True)
    elif folder.resolve().parent != (output / short).resolve():
        raise ValueError('Resume folder must belong to the selected frozen fixture.')
    fixture = read(output / short / 'fixture.json')
    context, runtime = setup(fixture)
    before = context.task_before
    keys = read_key_slots(Path(__file__).resolve().parents[1] / 'configs/sql_training.keys')
    secrets = [key.api_key.get_secret_value() for key in keys]
    agent = IncrementalIntegrationAgent(model_for(folder, slot), max_calls=8, request_max_characters=500000,
        trace_sink=FileSystemGraphMergeTraceSink(folder / 'agents.jsonl'))
    agent.call_budget = {'used': 0, 'limit': 16}
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=3)
    engine.ctx, engine.current, engine.lineage = context, before, LineageIndex(context)
    engine.history, engine.reviewed_sources, engine._revision = [], {}, 0
    engine.identity_decision, engine.open_tasks = {}, []
    engine.workspace_path, engine._storage_revision = folder / 'workspace.sqlite', None
    engine.pending_relation_work = engine.pending_review = None
    task = context.active_task
    if resume is not None:
        from cortex.capabilities.graph_merge.runtime.state import (
            TaskState,
            build_contract_versions,
        )
        recovered = WorkspaceStore(engine.workspace_path).load(task.id)
        restored = TaskState.from_dict(recovered['task_state'])
        if restored.versions != build_contract_versions(context.schema) or canonical_hash(recovered['current']) != canonical_hash(before):
            raise ValueError('Probe checkpoint policy/base differs; explicit migration is required.')
        engine.task_state = restored
        engine.contract_versions = restored.versions
        engine._storage_revision = recovered['revision']
        engine._revision = recovered['accepted_revision']
        engine.history = list(recovered['history'])
    write(folder / 'before.json', before.model_dump(mode='json'))
    write(folder / 'task.json', fixture['task'])
    write(folder / 'first_request.json', request(runtime))
    result = {'database': short, 'status': 'running', 'task_id': task.id, 'model': 'deepseek-v4-pro',
              'anonymous_key_slot': slot, 'source_checkpoint_untouched': True, 'request_max_characters': 500000}
    write(folder / 'result.json', result)
    started = time.monotonic()
    try:
        engine._run_task(task, task.id)
        saved = WorkspaceStore(engine.workspace_path).load(task.id)
        write(folder / 'task_state.json', saved['task_state'])
        result.update(status='passed', independent_assessment=True,
            history=engine.history, accepted_hash=canonical_hash(engine.current),
            checkpoint_stage='committed', receipt_count=len(getattr(engine, 'signal_receipts', [])))
    except Exception as error:  # noqa: BLE001 - Preserve the controlled probe checkpoint and sanitized diagnostic.
        result.update(status='unfinished', errors=transport_error_details(error, secrets),
                      checkpoint_stage=getattr(getattr(engine, 'task_state', None), 'stage', None))
        if getattr(engine, 'task_state', None):
            write(folder / 'task_state.json', engine.task_state.to_dict())
    result.update(elapsed_seconds=round(time.monotonic() - started, 2), model_calls=agent.call_budget['used'])
    write(folder / 'after.json', engine.current.model_dump(mode='json'))
    write(folder / 'result.json', result)
    export(folder)
    if result['status'] == 'passed':
        result = verify_completed_probe(folder, fixture)
        export(folder)
    print(json.dumps({'folder': str(folder), 'status': result['status'], 'model_calls': result['model_calls']}, ensure_ascii=False), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', choices=['thrombosis', 'toxicology'], required=True)
    parser.add_argument('--slot', type=int, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--resume-folder', type=Path)
    args = parser.parse_args()
    for variable in ('NO_PROXY', 'no_proxy'):
        values = [x.strip() for x in os.environ.get(variable, '').split(',') if x.strip()]
        if 'ciyuanzhiyan.com' not in values:
            values.append('ciyuanzhiyan.com')
        os.environ[variable] = ','.join(values)
    run(args.output, args.database, args.slot, resume=args.resume_folder)
