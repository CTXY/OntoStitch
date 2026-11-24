"""No API calls: replay the five accepted-then-retriggered toxicology checks.

The fixture is the actual public local graph shown to the model, supplemented
with frozen physical fields. It is not used to prove whole-ontology absence.
"""
import copy
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex.capabilities.graph_merge.reconciliation.premises import signal_key, signal_premise
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.capabilities.graph_merge.evidence import EvidenceItem
from cortex.schema import GraphNode, GraphState, SourceReference, load_root_schema

FIXTURES = Path(__file__).parent / 'fixtures/reconciliation_signal_receipts'


@pytest.fixture(scope='module')
def actual_records():
    manifest = json.loads((FIXTURES/'manifest.json').read_text())
    compressed = (FIXTURES/manifest['fixture']).read_bytes()
    assert hashlib.sha256(compressed).hexdigest() == manifest['fixture_sha256']
    encoded = gzip.decompress(compressed)
    assert hashlib.sha256(encoded).hexdigest() == manifest['uncompressed_sha256']
    result = json.loads(encoded)
    assert len(result['pairs']) == 5
    assert len(result['snapshots']) == 8
    return result


def context(records):
    evidence = []
    for ident, content in records['evidence'].items():
        source = ident.removeprefix('sql-training:')
        evidence.append(EvidenceItem(id=ident, source=SourceReference(source_id=source, case_id=source, locator='QA'),
            case_id=source, content=content))
    return SimpleNamespace(schema=load_root_schema(), evidence=tuple(evidence), sql_dialect='sqlite')


def case(records, index, side='from_episode'):
    pair = records['pairs'][index]
    snapshot = records['snapshots'][pair[side]]
    check = next(c for c in snapshot['checks'] if c['id'] == pair['signal_id'])
    return GraphState.model_validate(snapshot['graph_state']), copy.deepcopy(check), snapshot


def anchors(check):
    return set(check.get('node_ids', ()))


def revise_nodes(state, transform):
    return state.model_copy(update={'graph':state.graph.model_copy(update={
        'nodes':tuple(transform(node) for node in state.graph.nodes)})})


@pytest.mark.parametrize('index', range(5))
def test_real_accepted_signal_reused_after_actual_regrouping(actual_records, index):
    before, check, first = case(actual_records, index)
    after, repeated_check, second = case(actual_records, index, 'to_episode')
    assert first['accepted_assessment']['output']['feedback'] == []
    assert second['accepted_assessment']['output']['feedback'] == []
    assert first['checks'] != second['checks'] or first['graph'] != second['graph']
    assert signal_key(check) == signal_key(repeated_check)
    ctx = context(actual_records)
    assert canonical_hash(signal_premise(ctx,before,check)) == canonical_hash(signal_premise(ctx,after,repeated_check))


@pytest.mark.parametrize('index', range(5))
def test_real_premise_ignores_storage_order_and_check_reference_order(actual_records,index):
    state,check,_ = case(actual_records,index)
    reordered = state.model_copy(update={'graph':state.graph.model_copy(update={
        'nodes':tuple(reversed(state.graph.nodes)), 'edges':tuple(reversed(state.graph.edges)),
        'relation_contracts':tuple(reversed(state.graph.relation_contracts))})})
    changed = copy.deepcopy(check)
    for key in ('node_ids','edge_ids','shared_operands'):
        if key in changed:
            changed[key] = list(reversed(changed[key]))
    ctx=context(actual_records)
    assert signal_key(check) == signal_key(changed)
    assert signal_premise(ctx,state,check) == signal_premise(ctx,reordered,changed)


@pytest.mark.parametrize('index',range(5))
def test_real_premise_ignores_disconnected_node_and_unused_evidence(actual_records,index):
    state,check,_=case(actual_records,index)
    unrelated=GraphNode(id='fixture.disconnected',node_type='concept',name='Unrelated item',
        properties={'definition':'An isolated control node outside this check.'})
    revised=state.model_copy(update={'graph':state.graph.model_copy(update={'nodes':(*state.graph.nodes,unrelated)})})
    ctx=context(actual_records)
    original=signal_premise(ctx,state,check)
    assert signal_premise(ctx,revised,check)==original
    ctx.evidence=(*ctx.evidence,EvidenceItem(id='sql-training:fixture-unused',source=SourceReference(source_id='fixture-unused'),
        case_id=None,content={'question':'Unrelated control question','sql':'SELECT 1'}))
    assert signal_premise(ctx,revised,check)==original


@pytest.mark.parametrize('index',range(5))
def test_real_premise_changes_for_anchor_business_meaning(actual_records,index):
    state,check,_=case(actual_records,index)
    anchor=sorted(anchors(check))[0]
    revised=revise_nodes(state,lambda n:n.model_copy(update={'description':(n.description or '')+' An explicit new condition.'}) if n.id==anchor else n)
    ctx=context(actual_records)
    assert signal_premise(ctx,state,check)!=signal_premise(ctx,revised,check)


@pytest.mark.parametrize('index',range(5))
def test_real_premise_changes_for_incident_relation_and_new_incident_edge(actual_records,index):
    state,check,_=case(actual_records,index);roots=anchors(check)
    nominated=set(check.get('edge_ids',()))
    # A triangle examines its actual directed pairs, not every unrelated inbound
    # consumer of a triangle endpoint. Pick a nominated relation when present.
    edge=next(e for e in state.graph.edges if e.id in nominated) if nominated else next(
        e for e in state.graph.edges if e.from_node_id in roots or e.to_node_id in roots)
    modified=edge.model_copy(update={'properties':{**edge.properties,'description':edge.properties.get('description','')+' New scope.'}})
    changed=state.model_copy(update={'graph':state.graph.model_copy(update={'edges':tuple(modified if e.id==edge.id else e for e in state.graph.edges)})})
    ctx=context(actual_records);original=signal_premise(ctx,state,check)
    assert signal_premise(ctx,changed,check)!=original
    added=edge.model_copy(update={'id':'fixture.new-incident-edge'})
    expanded=state.model_copy(update={'graph':state.graph.model_copy(update={'edges':(*state.graph.edges,added)})})
    assert signal_premise(ctx,expanded,check)!=original


@pytest.mark.parametrize('index',range(5))
def test_real_premise_changes_when_supporting_evidence_content_changes(actual_records,index):
    state,check,_=case(actual_records,index);roots=anchors(check)
    sources={s.source_id for n in state.graph.nodes if n.id in roots for s in n.sources}
    ctx=context(actual_records);original=signal_premise(ctx,state,check)
    evidence=next(e for e in ctx.evidence if e.source.source_id in sources)
    changed=evidence.model_copy(update={'content':{**evidence.content,'evidence':str(evidence.content.get('evidence',''))+' A corrected qualification.'}})
    ctx.evidence=tuple(changed if e.id==evidence.id else e for e in ctx.evidence)
    assert signal_premise(ctx,state,check)!=original


def test_triangle_ignores_an_unrelated_metric_consumer_of_its_bond_endpoint(actual_records):
    state,check,_=case(actual_records,1)
    nominated=set(check['edge_ids'])
    edge=next(e for e in state.graph.edges if e.edge_type=='depends_on'
        and e.to_node_id=='input::n124' and e.id not in nominated)
    modified=edge.model_copy(update={'properties':{**edge.properties,
        'description':edge.properties['description']+' A change to an outside metric consumer.'}})
    changed=state.model_copy(update={'graph':state.graph.model_copy(update={
        'edges':tuple(modified if e.id==edge.id else e for e in state.graph.edges)})})
    ctx=context(actual_records)
    assert signal_premise(ctx,state,check)==signal_premise(ctx,changed,check)


@pytest.mark.parametrize('index',range(5))
def test_physical_reference_meaning_remains_a_required_premise(actual_records,index):
    state,check,_=case(actual_records,index);ctx=context(actual_records)
    original=signal_premise(ctx,state,check)
    physical=next(n for n in original['nodes'] if n.get('node_type')=='column')
    revised=revise_nodes(state,lambda n:n.model_copy(update={'properties':{
        **n.properties,'data_type':'DECIMAL'}}) if n.id==physical['id'] else n)
    assert signal_premise(ctx,revised,check)!=original


@pytest.mark.parametrize('index',range(5))
def test_premise_json_roundtrip_preserves_real_recovery_binding(actual_records,index):
    state,check,_=case(actual_records,index);ctx=context(actual_records)
    first=signal_premise(ctx,state,check)
    saved=json.loads(json.dumps({'graph':state.model_dump(mode='json'),'check':check,'premise':first}))
    restored=GraphState.model_validate(saved['graph'])
    assert signal_premise(ctx,restored,saved['check'])==saved['premise']
