"""Repair a rejected JSON proposal without applying fragments to the graph."""
from copy import deepcopy
from typing import Any, Literal

from pydantic import Field, ValidationError

from cortex.capabilities.graph_merge.integration.models import Model
from cortex.capabilities.graph_merge.integration.relation_edits import RelationPatch


class Correction(Model):
    op: Literal['add', 'replace', 'remove', 'move']
    path: str = Field(description='JSON Pointer into the rejected patch, e.g. /edge_updates/0/add_evidence. Use /create_edges for new edges.')
    value: Any = None
    from_path: str | None = Field(default=None, description='For move only: the existing JSON Pointer to rename or relocate, e.g. /new_edges.')


def _location(document, path):
    if not path.startswith('/'):
        raise ValueError('Correction path must be a non-root JSON Pointer beginning with /')
    parts = [p.replace('~1', '/').replace('~0', '~') for p in path[1:].split('/')]
    parent = document
    for part in parts[:-1]:
        if isinstance(parent, list):
            if not part.isdigit() or not 0 <= int(part) < len(parent):
                raise ValueError(f'Correction parent does not exist: {path}')
            parent = parent[int(part)]
        elif isinstance(parent, dict) and part in parent:
            parent = parent[part]
        else:
            raise ValueError(f'Correction parent does not exist: {path}')
    key = parts[-1]
    if isinstance(parent, list):
        if key == '-':
            key = len(parent)
        elif key.isdigit():
            key = int(key)
        else:
            raise ValueError(f'Correction list index must be nonnegative: {path}')
    if not isinstance(parent, (dict, list)):
        raise ValueError(f'Correction parent is not an object/list: {path}')
    return parent, key


def _remove(document, path):
    parent, key = _location(document, path)
    if isinstance(parent, list):
        if key >= len(parent):
            raise ValueError(f'Correction target does not exist: {path}')
    elif key not in parent:
        raise ValueError(f'Correction target does not exist: {path}')
    return parent.pop(key)


def _write(document, path, value, *, replace=False):
    parent, key = _location(document, path)
    if isinstance(parent, list):
        if key > len(parent) or (replace and key == len(parent)):
            raise ValueError(f'Correction target does not exist: {path}')
        if replace:
            parent[key] = value
        else:
            parent.insert(key, value)
    else:
        if replace and key not in parent:
            raise ValueError(f'Correction target does not exist: {path}')
        parent[key] = value


def field_contracts(error):
    """Resolve only faulty parent schemas; do not repeat input or full schema."""
    if not isinstance(error, ValidationError):
        return []
    schema = (Correction if error.title == "Correction" else RelationPatch).model_json_schema()
    definitions = schema.get('$defs', {})
    result, seen = [], set()

    def resolve(value):
        while '$ref' in value:
            value = definitions.get(value['$ref'].split('/')[-1], {})
        return value

    for issue in error.errors(include_input=False, include_url=False):
        parts = list(issue['loc'])
        if parts and parts[0] == 'patch':
            parts.pop(0)
        current = schema
        for part in parts[:-1]:
            current = resolve(current)
            current = current.get('items', {}) if isinstance(part, int) else current.get('properties', {}).get(part, {})
        current = resolve(current)
        fields = current.get('properties', {})
        if not fields:
            continue
        path = ('/' + '/'.join(str(p).replace('~','~0').replace('/','~1') for p in parts[:-1])) if parts[:-1] else ''
        if path not in seen:
            seen.add(path)
            result.append({'parent_path': path, 'allowed_fields': list(fields), 'required_fields': current.get('required', [])})
    return result


class ProposalRepair:
    """One ephemeral draft per Writer episode; never a partially accepted graph."""
    def __init__(self):
        self.draft = None
        self.revision = 0

    def prepare(self, arguments):
        if set(arguments) - {'patch', 'corrections'}:
            raise ValueError('submit_changes accepts exactly one of patch or corrections')
        patch, corrections = arguments.get('patch'), arguments.get('corrections')
        if (patch is None) == (not corrections):
            raise ValueError('Provide exactly one: a complete patch, or nonempty corrections to the latest rejected patch')
        if corrections:
            if self.draft is None:
                raise ValueError('No rejected patch exists in this episode; submit a complete patch first')
            value = deepcopy(self.draft)
            for raw in corrections:
                correction = Correction.model_validate(raw)
                if correction.op in ('add', 'replace') and 'value' not in raw:
                    raise ValueError('add/replace correction requires value, including explicit null')
                if correction.op == 'move':
                    if not correction.from_path or correction.path.startswith(correction.from_path + '/'):
                        raise ValueError('move requires an existing from_path and cannot move into itself')
                    moved = _remove(value, correction.from_path)
                    _write(value, correction.path, moved)
                elif correction.op == 'remove':
                    _remove(value, correction.path)
                else:
                    _write(value, correction.path, deepcopy(correction.value), replace=correction.op == 'replace')
        else:
            if not isinstance(patch, dict):
                raise ValueError('patch must be a JSON object')
            value = deepcopy(patch)
        # Save raw content even if schema/graph validation below will reject it.
        # Failed correction syntax above leaves the previous draft intact.
        self.draft = value
        self.revision += 1
        return value

    def feedback(self, error, context):
        current = context.active_batch.preview or context.active_batch.seed
        existing = {o.id for o in (*current.graph.nodes, *current.graph.edges)}
        ids = set()
        if self.draft:
            for values in self.draft.values():
                if isinstance(values, list):
                    ids.update(v['id'] for v in values if isinstance(v, dict) and isinstance(v.get('id'), str))
        absent = sorted(ids - existing)
        missing_updates = []
        for field, create in (("node_updates", "create_nodes"), ("edge_updates", "create_edges")):
            for i, item in enumerate((self.draft or {}).get(field, [])):
                if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'] not in existing:
                    missing_updates.append({'path': f'/{field}/{i}', 'id': item['id'], 'exists_in_candidate': False,
                        'repair': f'Use an existing current ID, or {create} with all required creation fields if this is a new assertion. A rejected proposal never creates its IDs.'})
        source_repair = None
        if hasattr(error, 'missing_sources'):
            source_repair = {'before': [r.model_dump(exclude_none=True) for r in error.before],
                             'after': [r.model_dump(exclude_none=True) for r in error.after],
                             'missing_evidence_ids': [e.id for e in context.evidence if e.source in error.missing_sources],
                             'successor_fields': 'Existing successors: node_updates/edge_updates.add_evidence; new successors: create_nodes/create_edges.evidence_ids. Choose the actual supporting successor(s), never broadcast evidence.'}
        return {'proposal_applied': False, 'graph_changed': False,
                'draft_saved': self.draft is not None, 'draft_revision': self.revision,
                'created_ids': [], 'not_created_ids': absent,
                'field_contracts': field_contracts(error), 'missing_update_objects': missing_updates,
                **({'source_repair': source_repair} if source_repair else {}),
                'repair': 'Use corrections on the latest rejected patch (JSON Pointer, add/replace/remove/move), or replace it with a complete patch. Other draft fields are retained. All graph changes remain atomic and require full validation. New edges: create_edges + id/evidence_ids; existing edge support: edge_updates + id/add_evidence. No rejected new object exists in the graph.'}
