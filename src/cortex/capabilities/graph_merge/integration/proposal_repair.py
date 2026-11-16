"""Repair one unapplied proposal; graph changes still require atomic validation."""
from copy import deepcopy
from typing import Any, Literal, get_args

from pydantic import Field, ValidationError, model_validator

from cortex.capabilities.graph_merge.integration.models import Model
from cortex.capabilities.graph_merge.integration.relation_edits import RelationPatch


class ValueCorrection(Model):
    op: Literal['add', 'replace']
    path: str = Field(description='JSON Pointer into the latest rejected patch, e.g. /edge_updates/0/add_evidence.')
    value: Any = Field(description='Required replacement or inserted value; explicit null is allowed.')


class RemoveCorrection(Model):
    op: Literal['remove']
    path: str = Field(description='JSON Pointer to the field or list item to remove from the saved patch.')


class MoveCorrection(Model):
    op: Literal['move']
    from_path: str = Field(description='Required source JSON Pointer in the saved patch, e.g. /new_edges.')
    path: str = Field(description='Required destination JSON Pointer, e.g. /create_edges; must not overwrite an existing object key.')


Correction = ValueCorrection | RemoveCorrection | MoveCorrection
_CORRECTION_MODELS = {op: model for model in get_args(Correction)
                      for op in get_args(model.model_fields['op'].annotation)}


class PatchSubmission(Model):
    # Public schema. Runtime saves raw patch JSON before validating its contents.
    patch: RelationPatch | None = None
    corrections: tuple[Correction, ...] | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def exactly_one(self):
        if (self.patch is None) == (self.corrections is None):
            raise ValueError('Supply exactly one: complete patch or nonempty corrections to the latest rejected patch')
        return self


class CorrectionError(ValueError):
    def __init__(self, index, message, details=None):
        self.path = f'/corrections/{index}'
        self.details = details
        super().__init__(f'{message}; the saved draft and graph are unchanged')


def _parts(path):
    if not path.startswith('/'):
        raise ValueError('Use a non-root JSON Pointer beginning with /')
    return [p.replace('~1', '/').replace('~0', '~') for p in path[1:].split('/')]


def _location(document, path):
    parts = _parts(path)
    parent = document
    for part in parts[:-1]:
        if isinstance(parent, list) and part.isdigit() and int(part) < len(parent):
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
            raise ValueError(f'List index must be nonnegative: {path}')
    if not isinstance(parent, (dict, list)):
        raise ValueError(f'Correction parent is not an object/list: {path}')
    return parent, key


def _remove(document, path):
    parent, key = _location(document, path)
    if (isinstance(parent, list) and key >= len(parent)) or (isinstance(parent, dict) and key not in parent):
        raise ValueError(f'Correction target does not exist: {path}')
    return parent.pop(key)


def _write(document, path, value, *, replace=False, move=False):
    parent, key = _location(document, path)
    if isinstance(parent, list):
        if key > len(parent) or (replace and key == len(parent)):
            raise ValueError(f'Correction target does not exist: {path}')
        if replace:
            parent[key] = value
        else:
            parent.insert(key, value)
    else:
        if move and key in parent:
            raise ValueError(f'Move destination already exists: {path}; explicitly combine the entries or replace the intended value')
        if replace and key not in parent:
            raise ValueError(f'Correction target does not exist: {path}')
        parent[key] = value


def _pointer(parts):
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts) if parts else ''


def _schema_errors(error):
    schema = {'PatchSubmission': PatchSubmission}.get(error.title, RelationPatch).model_json_schema()
    definitions = schema.get('$defs', {})
    seen, result = set(), []

    def resolve(value):
        while '$ref' in value:
            value = definitions.get(value['$ref'].split('/')[-1], {})
        if 'anyOf' in value:
            value = next((v for v in value['anyOf'] if v.get('type') != 'null'), {})
            return resolve(value)
        return value

    for issue in error.errors(include_input=False, include_url=False):
        parts = list(issue['loc'])
        current = schema
        for part in parts[:-1]:
            current = resolve(current)
            current = current.get('items', {}) if isinstance(part, int) else current.get('properties', {}).get(part, {})
        current = resolve(current)
        row = {'path': _pointer(parts), 'message': issue['msg']}
        parent = _pointer(parts[:-1])
        if parent not in seen and current.get('properties'):
            seen.add(parent)
            row['details'] = {'parent_path': parent, 'allowed_fields': list(current['properties']), 'required_fields': current.get('required', [])}
        result.append(row)
    return result


def _rows(draft, field):
    value = draft.get(field, ())
    return value if isinstance(value, (list, tuple)) else ()


def _references(rows):
    if not isinstance(rows, (list, tuple)) or any(not isinstance(r, dict) for r in rows):
        return None
    return frozenset((r.get('kind'), r.get('id'), r.get('alternative_id')) for r in rows)


class ProposalRepair:
    """One ephemeral draft per Writer episode; never a partially accepted graph."""
    def __init__(self):
        self.draft = None
        self.revision = 0  # Internal audit only, not a model-managed concurrency token.

    def prepare(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) - {'patch', 'corrections'}:
            raise ValueError('submit_changes accepts exactly one of patch or corrections')
        patch, corrections = arguments.get('patch'), arguments.get('corrections')
        if (patch is None) == (corrections is None):
            raise ValueError('Supply exactly one: complete patch or nonempty corrections')
        if corrections is not None:
            if not isinstance(corrections, (list, tuple)) or not corrections:
                raise ValueError('corrections must be a nonempty list')
            if self.draft is None:
                raise ValueError('No rejected draft exists; submit a complete patch first')
            value = deepcopy(self.draft)
            for index, raw in enumerate(corrections):
                model = None
                try:
                    op = raw.get('op') if isinstance(raw, dict) else None
                    model = _CORRECTION_MODELS.get(op) if isinstance(op, str) else None
                    if model is None:
                        raise ValueError('Correction op must be add, replace, remove, or move')
                    correction = model.model_validate(raw)
                    if correction.op == 'move':
                        source, target = _parts(correction.from_path), _parts(correction.path)
                        if source == target or target[:len(source)] == source:
                            raise ValueError('Cannot move an entry into itself or its descendant')
                        moved = _remove(value, correction.from_path)
                        _write(value, correction.path, moved, move=True)
                    elif correction.op == 'remove':
                        _remove(value, correction.path)
                    else:
                        _write(value, correction.path, deepcopy(correction.value), replace=correction.op == 'replace')
                except ValidationError as error:
                    message = '; '.join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors(include_input=False, include_url=False))
                    schema = model.model_json_schema()
                    raise CorrectionError(index, message, {
                        'allowed_fields': list(schema['properties']),
                        'required_fields': schema.get('required', []),
                    }) from error
                except ValueError as error:
                    raise CorrectionError(index, str(error)) from error
        else:
            if not isinstance(patch, dict):
                raise ValueError('patch must be a JSON object')
            value = deepcopy(patch)
        self.draft = value
        self.revision += 1
        return deepcopy(value)

    def feedback(self, error, context):
        draft = self.draft or {}
        current = context.active_batch.preview or context.active_batch.seed
        existing = {o.id for o in (*current.graph.nodes, *current.graph.edges)}
        if isinstance(error, ValidationError):
            errors = _schema_errors(error)
        elif isinstance(error, CorrectionError):
            errors = [{'path': error.path, 'message': str(error)}]
            if error.details:
                errors[0]['details'] = error.details
        elif hasattr(error, 'missing_sources'):
            before = [r.model_dump(exclude_none=True) for r in error.before]
            after = [r.model_dump(exclude_none=True) for r in error.after]
            mapped = {e.source for e in context.evidence if e.source in error.missing_sources}
            details = {'before': before, 'after': after,
                       'missing_evidence_ids': sorted({e.id for e in context.evidence if e.source in error.missing_sources}),
                       'unmapped_source_count': len(set(error.missing_sources) - mapped)}
            paths = [f'/retirements/{i}' for i, r in enumerate(_rows(draft, 'retirements')) if isinstance(r, dict)
                     and _references(r.get('before')) == _references(before)
                     and _references(r.get('after', [])) == _references(after)]
            row = {'message': 'Replacement loses source support. Assign each missing evidence ID to its actual successor: existing objects use add_evidence, new objects use evidence_ids. Do not broadcast evidence.', 'details': details}
            if len(paths) == 1:
                row['path'] = paths[0]
            if details['unmapped_source_count']:
                row['message'] += ' Some exact sources have no available evidence ID; this needs evidence-context resolution, not an invented ID.'
            errors = [row]
        else:
            errors = [{'message': str(error)}]
        if not isinstance(error, (ValidationError, CorrectionError)):
            evidence_ids = {e.id for e in context.evidence}
            for field, key in (('create_nodes', 'evidence_ids'), ('create_edges', 'evidence_ids'),
                               ('node_updates', 'add_evidence'), ('edge_updates', 'add_evidence')):
                for index, row in enumerate(_rows(draft, field)):
                    values = row.get(key, ()) if isinstance(row, dict) else ()
                    if not isinstance(values, (list, tuple)):
                        continue
                    unknown = sorted({v for v in values if isinstance(v, str) and v not in evidence_ids})
                    if unknown:
                        errors.append({'path': f'/{field}/{index}/{key}',
                            'message': 'Unknown evidence IDs; use available evidence IDs supporting this object.',
                            'details': {'unknown_evidence_ids': unknown}})
            for field, create in (('node_updates', 'create_nodes'), ('edge_updates', 'create_edges')):
                for index, row in enumerate(_rows(draft, field)):
                    if isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] not in existing:
                        errors.append({'path': f'/{field}/{index}/id',
                            'message': f'Object is absent from the candidate; rejected proposals create nothing. Use a current ID or {create} with required fields.',
                            'details': {'object_id': row['id'], 'exists_in_candidate': False}})
        return {'status': 'rejected_unapplied', 'errors': errors}
