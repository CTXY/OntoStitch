"""Semantic migration, current-graph isolation and nonrecursive context bounds."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_graph_integration import catalog,concept,edge
from test_reconciliation_context import runtime,node_context
from cortex.capabilities.graph_merge.integration.semantic_notes import migrate_relation_notes
from cortex.capabilities.graph_merge.integration.relation_merger import consolidate_relations
from cortex.capabilities.graph_merge.integration.local_validation import validate_local_transition
from cortex.capabilities.graph_merge.integration.reconciliation_contract import ObjectReference
from cortex.schema import GraphNode,GraphEdge,GraphState,GraphSpace,load_root_schema


def test_real_1270_combination_meaning_is_preserved_without_edge_group_references():
    raw=json.loads((Path(__file__).parent/'fixtures/single_graph_merge/real_1270_notes.json').read_text())
    original=deepcopy(raw);graph,records=migrate_relation_notes(raw)
    assert raw==original and len(records)==1
    old=next(n for n in original['graph']['nodes'] if n['node_type']=='concept')
    new=next(n for n in graph.graph.nodes if n.node_type=='concept')
    assert old['properties']['relation_metadata'][0]['description'] in new.properties['definition']
    assert new.properties['targets']==old['properties']['targets']
    assert new.sources==tuple(type(s).model_validate(row) for s,row in zip(new.sources,old['sources']))
    assert [e.model_dump(mode='json') for e in graph.graph.edges]==original['graph']['edges']
    assert 'relation_metadata' not in new.properties


def test_canonical_relation_copies_migrate_and_merge_without_coupled_agent():
    a=concept('A','a').model_dump(mode='json')
    a['properties']['relation_metadata']=[{'id':'copies','edges':['E1','E2'],'description':'A has the recorded representation a.'}]
    graph=GraphState(graph=GraphSpace(nodes=tuple([*catalog(),concept('A','a')]),edges=(edge('E1','A','a'),edge('E2','A','a')))).model_dump(mode='json')
    graph['graph']['nodes'][-1]=a
    migrated,records=migrate_relation_notes(graph)
    class Description:
        def merge_relations(self,context,packet):
            assert 'relation_metadata' not in packet
            return {'results':[{'group_id':g['group_id'],'description':'A has the recorded representation a.'} for g in packet['groups']]}
    result=consolidate_relations(Description(),SimpleNamespace(schema=load_root_schema(),evidence=()),migrated)
    assert len(result.state.graph.edges)==1
    assert records[0]['preserved_text'] in result.state.graph.nodes[-1].properties['definition']
    assert result.checkpoint['stage']=='completed'


def test_five_hundred_unrelated_physical_columns_do_not_expand_local_input():
    base=node_context();small=runtime(ctx=base);prefix=small.messages()[1].content
    table=next(n for n in base.task_before.graph.nodes if n.node_type=='table')
    added=[GraphNode(id=f'extra-column-{i}',node_type='column',name=f'c{i}',
                    properties={'table_ref':table.id,'column_name':f'c{i}'}) for i in range(500)]
    edges=[GraphEdge(id=f'belongs-{i}',edge_type='belongs_to',from_node_id=n.id,to_node_id=table.id,
                    properties={'description':'Catalog ownership.'}) for i,n in enumerate(added)]
    base.task_before=base.task_before.model_copy(update={'graph':base.task_before.graph.model_copy(update={
        'nodes':(*base.task_before.graph.nodes,*added),'edges':(*base.task_before.graph.edges,*edges)})})
    base.candidate=base.task_before
    enlarged=runtime(ctx=base)
    assert enlarged.messages()[1].content==prefix
    assert not any(n.id in enlarged.visible_node_ids['before'] for n in added)
    assert table.id not in enlarged._relation_anchors


def test_source_view_is_removed_from_the_public_contract():
    with pytest.raises(ValueError):ObjectReference(kind='node',view='source',id='old-node')
    assert not hasattr(runtime(),'source_snapshot')


def test_existing_binding_contest_can_be_repaired_incrementally_but_never_expanded():
    base=GraphState(graph=GraphSpace(nodes=tuple([*catalog(),concept('A','a'),concept('B','a'),concept('C','a')]),
               edges=(edge('EA','A','a'),edge('EB','B','a'))))
    # No-op does not hide an existing obligation, but local compilation can
    # defer it to its own Signal. Publication remains independently strict.
    assert validate_local_transition(load_root_schema(),base,base)==base
    changed=base.model_copy(update={'graph':base.graph.model_copy(update={
        'edges':(*base.graph.edges,edge('EC','C','a'))})})
    with pytest.raises(ValueError):validate_local_transition(load_root_schema(),base,changed)


def test_single_graph_checkpoint_resumes_assessment_and_skips_completed_models(tmp_path):
    from langchain_core.messages import AIMessage
    from cortex.capabilities.graph_merge.integration.single_graph import reconcile_current_graph
    from cortex.capabilities.graph_merge.integration.task_context import LocalTask
    from cortex.capabilities.graph_merge.llm.task_agent import IncrementalIntegrationAgent
    from test_graph_integration import state
    class BoundChat:
        def __init__(self,model,names):
            self.model,self.names=model,names
        def bind(self,**kw):return self
        def invoke(self,messages,config=None):
            return self.model.invoke_bound(self.names,messages,config)
    class Chat:
        writer_calls=0
        interrupt=True
        assessment_calls=0
        def bind(self,**kw):return BoundChat(self,set())
        def bind_tools(self,tools,**kw):return BoundChat(self,{t.name for t in tools})
        def invoke_bound(self,names,messages,config=None):
            if 'edit_draft' in names:
                self.writer_calls+=1
                return AIMessage(content='',tool_calls=[{'name':'edit_draft','id':'empty','args':{}}])
            self.assessment_calls+=1
            if self.interrupt:
                self.interrupt=False;raise RuntimeError('Controlled stop before Assessment')
            return AIMessage(content=json.dumps({'explanation':'The current local graph preserves meaning and evidence.','feedback':[]}))
    graph=state([*catalog(),concept('A','a')],[edge('EA','A','a')]);chat=Chat()
    task=LocalTask('bounded-check','reconcile',{'A'},checks=[{'id':'keep','kind':'node_relation_followup',
        'node_ids':['A'],'required_outcome':'Retain this valid current representation.'}])
    args=(IncrementalIntegrationAgent(chat),graph,load_root_schema(),(),task,tmp_path/'checkpoint.sqlite')
    with pytest.raises(RuntimeError,match='Controlled stop'):reconcile_current_graph(*args)
    final=reconcile_current_graph(*args)
    assert chat.writer_calls==1 and chat.assessment_calls==2 and final==graph
    assert reconcile_current_graph(*args)==graph
    assert chat.writer_calls==1 and chat.assessment_calls==2


def test_branch_specific_group_knowledge_migrates_without_resolving_conflict():
    from cortex.application.ontology_graph import GraphManager
    raw=json.loads((Path(__file__).parent/'fixtures/single_graph_merge/real_1270_notes.json').read_text())
    owner=next(n for n in raw['graph']['nodes'] if n['node_type']=='concept')
    group=owner['properties'].pop('relation_metadata')[0]
    owner['merge_status']='conflict';owner['consolidation_status']='semantic_conflict'
    owner['sources']=[{'source_id':'branch-A'},{'source_id':'branch-B'}]
    owner['alternatives']=[]
    for ident in ('A','B'):
        note={**group,'description':'Specific supported business meaning '+ident+'.'}
        owner['alternatives'].append({'alternative_id':ident,'overrides':{'properties.relation_metadata':[note]},'sources':[{'source_id':'branch-'+ident}]})
    owner['conflict_points']=[];owner['common_sources']=[];owner['preservation_reasons']=[]
    migrated,records=migrate_relation_notes(raw)
    node=next(n for n in migrated.graph.nodes if n.node_type=='concept')
    assert [a.alternative_id for a in node.alternatives]==['A','B'] and node.merge_status=='conflict'
    assert len(records)==2
    for alt in node.alternatives:
        content=GraphManager.resolve_alternative(node,alt)
        assert 'Specific supported business meaning '+alt.alternative_id+'.' in content.properties['definition']
        assert 'relation_metadata' not in content.properties


def test_new_invalid_field_cannot_hide_behind_an_existing_binding_contest():
    base=GraphState(graph=GraphSpace(nodes=tuple([*catalog(),concept('A','a'),concept('B','a')]),
                                  edges=(edge('EA','A','a'),edge('EB','B','a'))))
    bad=base.model_copy(update={'graph':base.graph.model_copy(update={'nodes':tuple(
        n.model_copy(update={'properties':{**n.properties,'invented_property':'wrong'}}) if n.id=='A' else n
        for n in base.graph.nodes)})})
    with pytest.raises(ValueError):validate_local_transition(load_root_schema(),base,bad)
