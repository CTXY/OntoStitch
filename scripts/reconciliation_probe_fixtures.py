"""Readonly case-graph fixture builders and probe evidence utilities.

Execution uses run_relation_reconciliation_v2_smoke.py; no v1 runtime protocol.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.context import remap_edge
from cortex.capabilities.graph_merge.integration.compiler import deduplicate_edges
from cortex.capabilities.graph_merge.integration.patch import MergePatch, _integrate
from cortex.capabilities.graph_merge.integration.task_state import canonical_hash
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import ConflictChoice, ConflictPoint, GraphSpace, GraphState, NodeContent, load_root_schema

# The sibling helpers read data only until configuration/observe are called.
from run_node_consolidation_smoke import project_case

REPO = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def content(node):
    return NodeContent(name=node.name, description=node.description, aliases=node.aliases,
        tags=node.tags, properties=deepcopy(node.properties))


@dataclass
class Fixture:
    name: str
    before: GraphState
    expected: GraphState
    evidence: tuple[EvidenceItem, ...]
    signal: dict
    changes: list[dict]
    provenance: dict
    read_node_ids: list[str]
    write_node_id: str | None = None
    write_kind: str | None = None

    @property
    def targets(self):
        return set(self.signal.get('node_ids', ())) | set(self.signal.get('edge_ids', ()))


def evidence_for(state, rows):
    sources = {source for node in state.graph.nodes for source in
        (*node.sources, *node.common_sources, *(source for alternative in node.alternatives for source in alternative.sources))}
    sources.update(source for edge in state.graph.edges for source in edge.sources)
    return tuple(EvidenceItem(id='sql-training:' + source.source_id, source=source, case_id=source.case_id,
        content=rows[source.source_id]) for source in sorted(sources, key=lambda source: source.model_dump_json())
        if source.source_id in rows)


def case_data(args, database, case_id):
    path = args.graph_root / database / 'case_level_graphs' / str(case_id) / 'state.json'
    state = GraphState.model_validate(read(path))
    source = read(args.source_input_root / database / 'cases' / str(case_id) / 'input.json')
    rows = {source['source_id']: {key: source[key] for key in ('question', 'sql', 'evidence') if key in source}}
    return state, rows, {str(path.resolve()): digest(path)}


def select_healthy(args):
    """Bounded discovery of real parallel formula roles, not a dataset-wide test."""
    root = args.graph_root / 'thrombosis_prediction/case_level_graphs'
    for path in sorted(root.glob('*/state.json'))[:args.discovery_limit]:
        state = GraphState.model_validate(read(path))
        nodes = {node.id: node for node in state.graph.nodes}
        grouped = defaultdict(list)
        for edge in state.graph.edges:
            if edge.edge_type == 'depends_on' and nodes[edge.from_node_id].node_type == 'metric':
                grouped[edge.from_node_id, edge.to_node_id].append(edge)
        for (metric_id, target), edges in grouped.items():
            refs = nodes[metric_id].properties.get('expression', {}).get('refs', {})
            if len(edges) < 2 or list(refs.values()).count(target) < 2:
                continue
            if len({json.dumps(edge.properties, sort_keys=True) for edge in edges}) != len(edges):
                continue
            fragment, metric = project_case(state, metric_id, path.parent.name)
            GraphManager(load_root_schema()).validate_state(fragment)
            original, rows, hashes = case_data(args, 'thrombosis_prediction', path.parent.name)
            current = [edge for edge in fragment.graph.edges if edge.from_node_id == metric]
            signal = {'id': 'healthy-parallel', 'kind': 'parallel_assertions', 'mode': 'suspect',
                'node_ids': [metric, 'case' + path.parent.name + '::' + target], 'edge_ids': [edge.id for edge in current],
                'observed': {'parallel_edge_ids': [edge.id for edge in current], 'distinct_role_descriptions': True},
                'check_question': '核查同一公式输入的多种角色是否已完整保留；不因平行边强行修改。'}
            return Fixture('healthy_parallel', fragment, fragment, evidence_for(fragment, rows), signal, [],
                {'source_sha256': hashes, 'case_ids': ['thrombosis_prediction/' + path.parent.name],
                 'selection': 'Automatically discovered distinct descriptions for one operand used in multiple formula positions.',
                 'controlled_changes': [], 'expected_outcome': 'Healthy no-op is legal; deterministic probe keeps all original roles.'},
                signal['node_ids'])
    raise ValueError('No healthy real parallel operand found within bounded discovery limit')


def select_abstract(args):
    state, rows, hashes = case_data(args, 'thrombosis_prediction', '1164')
    contracts = {contract.id: contract for contract in state.graph.relation_contracts}
    nodes = {node.id: node for node in state.graph.nodes}
    chosen = next(edge for edge in state.graph.edges if edge.edge_type in contracts
        and contracts[edge.edge_type].extends == 'has_attribute'
        and nodes[edge.from_node_id].node_type in {'concept', 'event'})
    expected, start = project_case(state, chosen.from_node_id, '1164')
    selected = next(edge for edge in expected.graph.edges if edge.id == 'case1164::' + chosen.id)
    changed = tuple(edge.model_copy(update={'edge_type': 'has_attribute'}) if edge.id == selected.id else edge
        for edge in expected.graph.edges)
    before = expected.model_copy(update={'graph': expected.graph.model_copy(update={'edges': changed})})
    signal = {'id': 'abstract-specific-role', 'kind': 'abstract_predicate', 'mode': 'required',
        'node_ids': [selected.from_node_id, selected.to_node_id], 'edge_ids': [selected.id],
        'observed': {'edge_id': selected.id, 'abstract_predicate': 'has_attribute', 'available_original_predicate': selected.edge_type},
        'check_question': '读取真实业务角色与已安装契约，纠正受控注入的抽象谓词，不改节点含义。'}
    return Fixture('abstract_predicate', before, expected, evidence_for(before, rows), signal,
        [{'change_id': 'restore-specific-predicate', 'kind': 'edge_update', 'edge_id': selected.id,
            'set': {'edge_type': selected.edge_type}, 'reason': 'Restore the evidenced original concrete attribute role.'}],
        {'source_sha256': hashes, 'case_ids': ['thrombosis_prediction/1164'],
         'controlled_changes': [{'edge_id': selected.id, 'field': 'edge_type', 'original': selected.edge_type, 'injected': 'has_attribute'}],
         'expected_outcome': 'Only the injected predicate changes back; all node contents and sources remain.'}, signal['node_ids'])


def real_metric_fixture(path):
    """Historical Metric-only coupled repair, not final post-consolidation reconciliation."""
    raw = read(path)
    original = GraphState.model_validate(raw['before'])
    evidence = tuple(EvidenceItem.model_validate(row) for row in raw['evidence'])
    patch = MergePatch.model_validate(raw['node_draft'])
    context = SimpleNamespace(evidence=evidence)
    before, *_ = _integrate(original, list(patch.integrate_groups), [raw['group']], context)
    retirement = raw['coupled_record']['patch']['retirements'][0]
    removed, survivor = retirement['before'][0]['id'], retirement['after'][0]['id']
    edges = {edge.id: edge for edge in before.graph.edges}
    retained = edges[survivor].model_copy(update={'sources': tuple(dict.fromkeys((*edges[survivor].sources, *edges[removed].sources)))})
    expected = before.model_copy(update={'graph': before.graph.model_copy(update={'edges': tuple(
        retained if edge.id == survivor else edge for edge in before.graph.edges if edge.id != removed)})})
    GraphManager(load_root_schema()).validate_state(expected)
    metric = raw['group']['owner_id']
    signal = {'id': 'real-metric-retirement', 'kind': 'metric_direct_inputs', 'mode': 'required',
        'node_ids': [metric, edges[removed].to_node_id, edges[survivor].to_node_id], 'edge_ids': [removed, survivor],
        'observed': {'stale_edge_ids': [removed], 'original_retirement_evidence': retirement['explanation']},
        'check_question': '核查保存的真实直接依赖退役及其语义后继，保留公式、端点概念和两份来源。'}
    return Fixture('metric_retirement', before, expected, evidence, signal,
        [{'change_id': 'replace-stale-direct-input', 'kind': 'edge_replace', 'edge_id': removed,
          'replacement_edge_id': survivor, 'reason': retirement['explanation']}],
        {'source_sha256': {str(Path(path).resolve()): digest(path)}, 'case_ids': ['toxicology/266', 'toxicology/204'],
         'controlled_changes': [],
         'test_stage': 'node_consolidation_coupled_repair', 'final_reconciliation_coverage': False,
         'consolidated_groups': [raw['group']],
         'unconsolidated_concept_ids': sorted({edges[removed].to_node_id, edges[survivor].to_node_id}),
         'selection': 'Historical regression after consolidating only the Metric group. Concept consolidation was not run; this is not final reconciliation input.',
         'expected_outcome': 'Retire only the stale direct edge with a real successor; preserve both exact sources.',
         'archived_original': raw['before']}, signal['node_ids'])


def combine_concept_cases(args, database, cases, selected):
    """Curated baseline only; the reconciliation Agent never decides identity."""
    fragments, originals, rows, hashes = [], [], {}, {}
    for case in cases:
        state, evidence_rows, source_hashes = case_data(args, database, case)
        fragment, ident = project_case(state, selected, case)
        fragments.append(fragment); originals.append(next(node for node in fragment.graph.nodes if node.id == ident))
        rows.update(evidence_rows); hashes.update(source_hashes)
    first = originals[0]
    mapping = {node.id: first.id for node in originals}
    nodes, edges = {}, []
    for fragment in fragments:
        for node in fragment.graph.nodes:
            if node.id not in mapping:
                nodes.setdefault(node.id, node)
        edges.extend(remap_edge(edge, mapping) for edge in fragment.graph.edges)
    # These source cases have compatible ordinary Concepts with simple target groups.
    properties = deepcopy(first.properties)
    groups, expressions, fingerprints = [], {}, set()
    for node in originals:
        for index, group in enumerate(node.properties['targets']):
            expression = node.properties.get('target_expressions', {}).get(str(index))
            fingerprint = json.dumps([group, expression], sort_keys=True)
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            if expression is not None:
                expressions[str(len(groups))] = expression
            groups.append(deepcopy(group))
    properties['targets'] = groups
    properties['target_expressions'] = expressions
    for key in ('definition',):
        values = list(dict.fromkeys(node.properties[key] for node in originals if node.properties.get(key)))
        if values:
            properties[key] = '\n'.join(values)
    ordinary = first.model_copy(update={'properties': properties,
        'description': '\n'.join(dict.fromkeys(node.description for node in originals if node.description)),
        'aliases': list(dict.fromkeys(alias for node in originals for alias in node.aliases)),
        'tags': list(dict.fromkeys(tag for node in originals for tag in node.tags)),
        'sources': tuple(dict.fromkeys(source for node in originals for source in node.sources))})
    nodes[first.id] = ordinary
    deduplicated, _ = deduplicate_edges(edges)
    state = GraphState(graph=GraphSpace(nodes=tuple(nodes.values()), edges=tuple(deduplicated.values())))
    GraphManager(load_root_schema()).validate_state(state)
    return state, ordinary, originals, rows, hashes


def metadata_fixture(args):
    baseline, node, originals, rows, hashes = combine_concept_cases(args, 'toxicology', ('300', '229'), 'concept.molecule')
    bindings = [edge for edge in baseline.graph.edges if edge.from_node_id == node.id and edge.edge_type == 'grounded_on']
    if len(bindings) != 2:
        raise ValueError('Metadata boundary needs exactly the two evidenced molecule representations')
    group = {'id': 'molecule-representations', 'edges': [edge.id for edge in bindings],
        'description': 'These relations retain the molecule identity representation in the molecule table and its representation in bond records.'}
    properties = {**node.properties, 'relation_metadata': [group]}
    annotated = node.model_copy(update={'properties': properties})
    before_nodes = tuple(annotated if item.id == node.id else item for item in baseline.graph.nodes)
    old = bindings[-1].id
    new = 'smoke-renamed::' + old
    renamed = tuple(edge.model_copy(update={'id': new}) if edge.id == old else edge for edge in baseline.graph.edges)
    before = baseline.model_copy(update={'graph': baseline.graph.model_copy(update={'nodes': before_nodes, 'edges': renamed})})
    updated = {**group, 'edges': [new if key == old else key for key in group['edges']]}
    fixed = annotated.model_copy(update={'properties': {**properties, 'relation_metadata': [updated]}})
    expected = before.model_copy(update={'graph': before.graph.model_copy(update={'nodes': tuple(
        fixed if item.id == node.id else item for item in before.graph.nodes)})})
    GraphManager(load_root_schema()).validate_state(expected)
    signal = {'id': 'metadata-alias', 'kind': 'concept_metadata_repair', 'mode': 'required',
        'node_ids': [node.id], 'edge_ids': [new, bindings[0].id],
        'observed': {'metadata_id': group['id'], 'missing_edge_id': old, 'trusted_edge_aliases': {old: new},
                     'original_edge_record': bindings[-1].model_dump(mode='json')},
        'check_question': '同步真实边ID映射，完整保留原组的两种表示含义；不能删除metadata来绕过引用错误。'}
    return Fixture('metadata_alias', before, expected, evidence_for(before, rows), signal,
        [{'change_id': 'repair-metadata-alias', 'kind': 'node_update', 'node_id':node.id, 'set':{'properties':{'relation_metadata':[updated]}},
            'reason':'Map only the declared edge alias and preserve the complete group meaning.'}],
        {'source_sha256': hashes, 'case_ids': ['toxicology/300', 'toxicology/229'],
         'controlled_changes': ['Curated same-identity baseline retains both original Concept contents and physical representations.',
             'Added a group restating the two existing representation roles, without introducing ordering/exclusivity.',
             {'renamed_edge': old, 'new_edge': new, 'metadata_deliberately_not_synced': True}],
         'expected_outcome': 'Only the stale metadata reference changes; the group and both real relations remain.'}, [node.id], node.id, 'node_update')


def preserve_conflict_fixture(args):
    ordinary_state, ordinary, originals, rows, hashes = combine_concept_cases(args, 'toxicology', ('266', '204'), 'concept.carcinogenic_molecule')
    branches = [('representation-' + str(index), content(node), node.sources) for index, node in enumerate(originals)]
    point = ConflictPoint(point_id='controlled-representation-split', aspect='matching representation',
        description='Controlled legacy over-fragmentation: these complete source meanings are compatible, not a claimed source-data contradiction.',
        choices=tuple(ConflictChoice(alternative_ids=(key,), statement='Preserve this complete original matching representation.') for key, _, _ in branches))
    conflicted, _ = GraphManager.compact_conflict(ordinary, branches, conflict_points=(point,), consolidation_status='semantic_conflict')
    # Associate each source-specific assertion with its exact input representation.
    scoped = []
    for edge in ordinary_state.graph.edges:
        if edge.from_node_id == ordinary.id:
            supported = [key for (key, _, sources) in branches if any(source.source_id == edge_source.source_id
                for source in sources for edge_source in edge.sources)]
            edge = edge.model_copy(update={'properties': {**edge.properties, 'alternative_ids': supported}})
        scoped.append(edge)
    before = ordinary_state.model_copy(update={'graph': ordinary_state.graph.model_copy(update={
        'nodes': tuple(conflicted if node.id == ordinary.id else node for node in ordinary_state.graph.nodes), 'edges': tuple(scoped)})})
    GraphManager(load_root_schema()).validate_state(before)
    signal={'id':'preserve-existing-conflict','kind':'parallel_assertions','mode':'suspect',
        'node_ids':[ordinary.id],'edge_ids':[edge.id for edge in scoped if edge.from_node_id==ordinary.id],
        'observed':{'existing_branch_scopes':'Retain every accepted complete branch and its valid relations'},
        'check_question':'核查现有分支关系；正确则保持。不得选择分支或将 conflict 普通化。'}
    return Fixture('preserve_conflict',before,before,evidence_for(before,rows),signal,[],
        {'source_sha256':hashes,'case_ids':['toxicology/266','toxicology/204'],
         'controlled_changes':['Combine real same-identity source records, retaining complete representations and their branch-specific relations.',
             'Encode compatible original representations as an existing conflict for a preservation test; this is not a real contradictory source-data case.'],
         'expected_outcome':'Preserve every complete branch, source and branch-specific edge without normalization.'},
        [ordinary.id])


def semantic_oracle(actual, fixture):
    """Deterministic oracle only: a model accept alone cannot mark the probe passed."""
    GraphManager(load_root_schema()).validate_state(actual)
    def business(state):
        nodes = {node.id: node.model_dump(mode='json', exclude={'sources', 'common_sources', 'merge_status',
            'consolidation_status', 'preservation_reasons'}) for node in state.graph.nodes}
        edges = {edge.id: edge.model_dump(mode='json', exclude={'sources', 'reference_origin'}) for edge in state.graph.edges}
        return nodes, edges
    if business(actual) != business(fixture.expected):
        raise AssertionError('Actual candidate differs from the explicit controlled-fixture semantic oracle')
    expected_sources = {('node', node.id): set(node.sources) for node in fixture.expected.graph.nodes}
    expected_sources.update({('edge', edge.id): set(edge.sources) for edge in fixture.expected.graph.edges})
    for kind, objects in [('node', actual.graph.nodes), ('edge', actual.graph.edges)]:
        for item in objects:
            if not expected_sources[kind, item.id] <= set(item.sources):
                raise AssertionError(f'Candidate lost expected sources on {item.id}')
    return {'strict_graph_validation': True, 'expected_business_graph': True, 'source_preservation': True}


def live_invariant_oracle(actual, fixture):
    """Mechanical invariants only; independent Assessment owns semantic equivalence.

    Unlike the deterministic oracle, this permits different edge IDs and equal-
    meaning prose. Novel predicates/organizations are flagged for human review.
    """
    GraphManager(load_root_schema()).validate_state(actual)
    actual_nodes = {node.id: node for node in actual.graph.nodes}
    expected_nodes = {node.id: node for node in fixture.expected.graph.nodes}
    manual = []
    if set(expected_nodes) - actual_nodes.keys():
        raise AssertionError('A protected fixture node disappeared during a relation-only probe')
    if actual_nodes.keys() - expected_nodes.keys():
        manual.append('Additional nodes need manual semantic review; no new identity was required by this fixture.')
    for ident, expected in expected_nodes.items():
        current = actual_nodes[ident]
        if current.node_type != expected.node_type:
            raise AssertionError('Protected node type changed')
        if not set(expected.sources) <= set(current.sources):
            raise AssertionError('A protected node lost its original sources')
        if current.properties != expected.properties:
            manual.append('Protected node content changed: ' + ident)
        if (current.name, current.aliases, current.tags) != (expected.name, expected.aliases, expected.tags):
            manual.append('Node labeling changed and needs semantic review: ' + ident)
    from cortex.schema.relation_vocabulary import ABSTRACT_RELATIONS
    for expected in fixture.expected.graph.edges:
        scope = expected.properties.get('alternative_ids')
        pair = [edge for edge in actual.graph.edges
            if edge.from_node_id == expected.from_node_id and edge.to_node_id == expected.to_node_id
            and edge.properties.get('alternative_ids') == scope]
        same = [edge for edge in pair if edge.edge_type == expected.edge_type]
        if not same:
            concrete = [edge for edge in pair if edge.edge_type not in ABSTRACT_RELATIONS]
            if not concrete:
                raise AssertionError('A required relationship/endpoint/scope was lost')
            same = concrete
            manual.append('A different concrete predicate needs manual equivalence review: ' + expected.id)
        if not set(expected.sources) <= {source for edge in same for source in edge.sources}:
            raise AssertionError('A required relationship lost source attribution')
        stable_properties = {key: value for key, value in expected.properties.items() if key != 'description'}
        if not any({key: value for key, value in edge.properties.items() if key != 'description'} == stable_properties for edge in same):
            manual.append('Non-prose edge properties changed: ' + expected.id)
    expected_arcs = {(edge.from_node_id, edge.to_node_id, edge.edge_type,
        tuple(edge.properties.get('alternative_ids') or ())) for edge in fixture.expected.graph.edges}
    for edge in actual.graph.edges:
        arc = (edge.from_node_id, edge.to_node_id, edge.edge_type,
            tuple(edge.properties.get('alternative_ids') or ()))
        if arc not in expected_arcs:
            manual.append('Additional or differently scoped relationship needs semantic review: ' + edge.id)
    return {'strict_graph_validation': True, 'protected_node_sources': True,
        'required_relationships_endpoints_scopes_and_sources': True,
        'semantic_equivalence_proven_by_code': False,
        'semantic_equivalence_review': 'Independent Assessment; mechanical oracle is not a truth proof.',
        'needs_manual_oracle_review': list(dict.fromkeys(manual))}




def validation_failures(root):
    """Retain the actual rejecting contract, separately from execution wrappers."""
    found = {}
    for path in (root / 'trace.jsonl', root / 'model_calls.jsonl'):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event.get('event_type') == 'tool_result':
                result = event.get('result')
                if not isinstance(result, dict) or result.get('status') != 'rejected_unapplied':
                    continue
                tool = event.get('name')
            elif event.get('event_type') == 'protocol_rejected':
                result, tool = event.get('feedback'), None
            else:
                continue
            details = result.get('details') or {}
            row = {'source_file': path.name, 'sequence': event.get('sequence'),
                'target': tool, 'phase': event.get('component'),
                'code': details.get('code', result.get('code')),
                'path': details.get('path', result.get('path')),
                'feedback': result}
            found.setdefault(canonical_hash([tool, result]), row)
    return list(found.values())


def request_contract_evidence(root):
    """Report measured complete inputs and actual advertised role permissions."""
    path = root / 'model_calls.jsonl'
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    contracts = [{key: event.get(key) for key in ('component', 'message_count', 'tool_names', 'draft_version')}
        for event in events if event.get('event_type') == 'request_contract']
    budgets = [{key: event.get(key) for key in ('component', 'characters', 'budget', 'components')}
        for event in events if event.get('event_type') == 'request_budget']
    pending, sent_budgets = {}, []
    for event in events:
        if event.get('event_type') == 'request_budget':
            pending[event.get('episode_id')] = event
        elif event.get('event_type') == 'model_start' and event.get('episode_id') in pending:
            sent_budgets.append(pending[event['episode_id']])
    return {'advertised_requests': contracts, 'request_budgets': budgets,
        'maximum_complete_request_characters': max((row['characters'] for row in budgets), default=None),
        'maximum_sent_request_characters': max((row['characters'] for row in sent_budgets), default=None),
        'constructed_requests_without_model_start': len(budgets) - len(sent_budgets),
        'all_requests_have_three_messages': all(row['message_count'] == 3 for row in contracts) if contracts else None,
        'provider_cache_hits': 'unknown_not_reported'}


def wire_evidence(args, fixture):
    """Audit actual SDK payloads without reading or recording authentication."""
    rows = []
    assessment = {'read_nodes', 'read_relations', 'read_evidence'}
    writer = assessment | {'edit_draft', 'read_predicates'}
    for path in sorted((args.output / 'wire').glob('*_request.json')):
        raw = read(path)
        body = json.dumps(raw.get('body', {}), ensure_ascii=False)
        if not any(prefix + fixture.name in body for prefix in ('reconciliation-smoke:', 'v2-smoke:')):
            continue
        names = [tool.get('function', {}).get('name') for tool in raw.get('body', {}).get('tools', [])]
        role = ('writer' if any(name in names for name in ('edit_draft',))
                else 'assessment' if names and set(names) <= assessment else 'unreported_recovery')
        allowed = assessment if role == 'assessment' else writer if role == 'writer' else writer | assessment
        timeouts = raw.get('timeout') or {}
        rows.append({'request_file': str(path.relative_to(args.output)), 'anonymous_slot': raw.get('credential_slot'),
            'role': role, 'advertised_tools': names,
            'assessment_output_protocol': 'structured_json' if role == 'assessment' else None,
            'permissions_valid': not (set(names) - allowed) if names else None,
            'permission_evidence': 'actual_advertised_tools' if names else 'no_tools_advertised_role_unreported',
            'timeout': timeouts, 'timeout_within_limit': bool(timeouts) and all(value is not None and value <= args.request_timeout for value in timeouts.values()),
            'request_bytes': raw.get('request_bytes'), 'sdk_retry': raw.get('sdk_retry')})
    return rows
