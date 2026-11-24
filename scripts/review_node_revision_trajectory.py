"""Review a finished real repair trajectory without overruling Assessment.

A deletion proposal may compile correctly and be rejected on semantic grounds.
The workflow test succeeds when that feedback leads to a genuinely reviewed,
accepted grouping correction; this never labels the deletion semantically valid.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from cortex.capabilities.graph_merge.graph.validation import validate_local_transition
from cortex.capabilities.graph_merge.runtime.state import TaskState
from cortex.schema import GraphState, load_root_schema

OWNER = 'input::input::n18'
SEPARATE = 'input::input::n202'
EDGE = 'input::input::e238'


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def packet(messages):
    result = {}
    for message in messages:
        text = message.get('content')
        if not isinstance(text, str):
            continue
        if '\n{' in text:
            text = text[text.find('\n{') + 1:]
        try:
            body = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict):
            result.update(body)
    return result


def outputs(event):
    message = event.get('message') or {}
    for call in message.get('tool_calls', []):
        if call.get('name') in {'Review', 'submit_patch'} and isinstance(call.get('args'), dict):
            yield call['name'], call['args']
    if not message.get('tool_calls'):
        try:
            value = json.loads(message.get('content', ''))
        except (ValueError, TypeError):
            return
        if isinstance(value, dict):
            if 'verdict' in value:
                yield 'Review', value
            elif 'integrate_groups' in value:
                yield 'submit_patch', value


def checkpoint(path, run_id):
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as connection:
        row = connection.execute('SELECT revision,payload FROM merge_workspaces WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        raise ValueError('Missing exact test checkpoint')
    value = json.loads(row[1])
    if value['revision'] != row[0]:
        raise ValueError('Checkpoint revision mismatch')
    TaskState.from_dict(value['task_state'])
    return value


def review(folder):
    folder = Path(folder).resolve()
    if folder.name != 'remove_relation':
        raise ValueError('This adjudication applies only to the controlled removal-proposal test')
    if not (folder.parent / 'results.json').exists() or not (folder / 'result.json').exists():
        raise ValueError('Wait for the original two-path real test runner to finish')
    artifact_hashes = {name: digest(folder / name) for name in
                       ('before.json', 'after.json', 'model_calls.jsonl', 'workspace.sqlite')}
    prior = folder / 'trajectory_review.json'
    if prior.exists():
        result = read(prior)
        if result['artifact_hashes'] != artifact_hashes:
            raise ValueError('Previously reviewed trajectory artifacts changed')
        return result
    saved = checkpoint(folder / 'workspace.sqlite', 'real-node-revision:remove_relation')
    task = saved['task_state']
    if task['stage'] != 'committed' or task['review']['verdict'] != 'accept':
        raise ValueError('Final local transaction has not been independently accepted and committed')
    draft = task['draft']
    if SEPARATE not in draft.get('keep_separate_ids', []) or draft.get('relation_removals'):
        raise ValueError('Final draft is not the required source-preserving grouping correction')
    before = GraphState.model_validate(read(folder / 'before.json'))
    after = GraphState.model_validate(read(folder / 'after.json'))
    if after.model_dump(mode='json') != saved['current']:
        raise ValueError('Displayed result differs from accepted checkpoint graph')
    validate_local_transition(load_root_schema(), before, after)
    old_nodes, nodes = ({n.id: n for n in state.graph.nodes} for state in (before, after))
    old_edges, edges = ({e.id: e for e in state.graph.edges} for state in (before, after))
    if nodes.get(SEPARATE) != old_nodes[SEPARATE]:
        raise ValueError('Standalone member changed or disappeared')
    actual, original = edges[EDGE], old_edges[EDGE]
    if (actual.from_node_id != OWNER or actual.to_node_id != SEPARATE
            or actual.properties != original.properties or actual.sources != original.sources):
        raise ValueError('Final relation meaning, direction, scope or exact sources changed')
    accepted_ids = read(folder / 'source_scope.json')['accepted_prefix_tasks']
    if [h['task_id'] for h in saved['history'][:-1]] != accepted_ids:
        raise ValueError('Accepted task prefix was replayed or changed')
    if len(saved['history']) != len(accepted_ids) + 1:
        raise ValueError('Unexpected extra local commits')
    events = []
    for line in (folder / 'model_calls.jsonl').read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            raise ValueError('Completed trace contains an incomplete record') from None
    requests, removal_calls, accepted_calls = {}, set(), set()
    deletion_proposals, rejected_reviews, accepted_reviews = [], [], []
    for index, event in enumerate(events):
        if event.get('event_type') == 'model_start':
            requests[event['call_id']] = packet(event.get('messages', []))
            if event.get('component') != 'integration_node_assess':
                continue
            supplied = requests[event['call_id']]
            candidate = supplied.get('candidate') or {}
            relations = {e['id']: e for e in candidate.get('relations', [])}
            removals = supplied.get('relation_removals', [])
            if any(r.get('edge_id') == EDGE for r in removals) and EDGE not in relations:
                removal_calls.add(event['call_id'])
            if (SEPARATE in {n['id'] for n in candidate.get('nodes', [])}
                    and EDGE in relations and relations[EDGE]['from_node_id'] == OWNER
                    and relations[EDGE]['to_node_id'] == SEPARATE):
                accepted_calls.add(event['call_id'])
        if event.get('event_type') != 'model_end':
            continue
        for name, output in outputs(event):
            if name == 'submit_patch' and any(r.get('edge_id') == EDGE for r in output.get('relation_removals', [])):
                deletion_proposals.append({'trace_index': index, 'call_id': event['call_id'], 'output': output})
            if name != 'Review' or event.get('component') != 'integration_node_assess':
                continue
            if output.get('verdict') == 'revise' and event['call_id'] in removal_calls:
                issues = output.get('issues', [])
                if not any(EDGE in json.dumps(issue, ensure_ascii=False) and SEPARATE in json.dumps(issue, ensure_ascii=False)
                           for issue in issues):
                    raise ValueError('Removal rejection does not explain the actual relation/grouping issue')
                rejected_reviews.append({'trace_index': index, 'call_id': event['call_id'], 'output': output})
            if output.get('verdict') == 'accept' and event['call_id'] in accepted_calls:
                accepted_reviews.append({'trace_index': index, 'call_id': event['call_id'], 'output': output})
    if not deletion_proposals or not rejected_reviews or not accepted_reviews:
        raise ValueError('Missing real compiled deletion review, semantic rejection, or final grouping acceptance')
    if not deletion_proposals[0]['trace_index'] < rejected_reviews[0]['trace_index'] < accepted_reviews[-1]['trace_index']:
        raise ValueError('Real model trajectory is not in the claimed chronological order')
    result = {'status': 'passed', 'tested_path': 'remove_relation', 'final_resolution': 'keep_separate',
        'validation_scope': 'The removal proposal compiled and was independently rejected; feedback led to an accepted grouping correction.',
        'removal_proposal_compiled_and_reviewed': True,
        'removal_proposal_accepted_by_assessment': False,
        'independent_real_assessment': True, 'accepted_prefix_preserved': True,
        'standalone_node_unchanged': True, 'original_relation_and_sources_retained': True,
        'checkpoint_stage': task['stage'], 'artifact_hashes': artifact_hashes,
        'real_model_calls': sum(e.get('event_type') == 'model_start' for e in events),
        'deletion_proposal': deletion_proposals[0], 'removal_rejection': rejected_reviews[0],
        'final_acceptance': accepted_reviews[-1]}
    history = folder / 'trajectory_history'
    history.mkdir()
    write(history / 'original_runner_result.json', read(folder / 'result.json'))
    write(history / 'original_runner_summary.json', read(folder.parent / 'results.json'))
    write(prior, result)
    updated = {k: v for k, v in result.items() if k not in
               {'artifact_hashes', 'deletion_proposal', 'removal_rejection', 'final_acceptance'}}
    updated.update(path='remove_relation', final_draft=draft, final_review=task['review'],
                   original_runner_result='trajectory_history/original_runner_result.json')
    write(folder / 'result.json', updated)
    summary = read(folder.parent / 'results.json')
    if isinstance(summary, list):
        summary = [updated if row.get('path') == 'remove_relation' else row for row in summary]
        write(folder.parent / 'results.json', summary)
    if digest(folder / 'workspace.sqlite') != artifact_hashes['workspace.sqlite']:
        raise RuntimeError('Read-only adjudication changed the checkpoint')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    arguments = parser.parse_args()
    outcome = review(arguments.folder)
    print(json.dumps({key: outcome[key] for key in ('status', 'tested_path', 'final_resolution',
                     'removal_proposal_accepted_by_assessment', 'real_model_calls')}, ensure_ascii=False))
