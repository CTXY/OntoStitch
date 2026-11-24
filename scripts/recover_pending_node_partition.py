"""Replan one rejected multi-identity proposal into ordinary single-group tasks.

Never apply the rejected graph draft. Each proposed subgroup gets a fresh Writer
and independent Assessment under the existing one-group public contract.
"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from cortex.capabilities.graph_merge.graph.publication import identity_map
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.engine import identity_task_projection
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.graph.edit_contract import IdentityDecision
from cortex.capabilities.graph_merge.consolidation.compiler import IdentityIntegration
from cortex.capabilities.graph_merge.runtime.state import ContractVersions, TaskState, canonical_hash
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.schema import GraphState, load_root_schema


def partition_plan(payload):
    """Validate exact source coverage; preserve numbering of all existing tasks."""
    result = deepcopy(payload)
    state = result.get('task_state') or {}
    if state.get('phase') != 'consolidate' or not state.get('task_id', '').startswith('content-'):
        raise ValueError('Recovery requires an unfinished node consolidation task')
    ident = state['task_id']
    if any(row.get('id') != ident for row in result.get('open_tasks', [])):
        raise ValueError('Recovery cannot discard other queued tasks')
    index = int(ident.removeprefix('content-'))
    if any(row['task_id'] == ident for row in result['history']):
        raise ValueError('An accepted task cannot be replanned')
    raw = state.get('draft') or {}
    if raw.get('failure') or raw.get('keep_separate_ids') or raw.get('relation_removals') or raw.get('relation_followups'):
        raise ValueError('Only a complete partition proposal without extra operations is supported')
    parts = [IdentityIntegration.model_validate(row) for row in raw.get('integrate_groups', [])]
    if len(parts) < 2:
        raise ValueError('The rejected draft does not propose multiple subgroups')
    original = result['content_plan'][index]
    if len(original) != 1:
        raise ValueError('Recovery requires one fixed original group')
    original_members = set(original[0]['members'])
    used = set()
    groups = []
    for part in parts:
        members = set(part.member_ids)
        if part.owner_id not in members or len(members) != len(part.member_ids) or used & members or not members <= original_members:
            raise ValueError('Proposed subgroups overlap or contain outside members')
        used.update(members)
        if part.content is None:
            raise ValueError('This bounded recovery requires normal complete subgroup descriptions')
        groups.append({'members': list(part.member_ids), 'meaning': part.content.properties.get('definition') or part.content.description or part.content.name,
                       'reason': part.explanation})
    if used != original_members:
        raise ValueError('Proposed subgroups must cover every original member exactly once')
    matches = [i for i, group in enumerate(result['identity']['groups']) if set(group['members']) == original_members]
    if len(matches) != 1:
        raise ValueError('Original identity group is not uniquely bound')
    result['identity']['groups'][matches[0]:matches[0] + 1] = groups
    result['content_plan'][index] = [groups[0]]
    appended_ids = []
    for group in groups[1:]:
        appended_ids.append('content-' + str(len(result['content_plan'])))
        result['content_plan'].append([group])
    return result, {'reopened_task': ident, 'original_group': original[0], 'proposed_groups': groups,
                    'appended_task_ids': appended_ids, 'rejected_draft_applied': False}


def repair(source, destination, *, run_id):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or destination == source:
        raise ValueError('Use a separate new recovery checkpoint')
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as connection:
        original = json.loads(connection.execute('select payload from merge_workspaces where run_id=?', (run_id,)).fetchone()[0])
    revised, audit = partition_plan(original)
    folder = source.parent
    ctx = IntegrationContext(GraphState.model_validate_json((folder / 'before.json').read_text()),
        GraphState.model_validate_json((folder / 'incoming.json').read_text()), load_root_schema())
    ctx.read_ids.update(revised['identity_read_ids'])
    aliases = identity_map(ctx, IdentityDecision.model_validate(revised['identity']))
    current = GraphState.model_validate(revised['current'])
    lineage = LineageIndex.restore(ctx, revised['lineage'])
    group = revised['content_plan'][int(audit['reopened_task'].removeprefix('content-'))][0]
    local_aliases, targets, feedback = identity_task_projection(current, {key: aliases[key] for key in group['members']}, lineage)
    revised['open_tasks'] = [{'id': audit['reopened_task'], 'phase': 'consolidate', 'targets': sorted(targets),
        'groups': [group], 'checks': [], 'feedback': feedback, 'aliases': local_aliases}]
    task = TaskState.capture(current, task_id=audit['reopened_task'], phase='consolidate',
                             versions=ContractVersions.from_dict(revised['contract_versions'])).to_dict()
    task['recovery'] = 'needs_rebuild'
    revised['task_state'] = TaskState.from_dict(task).to_dict()
    revised['pending_review'] = revised['pending_relation_work'] = None
    revised['final'] = False
    for key in ('current', 'history', 'lineage', 'signal_receipts', 'signal_scope_migrations', 'reviewed_sources', 'input_fingerprint', 'accepted_revision'):
        if revised.get(key) != original.get(key):
            raise ValueError('Recovery changed accepted progress: ' + key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    revised.pop('revision', None)
    store = WorkspaceStore(destination)
    store.save(run_id, revised)
    saved = store.load(run_id)
    audit.update(source_checkpoint=str(source), source_storage_revision=original['revision'],
        accepted_tasks_preserved=len(original['history']), accepted_graph_hash=canonical_hash(original['current']),
        model_contracts_unchanged=True, original_identity_hash=canonical_hash(original['identity']),
        revised_identity_hash=canonical_hash(saved['identity']), all_original_members_covered=True)
    (destination.parent / 'partition_recovery.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    return audit
