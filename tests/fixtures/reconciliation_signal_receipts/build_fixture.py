"""Extract immutable public views from the real toxicology repeated-signal run.

This fixture contains exactly the business views supplied to Writer, not a claim
that a rendered local view was the full authoritative ontology workspace.
"""
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

from cortex.schema import GraphState, load_root_schema
from cortex.schema.models import EdgeType

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SOURCE_ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
SOURCE = SOURCE_ROOT / 'output/toxicology_remaining_merge_pro_node_revision_20261122'


def parse_view(text):
    result = {'nodes': [], 'edges': [], 'contracts': []}
    for match in re.finditer(r'^### (Node|Relationship|Predicate) `([^`]+)`[^\n]*\n([\s\S]*?)(?=^### |^# |\Z)', text, re.M):
        kind, ident, body = match.groups()
        chunks = [json.loads(x) for x in re.findall(r'```json\n([\s\S]*?)\n```', body)]
        if not chunks:
            continue
        if kind == 'Node':
            result['nodes'].append(chunks[0])
        elif kind == 'Relationship':
            result['edges'].append({'id': ident, 'from_node_id': chunks[0][0], 'edge_type': chunks[0][1],
                'to_node_id': chunks[0][2], 'properties': chunks[1], 'evidence_ids': chunks[2]})
        else:
            result['contracts'].append(chunks[0])
    checks = []
    for match in re.finditer(r'^## Items to check this round .*?\(`([^`]+)`\)\n([\s\S]*?)(?=^## Items to check this round |^# |\Z)', text, re.M):
        ident, body = match.groups()
        check = {'id': ident, 'kind': ident.split(':', 1)[0]}
        for label, field in [('node', 'node_ids'), ('relationship', 'edge_ids')]:
            line = re.search('Matched ' + label + r' IDs[^\n]*', body)
            if line:
                check[field] = re.findall(r'`([^`]+)`', line.group())
        records = [json.loads(x) for x in re.findall(r'```json\n([\s\S]*?)\n```', body)]
        for record in records:
            check.update(record)
        checks.append(check)
    return result, checks


def build():
    source_path = SOURCE / 'merge/batch-0002/model_calls.jsonl'
    source = source_path.read_bytes()
    events = [json.loads(line) for line in source.splitlines()]
    report_path = SOURCE_ROOT / 'output/merge_timing_analysis_20261122/analysis.json'
    report = json.loads(report_path.read_bytes())['databases']['toxicology']
    pairs = []
    for row in report['repeated_signals']:
        for comparison in row['comparisons']:
            if comparison['from_task'] == comparison['to_task']:
                continue  # An Assessment revision is not an accepted-task replay.
            pairs.append({'signal_id': row['signal_id'], **comparison})
    assert len(pairs) == 5
    selected = {p[key] for p in pairs for key in ('from_episode', 'to_episode')}
    writer_starts = [e for e in events if e['event_type'] == 'model_start' and e['component'] == 'integration_reconcile']
    grouped = {}
    for event in writer_starts:
        grouped.setdefault(event['episode_id'], []).append(event)
    snapshots = {}
    physical, installed = {}, {}
    catalog_sources = []
    for name in ('before.json', 'incoming.json', 'state.json'):
        path = SOURCE / 'merge/batch-0002' / name
        data = path.read_bytes()
        graph = json.loads(data)['graph']
        catalog_sources.append({'path':str(path.relative_to(SOURCE_ROOT)), 'sha256':hashlib.sha256(data).hexdigest()})
        for node in graph['nodes']:
            # before already contains the batch's augmented canonical catalog.
            # Raw incoming still contains case-local physical namespaces; those
            # are not additional physical identities in the merged ontology.
            if name == 'before.json' and node['node_type'] in {'table','column','dimension_value'}:
                physical.setdefault(node['id'], node)
        for contract in graph.get('relation_contracts', []):
            installed[contract['id']] = contract
    root_predicates = set(load_root_schema().edge_type_index())

    def source_refs(ids):
        return [{'source_id':eid.removeprefix('sql-training:'), 'case_id':eid.removeprefix('sql-training:'), 'locator':'QA'} for eid in ids]

    def graph_state(public):
        # The complete physical catalog is copied from frozen batch artifacts,
        # including table_ref/column_ref and attributes omitted by presentation.
        nodes = dict(physical)
        for node in public['nodes']:
            if node['node_type'] in {'table','column','dimension_value'}:
                assert node['id'] in physical, node['id']
                continue
            actual = {k:v for k,v in node.items() if k not in {'evidence_ids','readonly_catalog'}}
            actual['sources'] = source_refs(node.get('evidence_ids', []))
            nodes[node['id']] = actual
        edges = [{**{k:v for k,v in edge.items() if k!='evidence_ids'}, 'sources':source_refs(edge['evidence_ids'])} for edge in public['edges']]
        contracts = []
        for contract in public['contracts']:
            if contract['id'] in root_predicates:
                continue
            # Keep the actual declared contract, rather than treating rendered
            # authorability metadata as an EdgeType property.
            item = installed.get(contract['id']) or {k:v for k,v in contract.items() if k in EdgeType.model_fields}
            contracts.append(item)
        state = GraphState.model_validate({'graph':{'nodes':list(nodes.values()), 'edges':edges, 'relation_contracts':contracts}})
        return state.model_dump(mode='json')
    for ident in sorted(selected):
        starts = grouped[ident]
        first = starts[0]
        last_start = starts[-1]['recorded_at']
        next_start = min((e['recorded_at'] for e in writer_starts if e['recorded_at'] > last_start and e['episode_id'] != ident), default='9999')
        accepted = [e for e in events if e['event_type'] == 'assessment_result'
            and last_start < e['recorded_at'] < next_start and not e['arguments']['feedback']]
        assert len(accepted) == 1, (ident, len(accepted))
        text = first['messages'][1]['content']
        public_graph, checks = parse_view(text)
        receipt = next(e for e in events if e['event_type']=='agent_episode' and e.get('episode_id') == ident)
        snapshots[ident] = {'task_id': receipt['task_id'], 'writer_recorded_at': first['recorded_at'],
            'writer_call_id': first['call_id'], 'writer_before_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'graph': public_graph, 'graph_state':graph_state(public_graph), 'checks': checks,
            'accepted_assessment': {'recorded_at': accepted[0]['recorded_at'],
                'episode_id': accepted[0]['episode_id'], 'output': accepted[0]['arguments']}}
    evidence = json.loads((SOURCE/'source_evidence.json').read_bytes())
    used = {eid for snapshot in snapshots.values() for kind in ('nodes','edges') for obj in snapshot['graph'][kind] for eid in obj.get('evidence_ids',[])}
    evidence = {eid: evidence[eid.removeprefix('sql-training:')] for eid in sorted(used)}
    result = {'version': 1, 'kind': 'real_model_public_local_views', 'database': 'toxicology', 'batch': 2,
        'source': {'path': str(source_path.relative_to(SOURCE_ROOT)), 'captured_prefix_bytes':len(source),
            'captured_prefix_sha256':hashlib.sha256(source).hexdigest()},
        'scope': 'Exact rendered local business nodes, relations, contracts and checks, plus independent accepted Assessment. graph_state is a validated local GraphState projection supplemented with the frozen physical catalog, not the full authoritative ontology. Public evidence IDs become canonical QA SourceReferences; storage provenance variants omitted by presentation are not asserted. No new model execution.',
        'catalog_sources':catalog_sources,
        'pairs': pairs, 'snapshots': snapshots, 'evidence': evidence}
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    destination = HERE/'toxicology_repeated_signals.json.gz'
    destination.write_bytes(gzip.compress(encoded, mtime=0))
    manifest = {'fixture': destination.name, 'fixture_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
        'uncompressed_sha256':hashlib.sha256(encoded).hexdigest(), 'source':result['source'],
        'pairs':len(pairs), 'snapshots':len(snapshots), 'evidence_records':len(evidence),
        'compressed_bytes':destination.stat().st_size,
        'notes': ['Original runtime, logs and checkpoints are read only.',
            'Same-task Assessment revisions are excluded; the five pairs are accepted-then-retriggered checks.',
            'Each public view preserves its original grouping so regrouping differences remain testable.',
            'Physical summaries remain summaries; tests must not infer full graph absence from them.']}
    (HERE/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(manifest,ensure_ascii=False))


if __name__ == '__main__':
    build()
