"""Task-local graph projection and an audited, bounded active conversation."""
import json
from copy import deepcopy

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage


def action_message(response):
    """Keep structured tool actions; raw provider narration stays in the trace."""
    if response.tool_calls and not response.invalid_tool_calls:
        return response.model_copy(update={"content": ""})
    return response


def compact_node_packet(payload):
    """Share exact equal input/candidate records in one self-contained packet.

    Applying this at planning and sending is intentionally idempotent. A same-ID
    record with different content is never shared across views.
    """
    packet = deepcopy(payload)
    reused = {}
    previous = packet.get("source_equals_candidate", {})
    for archived, current in (("nodes", "candidate_nodes"), ("relations", "candidate_relations")):
        index = {row["id"]: row for row in packet.get(current, [])}
        equal = {row["id"] for row in packet.get(archived, []) if row == index.get(row["id"])}
        existing = {row["id"] for row in packet.get(archived, [])}
        equal.update(key for key in previous.get(archived, []) if key in index and key not in existing)
        reused[archived] = sorted(equal)
        if archived in packet:
            packet[archived] = [row for row in packet[archived] if row["id"] not in equal]
    packet["source_equals_candidate"] = reused
    if "evidence" in packet:
        packet["evidence"] = packet.pop("evidence")
    return packet


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _representable_call(call):
    return (isinstance(call.get('id'), str) and bool(call['id'])
            and isinstance(call.get('name'), str) and bool(call['name']))


def output_protocol_feedback(response, *, result_name, allowed_names=None):
    """A result and any other call form one ambiguous, unapplied response."""
    calls = [*response.tool_calls, *response.invalid_tool_calls]
    results = [call for call in calls if call.get('name') == result_name]
    invalid = response.invalid_tool_calls
    ids = [call.get('id') for call in calls]
    problem = None
    if any(not _representable_call(call) for call in calls):
        problem = 'A tool call lacks a valid name or call ID.'
    elif len(set(ids)) != len(ids):
        problem = 'Tool call IDs must be distinct within one response.'
    elif allowed_names is not None and any(call['name'] not in allowed_names for call in calls):
        problem = 'Unknown or unavailable tool in this response.'
    elif invalid:
        problem = 'Malformed tool JSON; original output archived.'
    elif results and len(calls) != 1:
        problem = f'{result_name} must be the only call in its response.'
    if problem is None:
        return None
    return {
        'status': 'rejected_unapplied', 'code': 'output_protocol',
        'error': (problem + ' '
                  'No calls from this response were executed; the previous draft is unchanged. '
                  f'Read first, then send exactly one complete {result_name}, without empty placeholders.'),
        'received_calls': len(calls), 'result_calls': len(results),
    }


def premise_fingerprint(payload, reads=(), *, policy=''):
    """Hash visible fact atoms, independent of tool envelopes and repeat reads.

    Record fields are keyed by view and object identity, so a projected read
    already covered by the packet is not new knowledge. Error narration and
    draft/task bookkeeping never advance the premise version. Historical review
    basis and explicitly supplied context are collected as facts too.
    """
    from cortex.capabilities.graph_merge.runtime.state import canonical_hash

    atoms, evidence, aliases = set(), {}, {}
    views = {'input', 'before', 'candidate', 'unknown'}
    unordered = {'ids', 'fields', 'edge_types', 'columns', 'owner_ids', 'member_ids',
                 'evidence_ids', 'common_evidence_ids', 'alternative_ids', 'targets', 'writable_ids'}

    def add(kind, view, ident, path, value):
        atoms.add(canonical_hash([kind, view, ident, path, value]))

    def fields(kind, view, ident, value, path=()):
        if isinstance(value, dict):
            if not value:
                add(kind, view, ident, path, {})
            for key, child in value.items():
                if not path and (key in {'id', 'read_errors', 'error', 'revision', 'recorded_at', 'call_id', 'request_id'}
                                 or kind == 'graph' and key in {'kind', 'view', 'fields'}):
                    continue
                # Complete node reads wrap the same business fields in content.
                if (key in {'content', 'resolved_content'} and kind == 'graph'
                        and (not path or path[-2:-1] == ('alternatives',))):
                    fields(kind, view, ident, child, path)
                else:
                    fields(kind, view, ident, child, (*path, key))
        elif isinstance(value, list):
            keyed = [(next((row[k] for k in ('id', 'alternative_id', 'unit_id', 'point_id') if k in row), None), row)
                     for row in value] if all(isinstance(row, dict) for row in value) else []
            if value and keyed and all(key is not None for key, _ in keyed):
                for key, row in keyed:
                    fields(kind, view, ident, row, (*path, str(key)))
            elif path and path[-1] in unordered:
                add(kind, view, ident, path, sorted({canonical_hash(row) for row in value}))
            else:
                add(kind, view, ident, path, value)
        else:
            add(kind, view, ident, path, value)

    def records(rows, view, kind='graph'):
        for row in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(row, dict) or row.get('id') is None:
                continue
            # A failed field selection may return only an ID. It contains no
            # selected fact and must not reset a no-progress observation.
            if set(row) - {'id', 'read_errors', 'error', 'revision', 'kind', 'view', 'fields', 'alternative_id'}:
                fields(kind, view, row['id'], row)

    def collect_evidence(rows):
        for row in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(row, dict) or 'id' not in row:
                continue
            for ident in row.get('evidence_ids') or [row['id']]:
                if 'content' in row:
                    evidence.setdefault(ident, []).append(row['content'])
                elif row.get('same_content_as'):
                    aliases[ident] = row['same_content_as']

    def source_units(groups):
        for group in groups if isinstance(groups, (list, tuple)) else ():
            if not isinstance(group, dict):
                continue
            owner = group.get('owner_id')
            fields('source_scope', 'before', owner,
                   {k: v for k, v in group.items() if k != 'source_units'})
            for unit in group.get('source_units', []):
                if isinstance(unit, dict) and unit.get('unit_id'):
                    add('source_unit', 'before', unit['unit_id'], ('owner_id',), owner)
                    fields('source_unit', 'before', unit['unit_id'], unit)

    def packet(data, view='candidate', query=None, tool=None, *, allow_direct=False, historical=False):
        if not isinstance(data, dict) or data.get('status') in {'error', 'rejected_unapplied'}:
            return
        if data.get('error') and not any(key in data for key in ('items', 'nodes', 'edges', 'evidence', 'facts')):
            return
        view = data.get('view') if data.get('view') in views else view
        def tagged(selected):
            return ('historical_candidate' if historical and selected == 'candidate' else
                    'historical_unqualified' if selected == 'unknown' else selected)
        # The narrow node interface nests the same explicit graph facts. These
        # must participate in both assessment reuse and no-progress detection.
        group = data.get('group')
        if isinstance(group, dict):
            records(group.get('members', []), tagged('before'))
            records(data.get('endpoint_directory', []), tagged('before'))
            fields('node_group', 'before', group.get('owner_id'),
                   {key: val for key, val in group.items() if key != 'members'})
        local_candidate = data.get('candidate')
        if isinstance(local_candidate, dict):
            if isinstance(local_candidate.get('node'), dict):
                records([local_candidate['node']], tagged('candidate'))
            records(local_candidate.get('nodes', []), tagged('candidate'))
            records(local_candidate.get('relations', []), tagged('candidate'))
            records(local_candidate.get('endpoint_directory', []), tagged('candidate'))
            if 'integrated_groups' in local_candidate:
                # Routing is a premise; explanation wording and list order are
                # not new facts that justify retrying the same rejected draft.
                groups = []
                for row in local_candidate['integrated_groups']:
                    routes = [{'source_unit_id': route['source_unit_id'],
                        'destination': {**route['destination'], **({'alternative_ids': sorted(route['destination']['alternative_ids'])}
                            if 'alternative_ids' in route['destination'] else {})}}
                        for route in row.get('source_routes', [])]
                    groups.append({'owner_id': row['owner_id'], 'member_ids': sorted(row['member_ids']),
                                   'source_routes': sorted(routes, key=lambda r: r['source_unit_id'])})
                fields('local_partition', tagged('candidate'), 'integrated_groups', sorted(groups, key=lambda g: g['owner_id']))
            if 'singleton_ids' in local_candidate:
                fields('local_partition', tagged('candidate'), 'singleton_ids', sorted(local_candidate['singleton_ids']))
        for key in ('node_schema', 'relation_types', 'allowed_evidence_ids', 'allowed_edge_ids', 'completeness', 'change_map', 'structural_diagnostics'):
            if key in data:
                fields('local_contract', '', key, data[key])
        for key in ('nodes', 'edges', 'relations', 'candidate_nodes', 'candidate_relations',
                    'candidate_edges', 'before_nodes', 'before_edges', 'before_relations',
                    'physical_facts', 'endpoint_nodes'):
            if key not in data:
                continue
            selected_view = ('before' if key.startswith('before_') else
                             'candidate' if key.startswith('candidate_') else
                             'before' if isinstance(group, dict) and key == 'relations' else
                             'input' if key == 'nodes' and 'candidate_nodes' in data else
                             'input' if key == 'relations' and 'candidate_relations' in data else view)
            records(data[key], tagged(selected_view))
        for old, current in (('nodes', 'candidate_nodes'), ('relations', 'candidate_relations')):
            shared = set(data.get('source_equals_candidate', {}).get(old, []))
            records([row for row in data.get(current, []) if row.get('id') in shared], 'input')
        records(data.get('contracts', []), tagged(view), 'contract')
        records(data.get('current_conflict_views', []), tagged(view))
        collect_evidence(data.get('evidence', []))
        source_units(data.get('source_unit_manifest', []))
        if 'items' in data:
            if tool == 'read_evidence':
                collect_evidence(data['items'])
            else:
                records(data['items'], tagged(view), 'contract' if (query or {}).get('mode') == 'contracts' or (query or {}).get('kind') == 'contract' else 'graph')
        if data.get('guidance') is not None:
            fields('guidance', '', (query or {}).get('topic', 'guidance'), data['guidance'])
        # A completed empty relation query supplies an absence fact. A missing
        # item or failed selector does not. Offset remains part of its scope.
        if ((query or {}).get('mode') == 'relations' and data.get('items') == []
                and data.get('complete') is True and data.get('next_offset') is None
                and not data.get('read_errors') and not data.get('missing_ids')):
            qualifiers = {'ids': sorted(set((query or {}).get('ids') or [])),
                          'direction': (query or {}).get('direction', 'both'),
                          'edge_types': sorted(set((query or {}).get('edge_types') or [])),
                          'offset': (query or {}).get('offset', 0)}
            if qualifiers['ids']:
                add('empty_relations', view, '', (), qualifiers)
        for basis in data.get('semantic_issue_basis', []):
            for item in basis.get('records', []):
                if isinstance(item, dict) and isinstance(item.get('record'), dict):
                    records([item['record']], 'historical_candidate' if item.get('view', view) == 'candidate' else item.get('view', view),
                            'contract' if item.get('kind') == 'contract' else 'graph')
        for supplied in data.get('required_context', []):
            if isinstance(supplied, dict):
                request = supplied.get('request') or {}
                packet(supplied.get('result', {}), request.get('view', view), request, allow_direct=True, historical=historical)
        facts = data.get('facts')
        if isinstance(facts, dict):
            packet(facts, view, allow_direct=True, historical=historical)
        elif isinstance(facts, (list, tuple)):
            for fact in facts:
                if not isinstance(fact, dict):
                    fields('supplied', view, '', fact)
                elif 'result' in fact:
                    request = fact.get('request') or {}
                    packet(fact['result'], request.get('view', view), request, allow_direct=True, historical=historical)
                elif 'id' in fact:
                    records([fact], tagged(fact.get('view', view)))
                else:
                    packet(fact, view, allow_direct=True, historical=historical)
        for fact in data.get('repair_facts', []):
            if isinstance(fact, dict):
                packet({'view': fact.get('view', view), 'facts': fact.get('facts')}, view,
                       allow_direct=True, historical=fact.get('snapshot_role') in {'historical_candidate', 'historical_unqualified'})
        for key in ('scope', 'identity_plan', 'value_domains', 'signals', 'allowed_inference',
                    'conflict_record_semantics'):
            if key in data:
                value = data[key]
                if isinstance(value, list):
                    for row in value:
                        fields('context', view, key, row)
                else:
                    fields('context', view, key, value)
        # Some required-context readers return a direct exact record or text.
        if allow_direct and not any(k in data for k in ('items', 'nodes', 'edges', 'relations', 'facts',
                                     'evidence', 'source_unit_manifest', 'contracts', 'task')):
            if 'id' in data:
                records([data], tagged(view))
            else:
                ignored = {'view', 'revision', 'complete', 'next_offset', 'missing_ids', 'read_errors',
                           'error', 'status', 'draft', 'draft_id', 'issues', 'issues_draft_id',
                           'latest_output_error', 'semantic_issues', 'feedback', 'earlier_findings',
                           'task_id', 'phase', 'presentation', 'recorded_at', 'time', 'call_id', 'request_id',
                           'required_context', 'semantic_issue_basis', 'draft_revision', 'candidate_hash',
                           'candidate_draft_id', 'candidate_matches_draft', 'compiled_from_draft_id', 'stage',
                           'source_equals_candidate'}
                direct = {key: value for key, value in data.items() if key not in ignored}
                if direct:
                    ident = (query or {}).get('id', '')
                    fields('contract' if (query or {}).get('kind') == 'contract' else 'graph' if ident else 'supplied', tagged(view), ident, direct)

    initial_view = payload.get('view') if payload.get('view') in views else (
        'input' if payload.get('phase') == 'identity' else 'candidate')
    packet(payload, initial_view)
    for read in reads:
        if not isinstance(read, dict) or read.get('name') == 'inspect_draft':
            continue
        query = read.get('args') or {}
        packet(read.get('result', read), query.get('view', initial_view), query, read.get('name'), allow_direct=True)
    # Resolve evidence aliases after seeing every packet/read, independent of
    # transport order; duplicate bodies are represented by the same fact atoms.
    for ident in set(evidence) | set(aliases):
        target, seen = ident, set()
        while target not in evidence and target in aliases and target not in seen:
            seen.add(target)
            target = aliases[target]
        for content in evidence.get(target, []):
            fields('evidence', '', ident, content)
    return canonical_hash({'policy': policy, 'facts': sorted(atoms)})

class TaskDialogue:
    """Render one canonical draft plus exact reads and version-bound diagnostics.

    Submitted actions are archived, never replayed as another full draft. The
    task packet owns the draft body; read replies may reference that location.
    """
    def __init__(self, system, payload):
        self.system = system
        self.payload = compact_node_packet(payload)
        self.reads = {}
        self.sequence = 0
        self.fallback_schema = None
        self.latest_draft = deepcopy(payload.get('draft'))
        self.draft_id = payload.get('draft_id') or self._draft_hash(self.latest_draft)
        self.latest_draft_feedback = self._issues(payload.get('issues', []))
        self.issues_draft_id = payload.get('issues_draft_id', self.draft_id)
        self.repair_facts = {}
        self.latest_draft_feedback = self._pin_diagnostic_facts(self.latest_draft_feedback)
        self.latest_output_error = None
        self.read_names = {'inspect_graph', 'lookup_catalog', 'read_evidence', 'read_guidance', 'read_source_units', 'inspect_draft'}
        # Review findings already have their own candidate/basis section.
        if self.payload.get('issues') == self.payload.get('semantic_issues'):
            self.latest_draft_feedback = []

    @staticmethod
    def _issues(value):
        return deepcopy(value if isinstance(value, list) else list(value) if isinstance(value, tuple) else [value] if value else [])

    @staticmethod
    def _draft_hash(draft):
        if draft is None:
            return None
        from cortex.capabilities.graph_merge.runtime.state import (
            canonical_hash,
        )
        return canonical_hash(draft)

    def _pin_diagnostic_facts(self, feedback, *, errors_draft_id=...):
        """Separate enduring exact facts from replaceable error narration."""
        normalized = self._issues(feedback)
        origin_id = self.issues_draft_id if errors_draft_id is ... else errors_draft_id
        empty = premise_fingerprint({})
        for error in normalized:
            if not isinstance(error, dict):
                continue
            for key in ('required_context', 'counterexamples', 'before', 'after'):
                value = error.get(key)
                if not value or isinstance(value, dict) and 'repair_fact_ref' in value:
                    continue
                view = value.get('view') if isinstance(value, dict) else None
                view = view if view in {'input', 'before', 'candidate'} else (
                    'before' if key == 'before' else 'candidate' if key == 'after' else 'unknown')
                facts = {'view': view, 'facts': deepcopy(value)}
                if premise_fingerprint({}, [{'result': facts}]) == empty:
                    continue  # ID-only correction references are not new facts.
                ident = self._draft_hash(facts)
                if ident not in self.repair_facts:
                    binding = {'draft_id': origin_id,
                               'draft_hash': self._draft_hash(self.latest_draft) if origin_id == self.draft_id else None}
                    self.repair_facts[ident] = {'fact_id': ident, **facts,
                        'snapshot_role': ('historical_candidate' if view == 'candidate' else
                                          'historical_unqualified' if view == 'unknown' else view),
                        'snapshot_binding': binding if view in {'candidate', 'unknown'} else None}
                error[key] = {'repair_fact_ref': ident}
        return normalized

    def messages(self):
        packet = deepcopy(self.payload)
        for key in ('draft', 'draft_id', 'issues', 'issues_draft_id', 'draft_validation_issues'):
            packet.pop(key, None)
        if self.latest_draft is not None:
            packet.update(draft=deepcopy(self.latest_draft), draft_id=self.draft_id)
            if self.latest_draft_feedback:
                packet.update(issues=deepcopy(self.latest_draft_feedback), issues_draft_id=self.issues_draft_id)
                if self.issues_draft_id != self.draft_id:
                    packet['issues_binding'] = 'unbound_historical'
        elif self.latest_draft_feedback:
            packet['issues'] = deepcopy(self.latest_draft_feedback)
        if self.repair_facts:
            packet['repair_facts'] = deepcopy(list(self.repair_facts.values()))
        if self.latest_output_error is not None:
            packet['latest_output_error'] = deepcopy(self.latest_output_error)
        result = [SystemMessage(self.system), HumanMessage(_json(packet))]
        for _, messages in sorted(self.reads.values(), key=lambda item: item[0]):
            result.extend(messages)
        if self.fallback_schema is not None:
            result.append(HumanMessage('Return the final result as plain JSON matching this schema: '
                                       + _json(self.fallback_schema)))
        return result

    def use_fallback(self, schema):
        self.fallback_schema = schema

    def set_draft(self, draft, feedback=(), *, draft_id=None):
        if not isinstance(draft, dict):
            raise TypeError('A complete result must be a JSON object')
        changed = draft != self.latest_draft
        self.latest_draft = deepcopy(draft)
        self.draft_id = draft_id or self._draft_hash(draft)
        self.issues_draft_id = self.draft_id
        self.latest_draft_feedback = self._pin_diagnostic_facts(feedback)
        self.latest_output_error = None
        if changed:
            self._clear_draft_reads()

    def plain_failure(self, response, feedback, *, draft=None):
        if isinstance(draft, dict):
            self.set_draft(draft, feedback)
        else:
            self.latest_output_error = deepcopy(feedback)

    def _clear_draft_reads(self):
        self.reads = {key: value for key, value in self.reads.items()
                      if not key.startswith('inspect_draft:')}

    def _draft_read(self, value):
        """Share the body while preserving fresh, explicitly bound diagnostics."""
        result = deepcopy(value)
        projection = result.get('projection')
        body = result.get('draft')
        ident = result.get('draft_id') or (self.draft_id if projection else self._draft_hash(body))
        if not projection and isinstance(body, dict):
            if self.latest_draft is not None and body != self.latest_draft:
                errors_ident = result.get('issues_draft_id', ident)
                self.latest_output_error = {'code': 'stale_draft_read', 'draft_id': ident,
                    'issues': self._pin_diagnostic_facts(result.get('issues', []), errors_draft_id=errors_ident),
                    'issues_draft_id': errors_ident,
                    'error': 'The read does not match the canonical task draft; its body was archived.'}
                return None
            if self.latest_draft is None:
                self.set_draft(body, (), draft_id=ident)
            elif ident and (self.draft_id == self._draft_hash(body) or ident == self.draft_id):
                self.draft_id = ident
        matches = ident == self.draft_id
        errors_ident = result.get('issues_draft_id', ident)
        if 'issues' in result:
            if matches and errors_ident == self.draft_id:
                self.issues_draft_id = errors_ident
                self.latest_draft_feedback = ([] if result['issues'] == self.payload.get('semantic_issues')
                                              else self._pin_diagnostic_facts(result['issues']))
                result['issues'] = deepcopy(self.latest_draft_feedback)
            elif result['issues']:
                result['issues'] = self._pin_diagnostic_facts(result['issues'], errors_draft_id=errors_ident)
                self.latest_output_error = {'code': 'unbound_draft_diagnostics',
                    'issues': deepcopy(result['issues']), 'issues_draft_id': errors_ident,
                    'error': 'These diagnostics do not establish an error in the current draft.'}
        if not projection and isinstance(body, dict) and body == self.latest_draft:
            # Diagnostics have been transferred to the canonical task packet.
            return None
        return result

    def exchange(self, response, results, *, result_name):
        error = output_protocol_feedback(response, result_name=result_name)
        if error:
            self.plain_failure(response, error)
            return
        calls = {c.get('id'): c for c in response.tool_calls if _representable_call(c)}
        for call_id, result in results:
            call = calls.get(call_id)
            if call is None:
                continue
            name = call['name']
            if name == result_name:
                if isinstance(call['args'], dict):
                    self.set_draft(call['args'], result if isinstance(result, dict) and result.get('status') == 'rejected_unapplied' else ())
                continue
            if name not in self.read_names:
                self.latest_output_error = deepcopy(result)
                continue
            if name == 'inspect_draft':
                result = self._draft_read(result)
                if result is None:
                    continue
            self.sequence += 1
            action = action_message(response).model_copy(update={'tool_calls': [call], 'invalid_tool_calls': []})
            signature = name + ':' + _json([call.get('args'), result])
            self.reads[signature] = (self.sequence, [action, ToolMessage(_json(result), tool_call_id=call_id)])

    def premises(self):
        facts = []
        for _, messages in self.reads.values():
            call = messages[0].tool_calls[0]
            if call['name'] != 'inspect_draft':
                facts.append({'name': call['name'], 'args': call['args'], 'result': json.loads(messages[1].content)})
        if self.repair_facts:
            facts.append({'name': 'repair_facts', 'result': {'repair_facts': deepcopy(list(self.repair_facts.values()))}})
        return facts

    def premise_hash(self, *, policy=''):
        return premise_fingerprint(json.loads(self.messages()[1].content), self.premises(), policy=policy)

    def snapshot(self):
        return {'version': 1, 'draft': deepcopy(self.latest_draft), 'draft_id': self.draft_id,
                'draft_hash': self._draft_hash(self.latest_draft),
                'issues': deepcopy(self.latest_draft_feedback), 'issues_draft_id': self.issues_draft_id,
                'latest_output_error': deepcopy(self.latest_output_error),
                'repair_facts': deepcopy(list(self.repair_facts.values())),
                'reads': [{'sequence': seq, 'call': deepcopy(messages[0].tool_calls[0]),
                           'result': json.loads(messages[1].content)} for seq, messages in self.reads.values()]}

    def restore(self, saved, *, draft_authoritative=True):
        """Validate the whole cache before applying it to authoritative state."""
        if not isinstance(saved, dict) or type(saved.get('version')) is not int or saved['version'] != 1:
            raise ValueError('Invalid saved task dialogue')
        self._draft_hash(saved)  # Reject non-JSON and non-finite cache values before mutation.
        draft = saved.get('draft')
        ident, binding = saved.get('draft_id'), saved.get('issues_draft_id')
        if ((draft is not None and not isinstance(draft, dict))
                or saved.get('draft_hash') != self._draft_hash(draft)
                or draft is None and ident is not None
                or draft is not None and (not isinstance(ident, str) or not ident)
                or not isinstance(saved.get('issues'), list)
                or binding is not None and (not isinstance(binding, str) or not binding or binding != ident)
                or saved.get('latest_output_error') is not None and not isinstance(saved['latest_output_error'], dict)):
            raise ValueError('Invalid saved draft or diagnostic binding')
        rows = saved.get('reads', [])
        repair = saved.get('repair_facts', [])
        if not isinstance(rows, list) or not isinstance(repair, list):
            raise TypeError('Invalid saved read/fact collection')
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError('Invalid saved read')
            call, result = row.get('call'), row.get('result')
            if (not isinstance(call, dict) or not _representable_call(call)
                    or call['name'] not in self.read_names or not isinstance(call.get('args'), dict)
                    or not isinstance(result, dict) or type(row.get('sequence')) is not int
                    or row['sequence'] < 0):
                raise ValueError('Only complete read exchanges may be restored')
            if call['name'] == 'inspect_draft' and (
                    result.get('draft') is not None and not isinstance(result['draft'], dict)
                    or 'issues' in result and not isinstance(result['issues'], list)
                    or result.get('issues_draft_id') is not None and not isinstance(result['issues_draft_id'], str)
                    or result.get('projection') is not None and not isinstance(result['projection'], dict)):
                raise ValueError('Invalid saved draft read or diagnostics')
        for fact in repair:
            if (not isinstance(fact, dict) or fact.get('view') not in {'input', 'before', 'candidate', 'unknown'}
                    or fact.get('fact_id') != self._draft_hash({'view': fact.get('view'), 'facts': fact.get('facts')})
                    or fact.get('snapshot_role') != ('historical_candidate' if fact['view'] == 'candidate' else
                                                        'historical_unqualified' if fact['view'] == 'unknown' else fact['view'])):
                raise ValueError('Invalid saved compiler facts')
            source = fact.get('snapshot_binding')
            if fact['view'] in {'candidate', 'unknown'} and (not isinstance(source, dict)
                    or source.get('draft_id') is not None and not isinstance(source['draft_id'], str)
                    or source.get('draft_hash') is not None and not isinstance(source['draft_hash'], str)):
                raise ValueError('Historical candidate facts require their source binding')
        known_facts = set(self.repair_facts) | {fact['fact_id'] for fact in repair}
        def validate_refs(value):
            if isinstance(value, dict):
                if 'repair_fact_ref' in value and value['repair_fact_ref'] not in known_facts:
                    raise ValueError('Saved diagnostic refers to missing compiler facts')
                for child in value.values():
                    validate_refs(child)
            elif isinstance(value, list):
                for child in value:
                    validate_refs(child)
        validate_refs(saved['issues'])
        validate_refs(saved.get('latest_output_error'))
        for row in rows:
            validate_refs(row['result'])
        # Apply to a detached staging object. A bad read/schema cannot partially
        # mutate the canonical draft or diagnostics before failing validation.
        target = deepcopy(self)
        same_draft = saved['draft_hash'] == self._draft_hash(target.latest_draft)
        if not draft_authoritative:
            if draft is not None:
                target.set_draft(draft, (), draft_id=ident)
                target.issues_draft_id = binding
                target.latest_draft_feedback = target._pin_diagnostic_facts(saved['issues'])
            same_draft = True
        elif same_draft and 'issues' not in target.payload:
            target.issues_draft_id = binding
            target.latest_draft_feedback = target._pin_diagnostic_facts(saved['issues'])
        target.latest_output_error = deepcopy(saved.get('latest_output_error'))
        for fact in repair:
            target.repair_facts.setdefault(fact['fact_id'], deepcopy(fact))
        for row in sorted(rows, key=lambda value: value['sequence']):
            call = row['call']
            if call['name'] == 'inspect_draft' and not same_draft:
                continue
            result = deepcopy(row['result'])
            if call['name'] == 'inspect_draft' and 'issues' in target.payload:
                result.pop('issues', None)
                result.pop('issues_draft_id', None)
            from langchain_core.messages import AIMessage
            target.exchange(AIMessage(content='', tool_calls=[call]), [(call['id'], result)], result_name='__restored_read__')
        self.__dict__.clear()
        self.__dict__.update(target.__dict__)
