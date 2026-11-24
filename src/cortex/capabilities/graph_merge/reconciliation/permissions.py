"""Permission-bound reconciliation facts and readable cached user prompts."""
from copy import deepcopy
from functools import wraps

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.evidence import EvidencePresentation
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.schema import GraphState
from cortex.schema.references import property_reference_ids
from cortex.schema.relation_contracts import authoring_schema, effective_edge_fields, effective_schema
from cortex.capabilities.graph_merge.llm.prompt_language import english_contract
from cortex.schema.relation_vocabulary import ABSTRACT_RELATIONS
from cortex.capabilities.graph_merge.reconciliation.policy import SIGNALS, OBLIGATIONS


def _atomic_read(method):
    """A failed multi-object read must not leave unjournalled facts or grants."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        names = ('working', 'evidence_view', 'visible_node_ids', 'visible_edge_ids',
                 'allowed_evidence_ids', 'complete_reads', 'write_contracts',
                 'coverage', '_journal')
        previous = {name: deepcopy(getattr(self, name)) for name in names}
        try:
            return method(self, *args, **kwargs)
        except Exception:
            for name, value in previous.items():
                setattr(self, name, value)
            raise
    return wrapped


class ReconciliationRuntime:
    """Authoritative task-local facts; no model-call or tool-message history."""

    def __init__(self, context, payload, phase, *, system=''):
        from cortex.capabilities.graph_merge.reconciliation.contract import DraftDocument
        self.context, self.payload, self.phase, self.system = context, deepcopy(payload), phase, system
        self.before = getattr(context, 'task_before', None)
        if self.before is None:
            raise ValueError('Reconciliation requires its immutable before snapshot')
        self.before_id = canonical_hash(self.before)
        self.task_id = payload.get('task_id') or getattr(getattr(context, 'active_task', None), 'id', None)
        if not self.task_id:
            raise ValueError('Reconciliation requires a task ID')
        raw = payload.get('draft') or getattr(context, 'active_draft', None)
        self.draft = DraftDocument.model_validate(raw) if isinstance(raw, dict) and 'before_id' in raw and 'version' in raw else DraftDocument(task_id=self.task_id, before_id=self.before_id, version=0)
        self.feedback = {}
        self.packet = {'task_id': self.task_id, 'before_id': self.before_id, 'signals': [], 'obligations': []}
        self.visible_node_ids = {'before': set(), 'candidate': set()}
        self.visible_edge_ids = {'before': set(), 'candidate': set()}
        self.allowed_evidence_ids = set()
        self.signal_ids = set()
        self.working = {'before': {'nodes': {}, 'edges': {}}, 'candidate': {'nodes': {}, 'edges': {}}}
        self.evidence_view = {'bodies': {}, 'id_to_body': {}}
        self.complete_reads = set()
        self.write_contracts = set()
        self.coverage = []
        self._journal = []
        self._candidate = getattr(context, 'candidate', None) if phase == 'graph_assess' else None
        self.candidate_version = self.draft.version if self._candidate is not None else None
        self._draft_authoritative = isinstance(raw, dict) and 'before_id' in raw and 'version' in raw
        self._relation_anchors = set()
        self._initialize()
        if phase == 'graph_assess':
            from cortex.capabilities.graph_merge.reconciliation.contract import AssessmentResult, public_schema
            self.packet['assessment_output_schema'] = public_schema(AssessmentResult)
        self._bundle_seed_nodes = {view: set(self.visible_node_ids[view]) for view in ('before', 'candidate')}
        self._bundle_seed_edges = {view: set(self.visible_edge_ids[view]) for view in ('before', 'candidate')}
        self._finish_bundle()
        if phase == 'graph_assess':
            # Source-variant checks explicitly nominate the evidence to compare.
            # Use the ordinary read path so snapshots and receipts retain dependencies.
            ids = sorted({v['evidence_id'] for check in self.packet['signals']
                          if check['kind'] == 'source_selection_variants'
                          for v in check.get('variants', []) if v.get('evidence_id')})
            if ids:
                self.read_evidence(ids)
        self._initial_before = deepcopy(self.working['before'])
        from cortex.capabilities.graph_merge.reconciliation.presentation import fixed_prompt
        self._fixed_user_prompt = fixed_prompt(self, self._initial_before)
        context.reconciliation_runtime = self

    def _initialize(self):
        task = getattr(self.context, 'active_task', None)
        checks = deepcopy(list(getattr(task, 'checks', ()) or self.payload.get('signals', [])))
        checks.extend(deepcopy(self.payload.get('obligations', [])))
        obligations = self.payload.get('node_relation_obligations')
        # Current tasks already carry the complete explicit signal set. Legacy
        # fallback must not duplicate it or attach whole graphs to each signal.
        if not checks and obligations:
            for row in obligations.get('diagnostics', []):
                code = str(row.get('code', '')).lower()
                kind = ('metric_direct_inputs' if 'metric' in code or 'formula' in code else
                        'target_alternative_scope' if 'target' in code or 'alternative' in code else 'node_relation_followup')
                checks.append({'kind': kind, 'mode': 'required', 'observed': deepcopy(row)})
            if not obligations.get('diagnostics'):
                checks.append({'kind': 'node_relation_followup', 'mode': 'required',
                    'observed': {'node_ids': list(obligations.get('member_ids', [])),
                                 'reason': 'Required node-relation followup'}})
        legacy_feedback = [] if checks else list(getattr(task, 'feedback', ()) or self.payload.get('feedback', []))
        for row in legacy_feedback:
            if not isinstance(row, dict):
                continue
            if row.get('kind') in SIGNALS:
                checks.append(deepcopy(row))
            elif row.get('kind') == 'node_relation_obligations' or row.get('code'):
                checks.append({'kind': 'node_relation_followup', 'mode': 'required', 'observed': deepcopy(row)})
        unique = {}
        for row in checks:
            if not isinstance(row, dict) or row.get('kind') not in {*SIGNALS, *OBLIGATIONS}:
                raise ValueError('Reconciliation requires a supported explicit signal')
            row.setdefault('id', row.get('signal_id') or 'signal-' + canonical_hash(row)[:16])
            if row['id'] in unique and unique[row['id']] != row:
                raise ValueError('Ambiguous reconciliation check ID with different content: ' + row['id'])
            unique[row['id']] = row
        if not unique:
            raise ValueError('Reconciliation has no explicit signal or required obligation')
        self.packet['signals'] = [row for row in unique.values() if row['kind'] in SIGNALS]
        self.packet['obligations'] = [row for row in unique.values() if row['kind'] in OBLIGATIONS]
        self.signal_ids = set(unique)
        self.packet['scope_rules'] = {'initial_scope': deepcopy(self.payload.get('scope', {})), 'unrelated_writes': False}
        from cortex.capabilities.graph_merge.reconciliation.scope import signal_scope
        self._checks = list(unique.values())
        self._signal_scope = signal_scope(self.context, self.before, self._checks)
        self.packet['relation_sets'] = deepcopy(self._signal_scope.queries)
        self._relation_anchors = set(self._signal_scope.core_node_ids)
        self.write_node_ids = set(self._signal_scope.writable_node_ids)
        self.write_edge_ids = set(self._signal_scope.writable_edge_ids)
        self.packet['write_scope'] = {'node_ids': sorted(self.write_node_ids),
                                      'edge_ids': sorted(self.write_edge_ids),
                                      'metadata_only_node_ids': sorted(getattr(self._signal_scope, 'metadata_only_node_ids', set()))}
        nodes = {n.id: n for n in self.before.graph.nodes}
        edges = {e.id: e for e in self.before.graph.edges}
        for ident in sorted(self._signal_scope.node_ids & nodes.keys()):
            self._put_summary('before', nodes[ident])
        for ident in sorted(self._signal_scope.edge_ids & edges.keys()):
            self._put_edge('before', edges[ident])
        if self.phase == 'graph_assess':
            self._initialize_candidate()
        semantic = self.payload.get('semantic_issues', ()) or getattr(self.context, 'task_semantic_issues', ())
        self.feedback['semantic_issues'] = deepcopy(list(semantic))
        self.feedback['mechanical'] = deepcopy(self.payload.get('issues', list(getattr(self.context, 'task_draft_errors', ()))))
        self.feedback['semantic_issue_basis'] = deepcopy(self.payload.get('semantic_issue_basis', list(getattr(self.context, 'task_semantic_issue_basis', ()))))
        if any(row['kind'] == 'abstract_predicate' for row in [*self.packet['signals'], *self.packet['obligations']]):
            for view in ('before', 'candidate'):
                if view == 'before' or self._candidate is not None:
                    self._put_predicate_catalog(view)


    def _initialize_candidate(self):
        candidate = self._candidate
        if candidate is None:
            raise ValueError('Assessment requires the actual compiled candidate')
        self.working['candidate'] = {'nodes': {}, 'edges': {}}
        self.visible_node_ids['candidate'], self.visible_edge_ids['candidate'] = set(), set()
        self.complete_reads = {row for row in self.complete_reads if row[0] != 'candidate'}
        before_nodes = {n.id: n for n in self.before.graph.nodes}
        before_edges = {e.id: e for e in self.before.graph.edges}
        candidate_nodes = {n.id: n for n in candidate.graph.nodes}
        candidate_edges = {e.id: e for e in candidate.graph.edges}
        def delta(old, new):
            return {'created': sorted(new.keys() - old.keys()),
                    'deleted': sorted(old.keys() - new.keys()),
                    'updated': sorted(key for key in old.keys() & new.keys() if old[key] != new[key])}
        changes = {'nodes': delta(before_nodes, candidate_nodes), 'edges': delta(before_edges, candidate_edges)}
        # The independent reviewer receives actual writes, including writes the
        # author did not mention. Never derive this coverage from its promises.
        changed_nodes = {ident for values in changes['nodes'].values() for ident in values}
        changed_edges = {ident for values in changes['edges'].values() for ident in values}
        ids = set(self._signal_scope.node_ids) | changed_nodes
        ids |= set(getattr(self, '_bundle_seed_nodes', {}).get('before', self.visible_node_ids['before']))
        for ident in changed_edges:
            for edge in (before_edges.get(ident), candidate_edges.get(ident)):
                if edge:
                    ids.update((edge.from_node_id, edge.to_node_id))
            if ident in before_edges:
                self._put_edge('before', before_edges[ident])
        for ident in ids & before_nodes.keys():
            self._put_summary('before', before_nodes[ident])
        for node in candidate.graph.nodes:
            if node.id in ids:
                self._put_summary('candidate', node)
        edges = set(self._signal_scope.edge_ids) | changed_edges
        for edge in candidate.graph.edges:
            if edge.id in edges:
                self._put_edge('candidate', edge)
                for node in candidate.graph.nodes:
                    if node.id in {edge.from_node_id, edge.to_node_id}:
                        self._put_summary('candidate', node)
        self.working['actual_changes'] = changes
        if hasattr(self, '_bundle_seed_nodes'):
            self._bundle_seed_nodes['candidate'] = set(self.visible_node_ids['candidate'])
            self._bundle_seed_edges['candidate'] = set(self.visible_edge_ids['candidate'])

    def _put_summary(self, view, node):
        """Install a complete public business object (name kept private to callers)."""
        from cortex.capabilities.graph_merge.reconciliation.projection import node_record
        row = node_record(self.context, node)
        if view in {'before', 'candidate'}:
            snapshot = self._snapshot(view)
            for field in ('name', 'description', 'aliases', 'tags', 'content', 'alternatives'):
                self.complete_reads.add((view, snapshot, node.id, field))
            for key in self.context.schema.effective_node_fields(node.node_type):
                self.complete_reads.add((view, snapshot, node.id, 'properties.' + key))
            for key in node.properties:
                self.complete_reads.add((view, snapshot, node.id, 'properties.' + key))
        self.working[view]['nodes'][node.id] = row
        self.visible_node_ids[view].add(node.id)
        self.allowed_evidence_ids.update(row['evidence_ids'])

    def _put_edge(self, view, edge):
        evidence = EvidencePresentation(self.context)
        from cortex.capabilities.graph_merge.reconciliation.projection import edge_record
        self.working[view]['edges'][edge.id] = edge_record(self.context, edge)
        self.visible_edge_ids[view].add(edge.id)
        self.allowed_evidence_ids.update(evidence.ids_for(edge.sources))
        self._put_contract(view, edge.edge_type)

    def _contract_record(self, view, ident):
        state = self._state(view)
        schema = effective_schema(self.context.schema, state.graph)
        contract = schema.edge_type_index().get(ident)
        if contract is None:
            return None
        row = contract.model_dump(mode='json')
        row['fields'] = {name: field.model_dump(mode='json') for name, field in effective_edge_fields(schema, contract).items()}
        installed = authoring_schema(self.context.schema, state.graph).edge_type_index()
        row['authorable'] = ident in installed and ident not in ABSTRACT_RELATIONS
        if not row['authorable']:
            row['read_only_reason'] = 'abstract_relation_base' if ident in ABSTRACT_RELATIONS else 'historical_compatibility_only'
        def clean(value):
            if isinstance(value, dict):
                if value.get('default', ...) is None:
                    value.pop('default')
                for item in value.values():
                    clean(item)
            elif isinstance(value, list):
                for item in value:
                    clean(item)
        clean(row)
        return english_contract(row)

    def _put_contract(self, view, ident):
        row = self._contract_record(view, ident)
        if row is not None:
            self.working[view].setdefault('contracts', {})[ident] = row
        return row

    def _put_predicate_catalog(self, view):
        """Only contracts used by or compatible with the declared signal sets."""
        from cortex.capabilities.graph_merge.reconciliation.scope import signal_scope
        scope = signal_scope(self.context, self._state(view), self._checks)
        for ident in sorted(scope.contract_ids):
            self._put_contract(view, ident)

    def _finish_bundle(self):
        """Render declared signal sets plus actual effects; never generic adjacency."""
        from cortex.capabilities.graph_merge.reconciliation.scope import signal_scope
        for view in ('before', 'candidate'):
            if view == 'candidate' and self._candidate is None:
                continue
            state = self._state(view)
            scope = signal_scope(self.context, state, self._checks)
            nodes = {node.id: node for node in state.graph.nodes}
            edges = {edge.id: edge for edge in state.graph.edges}
            selected_nodes = set(scope.node_ids) | self._bundle_seed_nodes.get(view, set())
            selected_edges = set(scope.edge_ids) | self._bundle_seed_edges.get(view, set())
            if self._candidate is not None:
                affected = self._affected_closure()
                selected_nodes.update(affected & nodes.keys())
                selected_edges.update(affected & edges.keys())
            for ident in selected_edges & edges.keys():
                selected_nodes.update((edges[ident].from_node_id, edges[ident].to_node_id))
            for ident in sorted(selected_nodes & nodes.keys()):
                self._put_summary(view, nodes[ident])
            for ident in sorted(selected_edges & edges.keys()):
                self._put_edge(view, edges[ident])
            if self.phase == 'reconcile':
                self._put_predicate_catalog(view)
            self._set_coverage(view, sorted(self._relation_anchors & nodes.keys()))
        if self.phase == 'reconcile':
            contracts = []
            for node_type in ('concept', 'metric', 'generation_rule', 'event'):
                fields = {name: {'type': descriptor.type, 'required': descriptor.required,
                                'description': descriptor.description}
                          for name, descriptor in self.context.schema.effective_node_fields(node_type).items()}
                contracts.append({'node_type': node_type, 'properties': fields})
                self.write_contracts.add((node_type, 'node_create', '*'))
            self.packet['node_contracts'] = english_contract(contracts)
            for ident in self.write_node_ids:
                node = next(node for node in self.before.graph.nodes if node.id == ident)
                if node.node_type in {'table', 'column', 'dimension_value'}:
                    continue
                for operation in ('node_update',):
                    self.write_contracts.add((node.node_type, operation, ident))
        self.working['bundle_ready'] = True
        self.working['session'] = {'task_id': self.task_id, 'before_hash': self.before_id,
            'candidate_hash': canonical_hash(self._candidate) if self._candidate is not None else None,
            'obligations_hash': canonical_hash([*self.packet['signals'], *self.packet['obligations']])}

    @staticmethod
    def _node_refs(node):
        props = [node.properties, *[GraphManager.resolve_alternative(node, alt).properties for alt in node.alternatives]]
        return set().union(*(property_reference_ids(item) for item in props))

    def _set_coverage(self, view, anchors):
        from cortex.capabilities.graph_merge.reconciliation.scope import signal_scope
        scope = signal_scope(self.context, self._state(view), self._checks)
        coverage = {'view': view, 'snapshot': self._snapshot(view), 'node_ids': anchors,
                    'relation_sets': deepcopy(scope.queries), 'scope': 'signal_sets', 'complete': True}
        row = {'key': canonical_hash(coverage), 'coverage': coverage}
        self.coverage = [item for item in self.coverage if item['coverage'].get('view') != view]
        self.coverage.append(row)
        self.working['coverage'] = deepcopy(self.coverage)

    def _snapshot(self, view):
        return self.before_id if view == 'before' else canonical_hash(self._candidate)

    def _state(self, view):
        if view == 'before':
            return self.before
        if view == 'candidate' and self._candidate is not None:
            return self._candidate
        raise ValueError('Requested graph snapshot is unavailable')

    def set_draft(self, value):
        from cortex.capabilities.graph_merge.reconciliation.contract import DraftDocument
        draft = DraftDocument.model_validate(value)
        if draft.before_id != self.before_id or draft.task_id != self.task_id:
            raise ValueError('Canonical draft has a different task/before binding')
        if draft.version < self.draft.version or (draft.version == self.draft.version and draft != self.draft):
            raise ValueError('VERSION_MISMATCH: canonical head cannot move backwards or change at the same version')
        changed = draft != self.draft
        self.draft = draft
        if changed:
            if self.phase == 'reconcile':
                # Previously compiled objects are historical until this exact
                # new head compiles. Never expose them as the current candidate.
                self.clear_compiled_candidate()
            self.feedback['mechanical'] = []

    def _reset_before_projection(self):
        """Keep fixed signal facts and explicit reads, discard stale draft effects."""
        nodes = {n.id: n for n in self.before.graph.nodes}
        edges = {e.id: e for e in self.before.graph.edges}
        selected_nodes = set(self._signal_scope.node_ids)
        selected_edges = set(self._signal_scope.edge_ids)
        extra_contracts = set()
        for entry in self._journal:
            name, args = entry['name'], entry['args']
            if name == 'read_nodes':
                selected_nodes.update(args['ids'])
                selected_nodes.update(ref for ident in args['ids'] if ident in nodes for ref in self._node_refs(nodes[ident]))
            elif name == 'counterexample' and args.get('view') == 'before':
                for edge in edges.values():
                    if (edge.from_node_id == args.get('from_node_id') and edge.to_node_id == args.get('to_node_id')
                            and edge.edge_type == args.get('edge_type')):
                        selected_edges.add(edge.id)
            elif name == 'read_predicates':
                schema = authoring_schema(self.context.schema, self.before.graph)
                extra_contracts.update(c.id for c in schema.edge_types
                    if any(schema.is_node_type_compatible(args['from_node_type'], t) for t in c.from_node_types)
                    and any(schema.is_node_type_compatible(args['to_node_type'], t) for t in c.to_node_types))
        for ident in selected_edges & edges.keys():
            selected_nodes.update((edges[ident].from_node_id, edges[ident].to_node_id))
        self.working['before'] = {'nodes': {}, 'edges': {}}
        self.visible_node_ids['before'], self.visible_edge_ids['before'] = set(), set()
        self.complete_reads = {row for row in self.complete_reads if row[0] != 'before'}
        for ident in sorted(selected_nodes & nodes.keys()):
            self._put_summary('before', nodes[ident])
        for ident in sorted(selected_edges & edges.keys()):
            self._put_edge('before', edges[ident])
        for ident in sorted(self._signal_scope.contract_ids | extra_contracts):
            self._put_contract('before', ident)
        self.allowed_evidence_ids = {ident for view in ('before', 'candidate')
            for category in ('nodes', 'edges') for row in self.working[view].get(category, {}).values()
            for ident in row.get('evidence_ids', [])}
        self.allowed_evidence_ids.update(ident for row in self._journal if row['name'] == 'read_evidence' for ident in row['args']['ids'])
        self._set_coverage('before', sorted(self._relation_anchors & nodes.keys()))

    def clear_compiled_candidate(self):
        self._candidate, self.candidate_version = None, None
        self.working['candidate'] = {'nodes': {}, 'edges': {}}
        self.visible_node_ids['candidate'], self.visible_edge_ids['candidate'] = set(), set()
        self.working.pop('actual_changes', None)
        self.working.get('session', {})['candidate_hash'] = None
        self.coverage = [row for row in self.coverage if row.get('coverage', {}).get('view') != 'candidate']
        self.complete_reads = {row for row in self.complete_reads if row[0] != 'candidate'}
        self.working['coverage'] = deepcopy(self.coverage)
        self._reset_before_projection()

    def set_compiled_candidate(self, state, version):
        if version != self.draft.version:
            raise ValueError('STALE_CANDIDATE: compiled version differs from draft')
        if self.phase == 'reconcile':
            self._reset_before_projection()
        self._candidate, self.candidate_version = GraphState.model_validate(state), version
        self._initialize_candidate()
        self._finish_bundle()

    def set_feedback(self, value):
        if not isinstance(value, dict):
            raise TypeError('Feedback is a structured object')
        self.feedback.update(deepcopy(value))

    def messages(self):
        from cortex.capabilities.graph_merge.reconciliation.presentation import state_prompt
        return [SystemMessage(self.system), HumanMessage(self._fixed_user_prompt),
                HumanMessage(state_prompt(self))]

    def _public_feedback(self):
        # Full diagnostics and candidate-bound evidence remain in the checkpoint.
        # The model sees actionable current errors and concise previous feedback,
        # never another copy of archived graphs or internal binding ledgers.
        result = {key: deepcopy(value) for key, value in self.feedback.items()
                  if key not in {'semantic_issues', 'semantic_issue_basis', 'historical_compile_feedback', 'historical_draft_feedback'}}
        previous = []
        for item in self.feedback.get('semantic_issues', ()):
            row = {'problem': item.get('problem', ''), 'required_outcome': item.get('required_outcome', ''), 'refs': []}
            for ref in item.get('refs', ()):
                ident = ref['id']
                fields = ref.get('fields') or (None,)
                for field in fields:
                    public = {'view': ref['view'], 'kind': ref['kind'], 'id': ident}
                    if field: public['field'] = field
                    row['refs'].append(public)
            previous.append(row)
        if previous:
            result['previous_feedback'] = previous
        return result

    def premise_hash(self, policy=''):
        from cortex.capabilities.graph_merge.reconciliation.presentation import candidate_prompt, read_prompt
        return canonical_hash({'policy':policy,'fixed_input':self._fixed_user_prompt,
            'actual_candidate':candidate_prompt(self),'read_material':read_prompt(self)})

    def _wire_working(self):
        """Full object comparisons: unchanged content once, edge delta separate."""
        result = {key: deepcopy(value) for key, value in self.working.items()
                  if key not in {'before', 'candidate', 'session'}}
        result['nodes'], result['relations'], result['contracts'] = {}, {}, {}
        before_objects = {kind: {item.id: item for item in getattr(self.before.graph, kind)}
                          for kind in ('nodes', 'edges')}
        after_objects = {kind: {item.id: item for item in getattr(self._candidate.graph, kind)}
                         for kind in ('nodes', 'edges')} if self._candidate is not None else None
        for kind, output in (('nodes', 'nodes'), ('edges', 'relations'), ('contracts', 'contracts')):
            old = self.working['before'].get(kind, {})
            new = self.working['candidate'].get(kind, {})
            for ident in sorted(old.keys() | new.keys()):
                left, right = old.get(ident), new.get(ident)
                if self._candidate is None:
                    result[output][ident] = {'content_change': 'before_only', 'before': deepcopy(left)}
                    continue
                equal = (before_objects[kind].get(ident) == after_objects[kind].get(ident)) if kind != 'contracts' else left == right
                if equal and left is not None and right is not None:
                    result[output][ident] = {'content_change': 'unchanged', 'before': deepcopy(left)}
                else:
                    status = 'created' if left is None else 'deleted' if right is None else 'modified'
                    result[output][ident] = {'content_change': status, 'before': deepcopy(left), 'after': deepcopy(right)}
        return result


    def build_read_tools(self):
        from cortex.capabilities.graph_merge.reconciliation.contract import ReadNodes, ReadEvidence, ReadRelations, ReadPredicates
        @tool(args_schema=ReadNodes)
        def read_nodes(**kwargs):
            """Read complete authorized nodes and their bound before/candidate comparison. Only the current ontology graph is readable."""
            return self.read_nodes(**kwargs)
        @tool(args_schema=ReadRelations)
        def read_relations(**kwargs):
            """Read declared signal relationship sets for nominated core nodes; never expand general adjacency."""
            return self.read_relations(**kwargs)
        @tool(args_schema=ReadEvidence)
        def read_evidence(**kwargs):
            """Read displayed evidence IDs; the controller adds exact bodies once."""
            return self.read_evidence(**kwargs)
        @tool(args_schema=ReadPredicates)
        def read_predicates(**kwargs):
            """Read installed predicate contracts for one business-source/target type pair; no graph traversal or write permission."""
            return self.read_predicates(**kwargs)
        return {item.name: item for item in (read_nodes, read_relations, read_evidence, read_predicates)}

    def available_read_tools(self, tools):
        """Expose a graph read only when authorized local material is missing."""
        names = {'read_evidence'} if self.allowed_evidence_ids - self.evidence_view['id_to_body'].keys() else set()
        if self.phase == 'reconcile': names.add('read_predicates')
        for view in ('before', 'candidate'):
            if view == 'candidate' and self._candidate is None:
                continue
            state = self._state(view)
            for ident in self.visible_node_ids[view]:
                if not self.working[view]['nodes'].get(ident, {}).get('content_complete'):
                    names.add('read_nodes')
            anchors = self._relation_anchors & {node.id for node in state.graph.nodes}
            covered = set()
            for item in self.coverage:
                row = item['coverage']
                if (row.get('view') == view and row.get('snapshot') == self._snapshot(view)
                        and row.get('complete') and row.get('scope') == 'signal_sets'):
                    covered.update(row.get('node_ids', []))
            if anchors - covered:
                names.add('read_relations')
        return {name: tool for name, tool in tools.items() if name in names}

    def _record(self, name, args):
        row = {'name': name, 'args': deepcopy(args)}
        if not any(canonical_hash(old) == canonical_hash(row) for old in self._journal):
            self._journal.append(row)

    @_atomic_read
    def read_nodes(self, ids):
        from cortex.capabilities.graph_merge.reconciliation.contract import ReadNodes
        request = ReadNodes(ids=ids)
        before_hash = self.premise_hash()
        for ident in request.ids:
            found = False
            for view in ('before', 'candidate'):
                if view == 'candidate' and self._candidate is None:
                    continue
                if ident not in self.visible_node_ids[view]:
                    continue
                node = next(node for node in self._state(view).graph.nodes if node.id == ident)
                self._put_summary(view, node)
                for ref in self._node_refs(node):
                    state_nodes = {item.id: item for item in self._state(view).graph.nodes}
                    if ref in state_nodes:
                        self._put_summary(view, state_nodes[ref])
                found = True
            if not found:
                raise ValueError('OUT_OF_SCOPE nodes: ' + ident)
        self._record('read_nodes', {'ids': list(request.ids)})
        return {'status': 'already_provided' if before_hash == self.premise_hash() else 'merged',
                'ids': list(request.ids), 'content_complete': True, 'location': 'Supplementary material in the current work state; fully provided objects retain their fixed task input.'}

    @_atomic_read
    def read_relations(self, node_ids):
        from cortex.capabilities.graph_merge.reconciliation.contract import ReadRelations
        request = ReadRelations(node_ids=node_ids)
        if set(request.node_ids) - self._relation_anchors:
            raise ValueError('OUT_OF_SCOPE: boundary nodes do not authorize recursive relation traversal')
        initial = self.premise_hash()
        all_edges = set()
        for view in ('before', 'candidate'):
            if view == 'candidate' and self._candidate is None:
                continue
            state = self._state(view)
            nodes = {node.id: node for node in state.graph.nodes}
            anchors = set(request.node_ids) & nodes.keys()
            from cortex.capabilities.graph_merge.reconciliation.scope import signal_scope
            scope = signal_scope(self.context, state, self._checks)
            for edge in state.graph.edges:
                if edge.id in scope.edge_ids:
                    self._put_edge(view, edge)
                    self._put_summary(view, nodes[edge.from_node_id])
                    self._put_summary(view, nodes[edge.to_node_id])
                    all_edges.add(edge.id)
            self._set_coverage(view, sorted(anchors))
        self._record('read_relations', {'node_ids': list(request.node_ids), 'checks': deepcopy(self._checks)})
        return {'status': 'already_provided' if initial == self.premise_hash() else 'merged',
            'edge_ids': sorted(all_edges), 'complete': True, 'location': 'Supplementary material in the current work state; fully provided objects retain their fixed task input.'}

    @_atomic_read
    def read_predicates(self, from_node_type, to_node_type):
        from cortex.capabilities.graph_merge.reconciliation.contract import ReadPredicates
        if self.phase != 'reconcile':
            raise ValueError('READONLY_ASSESSMENT: current candidate contracts are supplied automatically')
        request = ReadPredicates(from_node_type=from_node_type,to_node_type=to_node_type)
        before_hash = self.premise_hash()
        predicates=set()
        schema=authoring_schema(self.context.schema,self.before.graph)
        for contract in schema.edge_types:
            if (any(schema.is_node_type_compatible(request.from_node_type,kind) for kind in contract.from_node_types)
                    and any(schema.is_node_type_compatible(request.to_node_type,kind) for kind in contract.to_node_types)):
                self._put_contract('before',contract.id)
                predicates.add(contract.id)
        query=request.model_dump(mode='json')
        self.working.setdefault('predicate_queries',{})[request.from_node_type+'→'+request.to_node_type]={
            **query,'predicate_ids':sorted(predicates)}
        self._record('read_predicates',request.model_dump(mode='json'))
        return {'status':'already_provided' if before_hash==self.premise_hash() else 'merged',
            'predicate_ids':sorted(predicates),'location':'Supplementary predicate contracts in the current work state.'}

    @_atomic_read
    def read_evidence(self, ids):
        unknown = set(ids) - self.allowed_evidence_ids
        if unknown:
            raise ValueError(f'OUT_OF_SCOPE evidence IDs: {sorted(unknown)}')
        if not ids:
            raise ValueError('Provide at least one authorized evidence ID')
        result = EvidencePresentation(self.context).read(ids, refresh=True)
        for row in result['items']:
            body = canonical_hash(row['content'])
            self.evidence_view['bodies'].setdefault(body, deepcopy(row['content']))
            for ident in row.get('evidence_ids', [row['id']]):
                if ident in self.evidence_view['id_to_body'] and self.evidence_view['id_to_body'][ident] != body:
                    raise ValueError('EVIDENCE_CHANGED: an immutable evidence ID changed content')
                self.evidence_view['id_to_body'][ident] = body
        self._record('read_evidence', {'ids': list(ids)})
        return {'status': 'merged', 'ids': ids, 'missing_ids': result['missing_ids']}

    def require_complete_read(self, node_id, fields, view='before'):
        snapshot = self._snapshot(view)
        for field in fields:
            if (view, snapshot, node_id, field) not in self.complete_reads:
                raise ValueError(f'MISSING_REQUIRED_READ: {node_id} {view} {field}')

    def require_write_contract(self, node_type, operation_kind, owner_id=None):
        key = (node_type, operation_kind, owner_id or '*')
        if key not in self.write_contracts:
            raise ValueError(f'MISSING_WRITE_CONTRACT: {node_type} {operation_kind} {owner_id or "new"}')

    def validate_submission(self, draft):
        from cortex.capabilities.graph_merge.reconciliation.contract import DraftDocument
        from cortex.capabilities.graph_merge.graph.source_units import PatchContractError
        value = DraftDocument.model_validate(draft)
        if value != self.draft or value.before_id != self.before_id:
            raise ValueError('STALE_DRAFT: this is not the current controller-bound head')
        edges = {edge.id: edge for edge in self.before.graph.edges}
        allowed_nodes = self.visible_node_ids['before'] | self.visible_node_ids['candidate']
        new_nodes = {row.node_id for row in value.changes if row.kind == 'node_create'}
        new_edges = {row.edge_id for row in value.changes if row.kind == 'edge_create'}
        declared = {row.contract.id for row in value.changes if row.kind == 'relation_declare'}
        contracts = {key: row for view in ('before','candidate') for key,row in self.working[view].get('contracts',{}).items()}
        for row in value.changes:
            def reject(field, ident):
                raise PatchContractError('OUT_OF_SCOPE', owner_id=getattr(row,'node_id',getattr(row,'edge_id',None)),
                    path=f'changes.{row.change_id}.{field}', change_id=row.change_id, object_ids=[ident],
                    required_action='Use authorized local objects or create the justified new object in this draft.')
            if row.kind in {'node_update'}:
                if row.node_id not in self.write_node_ids: reject('node_id',row.node_id)
                fields=list(row.set.model_fields_set-{'properties'})+['properties.'+key for key in row.set.properties]
                fields.extend(row.unset)
                if row.node_id in getattr(self._signal_scope, 'metadata_only_node_ids', set()):
                    if set(fields) - {'properties.relation_metadata'}:
                        reject('set', row.node_id)
                    owner = next(n for n in self.before.graph.nodes if n.id == row.node_id)
                    original = owner.properties.get('relation_metadata', [])
                    replacement = ([] if 'properties.relation_metadata' in row.unset else
                                   row.set.properties.get('relation_metadata', original))
                    selected = set(self._signal_scope.edge_ids)
                    protected = sorted(canonical_hash(g) for g in original if not set(g.get('edges', ())) & selected)
                    retained = sorted(canonical_hash(g) for g in replacement if not set(g.get('edges', ())) & selected)
                    if protected != retained:
                        reject('properties.relation_metadata', row.node_id)
                    if any(set(g.get('edges', ())) - selected - new_edges for g in replacement
                           if set(g.get('edges', ())) & selected):
                        reject('properties.relation_metadata.edges', row.node_id)
                self.require_complete_read(row.node_id,fields)
            elif row.kind=='node_create':
                self.require_write_contract(row.node_type,'node_create')
            elif row.kind in {'edge_update','edge_replace','edge_remove'}:
                if row.edge_id not in self.write_edge_ids: reject('edge_id',row.edge_id)
                if row.kind=='edge_replace' and row.replacement_edge_id not in self.write_edge_ids | new_edges:
                    reject('replacement_edge_id',row.replacement_edge_id)
                if row.kind=='edge_update':
                    self._require_predicate(row.set.edge_type or edges[row.edge_id].edge_type,contracts,declared)
                    for field in ('from_node_id','to_node_id'):
                        ident=getattr(row.set,field)
                        if ident is not None and ident not in allowed_nodes | new_nodes: reject('set.'+field,ident)
            elif row.kind=='edge_create':
                if not ({row.from_node_id, row.to_node_id} & ((self.write_node_ids - getattr(self._signal_scope, 'metadata_only_node_ids', set())) | new_nodes)):
                    reject('endpoints', row.from_node_id + ' -> ' + row.to_node_id)
                self._require_predicate(row.edge_type,contracts,declared)
                for field in ('from_node_id','to_node_id'):
                    if getattr(row,field) not in allowed_nodes | new_nodes: reject(field,getattr(row,field))
            def private(item):
                if isinstance(item,dict):
                    if set(item)&{'sources','common_sources','reference_origin','overrides','_provenance'}:
                        raise ValueError('INTERNAL_WRITE_FIELD: controller provenance is read-only')
                    for child in item.values(): private(child)
                elif isinstance(item,(list,tuple)):
                    for child in item: private(child)
            private(row.model_dump(mode='json',exclude_unset=True))
        return value

    @staticmethod
    def _require_predicate(ident, readable_contracts, declared_contracts):
        if ident in declared_contracts:
            return
        if ident not in readable_contracts:
            raise ValueError(f'MISSING_PREDICATE_CONTRACT: {ident}; use read_predicates(from_node_type,to_node_type) for the intended endpoints, or declare a genuinely new predicate. A hidden contract is not permission to redefine an installed one.')
        if not readable_contracts[ident]['authorable']:
            raise ValueError(f'READONLY_PREDICATE: {ident} is not an installed concrete authoring contract')


    def resolve_reference(self, reference):
        from cortex.capabilities.graph_merge.reconciliation.contract import ObjectReference, InternalReference
        from cortex.capabilities.graph_merge.reconciliation.projection import field_value
        ref = ObjectReference.model_validate(reference)
        ident=ref.id
        if ref.view=='candidate' and (self._candidate is None or self.candidate_version!=self.draft.version):
            raise ValueError('STALE_CANDIDATE: no compiled candidate for this draft')
        category={'node':'nodes','edge':'edges','contract':'contracts'}[ref.kind]
        row=self.working[ref.view].get(category,{}).get(ident)
        if row is None:raise ValueError('UNPROVIDED_REFERENCE_OBJECT: '+ident)
        field_value(row,ref.field)
        return InternalReference(kind=ref.kind,id=ident,view=ref.view,fields=(ref.field,) if ref.field else ())

    def _resolve_stored_reference(self, ref):
        from cortex.capabilities.graph_merge.reconciliation.contract import ObjectReference
        ident=ref.id
        for field in ref.fields or (None,):
            self.resolve_reference(ObjectReference(kind=ref.kind,id=ident,view=ref.view,field=field))

    def _validate_missing(self, ref):
        from cortex.capabilities.graph_merge.consolidation.review import AbsenceClaim, absence_counterexamples, ReviewFactError
        if {ref.from_node_id,ref.to_node_id}-self.visible_node_ids[ref.view]:
            raise ValueError('OUT_OF_SCOPE: missing-relation claim uses unknown endpoints')
        if ref.alternative_id:
            owner=next(n for n in self._state(ref.view).graph.nodes if n.id==ref.from_node_id)
            if ref.alternative_id not in {a.alternative_id for a in owner.alternatives}:
                raise ValueError('UNKNOWN_SOURCE_ALTERNATIVE')
        claim=AbsenceClaim(kind='relation_absent',view=ref.view,from_node_id=ref.from_node_id,
            to_node_id=ref.to_node_id,edge_type=ref.edge_type,alternative_id=ref.alternative_id)
        found=absence_counterexamples(self.context,claim,before=self.before,candidate=self._candidate)
        if found:
            self._merge_counterexample(claim,found)
            raise ReviewFactError('Missing relation claim is contradicted by the actual graph',counterexamples=found)
        return claim.model_dump(mode='json')

    def validate_review(self, value):
        from cortex.capabilities.graph_merge.reconciliation.contract import (
            AssessmentResult,AssessmentRecord,FeedbackIssue,InternalReference,MissingRelationReference)
        if not self.working.get('bundle_ready'): raise ValueError('BUNDLE_NOT_READY')
        if self._candidate is None or self.candidate_version!=self.draft.version:
            raise ValueError('STALE_CANDIDATE: no actual candidate for this draft')
        current_hash=canonical_hash(self._candidate)
        if canonical_hash(getattr(self.context,'candidate',None))!=current_hash:
            raise ValueError('STALE_CANDIDATE: actual graph changed after this request')
        if isinstance(value,AssessmentRecord):
            if value.expected_version!=self.draft.version or value.candidate_hash!=current_hash:
                raise ValueError('STALE_ASSESSMENT: record addresses a different candidate')
            for issue in value.issues:
                for ref in issue.refs: self._resolve_stored_reference(ref)
                for claim in issue.claims:
                    self._validate_missing(MissingRelationReference(kind='missing_relation',**{
                        key:claim[key] for key in ('view','from_node_id','to_node_id','edge_type','alternative_id') if claim.get(key) is not None}))
            return value
        result=AssessmentResult.model_validate(value)
        issues=[]
        relevant=self._affected_closure() | self._relation_anchors | set(getattr(getattr(self.context,'active_task',None),'targets',()))
        relevant.update(self._signal_scope.node_ids | self._signal_scope.edge_ids)
        for item in result.feedback:
            refs,claims=[],[]
            for ref in item.refs:
                if ref.kind=='missing_relation':
                    claims.append(self._validate_missing(ref))
                    refs.extend(InternalReference(kind='node',id=ident,view=ref.view) for ident in (ref.from_node_id,ref.to_node_id))
                else: refs.append(self.resolve_reference(ref))
            if not any(ref.view!='source' and ref.id in relevant for ref in refs):
                raise ValueError('Feedback must concern the given checks or actual changed subgraph')
            issues.append(FeedbackIssue(issue_id='feedback-'+canonical_hash(item)[:16],refs=refs,claims=claims,
                problem=item.problem,required_outcome=item.required_outcome))
        return AssessmentRecord(expected_version=self.draft.version,explanation=result.explanation,
            issues=issues,candidate_hash=current_hash)

    def _affected_closure(self):
        """Actual deltas and their semantic consumers, not endpoint neighborhoods."""
        from cortex.capabilities.graph_merge.graph.impact import modification_scope
        if self._candidate is None:
            return set()
        nodes, edges = modification_scope(self.before, self._candidate)
        return nodes | edges

    def _merge_counterexample(self, claim, found):
        state = self._state(claim.view)
        nodes, edges = {n.id: n for n in state.graph.nodes}, {e.id: e for e in state.graph.edges}
        for row in found:
            edge = edges[row['id']]
            self._put_edge(claim.view, edge)
            for ident in (edge.from_node_id, edge.to_node_id): self._put_summary(claim.view, nodes[ident])
        self._record('counterexample', claim.model_dump(mode='json'))


    def snapshot(self):
        return {'version': 3, 'before_id': self.before_id, 'task_id': self.task_id,
            'draft': self.draft.model_dump(mode='json', exclude_unset=True), 'working': deepcopy(self.working),
            'evidence_view': deepcopy(self.evidence_view), 'coverage': deepcopy(self.coverage),
            'complete_reads': [list(row) for row in sorted(self.complete_reads)],
            'write_contracts': [list(row) for row in sorted(self.write_contracts)], 'feedback': deepcopy(self.feedback),
            'phase': self.phase, 'candidate_version': self.candidate_version, 'candidate_hash': canonical_hash(self._candidate) if self._candidate else None,
            'journal': deepcopy(self._journal), 'signals_hash': canonical_hash([self.packet['signals'], self.packet['obligations']])}

    def restore(self, saved):
        """Rebuild permissions by replay; a durable TaskState head always wins."""
        required = {'version', 'before_id', 'task_id', 'phase', 'signals_hash', 'draft',
                    'candidate_hash', 'working', 'evidence_view', 'complete_reads',
                    'write_contracts', 'journal', 'feedback'}
        if (not isinstance(saved, dict) or not required <= saved.keys()
                or saved['version'] != 3 or saved['before_id'] != self.before_id):
            raise ValueError('Saved reconciliation context has incompatible bindings')
        if (saved['phase'] != self.phase or saved['task_id'] != self.task_id
                or saved['signals_hash'] != canonical_hash([self.packet['signals'], self.packet['obligations']])):
            raise ValueError('Saved reconciliation task/signal binding differs')
        from cortex.capabilities.graph_merge.reconciliation.contract import DraftDocument
        from cortex.capabilities.graph_merge.consolidation.review import AbsenceClaim
        old_draft = DraftDocument.model_validate(saved['draft'])
        current = self.draft
        if old_draft.before_id != self.before_id or old_draft.task_id != current.task_id:
            raise ValueError('Saved draft addresses another immutable task')
        if self._draft_authoritative:
            if old_draft.version > current.version or (old_draft.version == current.version and old_draft != current):
                raise ValueError('Saved canonical draft conflicts with the authoritative current head')
        else:
            current = old_draft
        head_advanced = current.version > old_draft.version
        controller_candidate = getattr(self.context, 'candidate', None)
        controller_hash = canonical_hash(controller_candidate) if controller_candidate is not None else None
        saved_hash = saved['candidate_hash']
        candidate_matches = saved_hash is not None and saved_hash == controller_hash
        if self.phase == 'graph_assess' and (saved_hash != controller_hash or head_advanced):
            raise ValueError('Saved assessment candidate is stale')
        # A Writer can recover fixed-before facts even when compilation saved a
        # newer candidate before the dialogue checkpoint was persisted. Old
        # candidate facts never become assertions about the new candidate.
        drop_candidate = self.phase == 'reconcile' and not candidate_matches
        original = getattr(self.context, 'reconciliation_runtime', None)
        try:
            staged = ReconciliationRuntime(self.context, self.payload, self.phase, system=self.system)
            staged.draft = current
            if candidate_matches:
                version = saved.get('candidate_version')
                if type(version) is not int or version < 0 or version > old_draft.version:
                    raise ValueError('Saved candidate lacks its compiled draft version')
                staged._candidate, staged.candidate_version = controller_candidate, version
                # Assessment construction already built the exact candidate,
                # including signal-specific read-only predicate contracts.
                # Reinitializing it here would erase those authorized facts.
                if self.phase == 'reconcile':
                    staged._initialize_candidate()
                    staged._finish_bundle()
            for entry in saved['journal']:
                if (not isinstance(entry, dict) or entry.get('name') not in
                        {'read_nodes', 'read_evidence', 'read_relations', 'read_predicates', 'counterexample'}
                        or not isinstance(entry.get('args'), dict)):
                    raise ValueError('Saved state contains an invalid pure-read journal')
                name, args = entry['name'], deepcopy(entry['args'])
                if drop_candidate and args.get('view') == 'candidate':
                    continue
                if name == 'counterexample':
                    claim = AbsenceClaim.model_validate(args)
                    from cortex.capabilities.graph_merge.consolidation.review import absence_counterexamples
                    endpoints = {claim.to_node_id} | ({claim.from_node_id} if claim.from_node_id else set())
                    if endpoints - staged.visible_node_ids[claim.view]:
                        raise ValueError('Saved counterexample has unauthorized anchors')
                    found = absence_counterexamples(self.context, claim, before=self.before, candidate=staged._candidate)
                    if not found:
                        raise ValueError('Saved mechanical counterexample no longer holds')
                    staged._merge_counterexample(claim, found)
                else:
                    if name == 'read_relations':
                        if args.pop('checks', None) != staged._checks:
                            raise ValueError('Saved relation read has a different signal scope')
                    if name == 'read_evidence' and drop_candidate:
                        args['ids'] = [ident for ident in args['ids'] if ident in staged.allowed_evidence_ids]
                        if not args['ids']:
                            continue
                    getattr(staged, name)(**args)

            def contained(expected, actual):
                if isinstance(expected, dict):
                    return isinstance(actual, dict) and all(key in actual and contained(value, actual[key]) for key, value in expected.items())
                return expected == actual

            if not head_advanced and not drop_candidate:
                if (canonical_hash(staged.working) != canonical_hash(saved['working'])
                        or canonical_hash(staged.evidence_view) != canonical_hash(saved['evidence_view'])
                        or staged.complete_reads != {tuple(row) for row in saved['complete_reads']}
                        or staged.write_contracts != {tuple(row) for row in saved['write_contracts']}):
                    raise ValueError('Restored facts/permissions differ from exact authorized source reads')
            else:
                for view in ('before', 'source'):
                    if not contained(saved['working'].get(view, {}), staged.working.get(view, {})):
                        raise ValueError('Saved immutable facts differ from authorized source reads')
                required_reads = {tuple(row) for row in saved['complete_reads'] if row[0] != 'candidate'}
                if not required_reads <= staged.complete_reads:
                    raise ValueError('Saved immutable read permissions cannot be reconstructed')
                # Each retained evidence binding is recomputed from its real
                # source, rather than trusted from the cached body dictionary.
                for ident, digest in staged.evidence_view['id_to_body'].items():
                    if ident in saved['evidence_view']['id_to_body'] and saved['evidence_view']['id_to_body'][ident] != digest:
                        raise ValueError('Saved evidence body changed under the same ID')
            current_feedback = deepcopy(self.feedback)
            staged.feedback = deepcopy(saved['feedback'])
            for key in ('semantic_issues', 'semantic_issue_basis'):
                staged.feedback[key] = current_feedback.get(key, [])
            if head_advanced:
                staged.feedback['historical_draft_feedback'] = {
                    'draft_version': old_draft.version, 'mechanical': saved['feedback'].get('mechanical'),
                    'protocol_error': saved['feedback'].get('protocol_error')}
                staged.feedback['mechanical'] = current_feedback.get('mechanical', [])
                staged.feedback['protocol_error'] = None
            elif current_feedback.get('mechanical'):
                staged.feedback['mechanical'] = current_feedback['mechanical']
            staged.recovery_head_advanced = head_advanced
            self.__dict__.update(staged.__dict__)
        finally:
            self.context.reconciliation_runtime = original or self



ReconciliationDialogue = ReconciliationRuntime
