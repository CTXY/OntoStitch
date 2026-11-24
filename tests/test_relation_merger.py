"""Real grouping/application and bounded model protocol, without API calls."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from cortex.capabilities.graph_merge.integration.relation_merger import (
    consolidate_relations, relation_groups,
)
from cortex.schema import SourceReference, load_root_schema
from test_graph_integration import catalog, concept, edge, state


def fixture(*, metadata=False):
    first = edge('E1', 'A', 'B', 'is_a').model_copy(update={
        'properties': {'description': 'A is a business subtype of B.'},
        'sources': (SourceReference(source_id='first'),)})
    second = edge('E2', 'A', 'B', 'is_a').model_copy(update={
        'properties': {'description': 'B includes the subtype A.'},
        'sources': (SourceReference(source_id='second'),)})
    a = concept('A', 'a')
    graph = state([*catalog(), a, concept('B', 'b')], [first, second])
    if metadata:
        from cortex.capabilities.graph_merge.integration.semantic_notes import migrate_relation_notes
        raw=graph.model_dump(mode='json')
        next(n for n in raw['graph']['nodes'] if n['id']=='A')['properties']['relation_metadata']=[
            {'id':'paired','edges':['E1','E2'],'description':'Both original roles remain meaningful.'}]
        graph,_=migrate_relation_notes(raw)
    context = SimpleNamespace(schema=load_root_schema(), evidence=())
    return graph, context


class Hook:
    def __init__(self): self.calls = []
    def merge_relations(self, context, packet):
        self.calls.append(deepcopy(packet))
        return {'results': [{'group_id': row['group_id'], 'description': 'A is a business subtype included in B.'}
                            for row in packet['groups']]}


def test_description_groups_merge_sources_and_resume_without_model_replay():
    graph, ctx = fixture(); before = graph.model_dump_json(); hook = Hook(); saved = []
    result = consolidate_relations(hook, ctx, graph, save=saved.append)
    assert len(hook.calls) == 1 and graph.model_dump_json() == before
    edges = {e.id: e for e in result.state.graph.edges}
    assert 'E2' not in edges and {s.source_id for s in edges['E1'].sources} == {'first', 'second'}
    assert result.edge_aliases == {'E1': 'E1', 'E2': 'E1'}
    assert result.checkpoint['stage'] == 'completed'
    same = consolidate_relations(hook, ctx, result.state, result.checkpoint)
    assert same.state == result.state and len(hook.calls) == 1
    reconstructed = consolidate_relations(hook, ctx, graph, result.checkpoint)
    assert reconstructed.state == result.state and len(hook.calls) == 1
    assert all('sources' not in row for row in hook.calls[0]['relations'])


def test_distinct_predicates_and_formal_scopes_are_not_merged():
    graph, _ = fixture()
    a, b = graph.graph.edges[-2:]
    # All properties except prose are protected, including explicit branch sets.
    variants = [a.model_copy(update={'id': str(i), 'properties': {'description': str(i),
        'alternative_ids': scope}}) for i, scope in enumerate((['x', 'y'], ['y', 'x'], ['x'], ['x']))]
    grouped = relation_groups(graph.model_copy(update={'graph': graph.graph.model_copy(update={'edges': tuple(variants)})}))
    assert sorted(len(g['member_edge_ids']) for g in grouped) == [2, 2]
    distinct = graph.model_copy(update={'graph': graph.graph.model_copy(update={
        'edges': (a, b.model_copy(update={'edge_type': 'depends_on'}))})})
    assert relation_groups(distinct) == []


def test_migrated_group_meaning_survives_merge_without_coupled_repair():
    graph,ctx=fixture(metadata=True);hook=Hook()
    result=consolidate_relations(hook,ctx,graph)
    assert result.checkpoint['stage']=='completed'
    assert 'Both original roles remain meaningful.' in next(n for n in result.state.graph.nodes if n.id=='A').properties['definition']
    assert len(hook.calls)==1
    restored=consolidate_relations(hook,ctx,graph,result.checkpoint)
    assert restored.state==result.state and len(hook.calls)==1


def test_checkpoint_corruption_or_source_change_is_rejected():
    graph, ctx = fixture(); hook = Hook()
    result = consolidate_relations(hook, ctx, graph)
    corrupt = deepcopy(result.checkpoint)
    corrupt['results'][next(iter(corrupt['results']))] = 'Corrupted hidden result'
    with pytest.raises(ValueError, match='integrity'):
        consolidate_relations(hook, ctx, graph, corrupt)
    changed = graph.model_copy(update={'graph': graph.graph.model_copy(update={
        'edges': tuple(e.model_copy(update={'properties': {**e.properties, 'description': 'Different input'}})
                       for e in graph.graph.edges)})})
    with pytest.raises(ValueError, match='source'):
        consolidate_relations(hook, ctx, changed, result.checkpoint)


def test_evidence_change_does_not_reuse_old_semantic_result():
    from cortex.capabilities.graph_merge.models import EvidenceItem
    graph, ctx = fixture(); hook = Hook()
    result = consolidate_relations(hook, ctx, graph)
    ctx.evidence = (EvidenceItem(id='new-evidence', source=SourceReference(source_id='first'),
        case_id='first', content={'question': 'A newly changed source', 'sql': 'SELECT 1'}),)
    with pytest.raises(ValueError, match='schema/evidence'):
        consolidate_relations(hook, ctx, graph, result.checkpoint)
    assert len(hook.calls) == 1


def test_singleton_does_not_call_model_or_reorder_graph():
    graph, ctx = fixture(); hook = Hook()
    graph = graph.model_copy(update={'graph': graph.graph.model_copy(update={
        'edges': tuple(reversed([e for e in graph.graph.edges if e.id != 'E2']))})})
    result = consolidate_relations(hook, ctx, graph)
    assert result.state.model_dump_json() == graph.model_dump_json()
    assert not hook.calls and result.edge_aliases == {}


@pytest.mark.parametrize('focus', [{'E2'}, {'A'}])
def test_focused_preview_does_not_consolidate_unrelated_groups(focus):
    graph, ctx = fixture(); hook = Hook()
    unrelated = (edge('U1', 'C', 'D', 'is_a'), edge('U2', 'C', 'D', 'is_a').model_copy(update={
        'properties': {'description': 'D also includes the unrelated subtype C.'}}))
    graph = state([*graph.graph.nodes, concept('C', 'c'), concept('D', 'a')],
                  [*graph.graph.edges, *unrelated])
    assert len(relation_groups(graph)) == 2 and len(relation_groups(graph, focus)) == 1
    result = consolidate_relations(hook, ctx, graph, focus_ids=focus)
    edges = {edge.id: edge for edge in result.state.graph.edges}
    assert 'E2' not in edges
    assert all(edges[edge.id] == edge for edge in unrelated)
    assert result.edge_aliases == {'E1': 'E1', 'E2': 'E1'}
    with pytest.raises(ValueError, match='focus scope'):
        consolidate_relations(hook, ctx, result.state, result.checkpoint)
    none = consolidate_relations(hook, ctx, graph, focus_ids=set())
    assert none.state == graph and len(hook.calls) == 1


class Chat:
    def __init__(self, *, bad_first=False): self.requests = []; self.tools = []; self.bad_first = bad_first
    def bind_tools(self, tools, **kwargs): self.tools = tools; return self
    def invoke(self, messages, config=None):
        self.requests.append((messages, [tool.name for tool in self.tools]))
        packet = json.loads(messages[1].content)
        result = {'results': [{'group_id': row['group_id'], 'description': 'The exact combined subtype relation.'}
                              for row in packet['groups']]}
        if self.bad_first and len(self.requests) == 1: result['results'][0]['edge_type'] = 'forbidden'
        return AIMessage(content='', tool_calls=[{'name': 'submit_relations', 'args': result, 'id': 'result'}])


def agent(chat):
    return SimpleNamespace(llm=chat, max_calls=4, trace_sink=None, call_budget=None, request_max_characters=90000)


def test_real_tool_loop_is_description_only_has_two_tools_and_no_assessment():
    graph, ctx = fixture(); chat = Chat(bad_first=True)
    result = consolidate_relations(agent(chat), ctx, graph)
    assert result.checkpoint['stage'] == 'completed' and len(chat.requests) == 2
    assert all(names == ['read_evidence', 'submit_relations'] for _, names in chat.requests)
    assert all(len(messages) == 4 for messages, _ in chat.requests)
    assert 'Extra inputs' in chat.requests[1][0][-1].content


def test_saved_remote_result_survives_crash_before_service_records_groups():
    graph, ctx = fixture(); chat = Chat(); saved = []
    def crash(record):
        saved.append(record)
        if not record['results'] and any(row.get('result') for row in record['batches'].values()):
            raise RuntimeError('Crash after durable model output')
    with pytest.raises(RuntimeError, match='durable'):
        consolidate_relations(agent(chat), ctx, graph, save=crash)
    result = consolidate_relations(agent(chat), ctx, graph, saved[-1])
    assert result.checkpoint['stage'] == 'completed' and len(chat.requests) == 1


def test_batch_limit_and_cross_batch_atomic_collection():
    graph, ctx = fixture(metadata=True)
    nodes = list(graph.graph.nodes); edges = []
    for i in range(12):
        target = 'B' if i == 0 else 'B' + str(i)
        if i: nodes.append(concept(target, 'b'))
        for j in range(2):
            key = ('E1' if j == 0 else 'E2') if i == 0 else f'e{i}-{j}'
            edges.append(edge(key, 'A', target, 'is_a').model_copy(update={'properties': {'description': f'Role {i} wording {j}'}}))
    graph = graph.model_copy(update={'graph': graph.graph.model_copy(update={'nodes': tuple(nodes), 'edges': tuple(edges)})})
    hook = Hook()
    result=consolidate_relations(hook,ctx,graph,batch_size=10)
    assert [len(packet['groups']) for packet in hook.calls] == [10, 2]
    assert len(result.checkpoint['results']) == 12
    assert len(graph.graph.edges) == 24
    with pytest.raises(ValueError, match='1–10'):
        consolidate_relations(hook, ctx, graph, batch_size=11)
