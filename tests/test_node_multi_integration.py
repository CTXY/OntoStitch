"""One fixed input group, multiple jointly compiled and assessed results."""
from copy import deepcopy
import json
import pytest
from langchain_core.messages import AIMessage

from graph_merge_fixtures import catalog, concept, edge, state
from test_merge_identity_integration_v2 import content, prepare
from graph_merge_runtime_fixtures import engine_for
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch, compile_patch
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.state import TaskState
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.consolidation.presentation import NodeTaskDialogue, build_node_packet
from cortex.capabilities.graph_merge.consolidation.contract import normalize_node_result
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.schema import GraphNode, GraphState, SourceReference


def fixture():
    base = state([*catalog(), concept('A','a'), concept('B','a'), concept('C','b'),
                  concept('D','b'), concept('E','b'), GraphNode(id='consumer', node_type='metric', name='Amount',
                      properties={'expression':{'expr':'COUNT(${x})','refs':{'x':'D'}}})],
                 [edge('cross','B','D','is_a'), edge('incoming','consumer','D','depends_on'),
                  edge('a-bind','B','a'), edge('b-bind','D','b')])
    ctx, _ = prepare(base)
    ctx.task_before=base
    ctx.task_identity_groups=[{'owner_id':'A','member_ids':['A','B','C','D','E']}]
    ctx.node_local_compile=True
    packet=build_node_packet(ctx,{'phase':'consolidate'})
    return base,ctx,packet


def draft(packet, groups=(('A','B'),('C','D','E')), separate=()):
    nodes={n['id']:n for n in packet['group']['members']}
    return {'integrate_groups':[{'owner_id':members[0],'member_ids':list(members),
        'content':{k:deepcopy(nodes[members[0]][k]) for k in ('name','description','aliases','tags','properties')},
        'source_routes':[{'source_unit_id':u['unit_id'],'destination':{'kind':'result'},'explanation':'Preserve this complete input.'}
                         for u in packet['source_unit_manifest'][0]['source_units'] if u['node_id'] in members],
        'explanation':'Integrate one identity subgroup.'} for members in groups],
        'keep_separate_ids':list(separate),'explanation':'Correct the proposed group locally.'}


def compile_raw(base,ctx,packet,raw,bypass=False):
    patch=MergePatch.model_validate(raw) if bypass else normalize_node_result('writer',raw,payload=packet,base=base)[0]
    return compile_patch(ctx,base,patch,targets={'A'},aliases={k:'A' for k in packet['group']['member_ids']})


def test_two_subgroups_preserve_sources_cross_edges_and_external_refs():
    base,ctx,packet=fixture();raw=draft(packet);old=deepcopy(raw)
    compiled=compile_raw(base,ctx,packet,raw)
    nodes={n.id:n for n in compiled.state.graph.nodes};edges={e.id:e for e in compiled.state.graph.edges}
    assert compiled.node_aliases=={'A':'A','B':'A','C':'C','D':'C','E':'C'}
    assert set(nodes['A'].sources)=={SourceReference(source_id=k) for k in ('A','B')}
    assert set(nodes['C'].sources)=={SourceReference(source_id=k) for k in ('C','D','E')}
    assert (edges['cross'].from_node_id,edges['cross'].to_node_id)==('A','C')
    assert edges['incoming'].to_node_id=='C'
    assert nodes['consumer'].properties['expression']['refs']=={'x':'C'}
    assert next(e for e in base.graph.edges if e.id=='cross').properties==edges['cross'].properties
    assert raw==old
    ctx.active_draft=raw;ctx.last_compiled_patch=compiled;ctx.candidate=compiled.state
    shown=build_node_packet(ctx,{'phase':'node_assess'})
    assert len(shown['candidate']['integrated_groups'])==2
    assert {n['id'] for n in shown['candidate']['nodes']}=={'A','C'}
    assert shown['candidate']['singleton_ids']==[] and 'node' not in shown['candidate']
    assert len(shown['candidate']['integrated_groups'][1]['source_routes'])==3


@pytest.mark.parametrize('bypass',[False,True])
@pytest.mark.parametrize('change',['missing','overlap','outside','partial_singletons','wrong_owner','cross_sources','missing_second_route'])
def test_invalid_partition_and_sources_fail_without_writing_graph(bypass,change):
    base,ctx,packet=fixture();raw=draft(packet);before=base.model_dump_json()
    if change=='missing':raw['integrate_groups'][1]['member_ids'].remove('E')
    if change=='overlap':raw['integrate_groups'][1]['member_ids'].append('B')
    if change=='outside':raw['integrate_groups'][1]['member_ids'].append('consumer')
    if change=='partial_singletons':raw=draft(packet,groups=(),separate=['A','B','C','D'])
    if change=='wrong_owner':raw['integrate_groups'][1]['owner_id']='D'
    if change=='cross_sources':raw['integrate_groups'][1]['source_routes'][0]=deepcopy(raw['integrate_groups'][0]['source_routes'][0])
    if change=='missing_second_route':raw['integrate_groups'][1]['source_routes'].pop()
    with pytest.raises(ValueError):compile_raw(base,ctx,packet,raw,bypass=bypass)
    assert base.model_dump_json()==before


@pytest.mark.parametrize('groups,separate',[((),('A','B','C','D','E')),
    ((('B','C'),('D','E')),('A',)),((('A','B'),('C','D')),('E',))])
def test_explicit_singletons_and_original_owner_retention(groups,separate):
    base,ctx,packet=fixture();compiled=compile_raw(base,ctx,packet,draft(packet,groups,separate))
    nodes={n.id:n for n in compiled.state.graph.nodes};old={n.id:n for n in base.graph.nodes}
    for key in separate:assert nodes[key]==old[key]
    if not groups:assert compiled.state==base


def test_partition_routes_and_second_result_participate_in_premise_hash():
    base,ctx,packet=fixture();raw=draft(packet);compiled=compile_raw(base,ctx,packet,raw)
    ctx.active_draft=raw;ctx.candidate=compiled.state;ctx.last_compiled_patch=compiled
    shown=build_node_packet(ctx,{'phase':'node_assess'})
    original=NodeTaskDialogue('fixed',shown).premise_hash(policy='p')
    for change in ('second_node','second_route'):
        altered=deepcopy(shown)
        if change=='second_node':altered['candidate']['nodes'][1]['description']='New semantic fact.'
        else:altered['candidate']['integrated_groups'][1]['source_routes'][0]['destination']={'kind':'discard'}
        assert NodeTaskDialogue('fixed',altered).premise_hash(policy='p')!=original


class MultiChat:
    def __init__(self,interrupt=False):self.writer_calls=0;self.assess_calls=0;self.interrupt=interrupt
    def bind(self,**kwargs):return self
    def bind_tools(self,tools,**kwargs):self.names={t.name for t in tools};return self
    def invoke(self,messages,config=None):
        fixed=json.loads(messages[2].content.split('\n',1)[1]);current=json.loads(messages[-1].content.split('\n',1)[1]);packet={**fixed,**current}
        if 'submit_patch' in self.names:
            self.writer_calls+=1;name='submit_patch';value=draft(packet)
        else:
            self.assess_calls+=1
            assert len(packet['candidate']['integrated_groups'])==2
            if self.interrupt:self.interrupt=False;raise RuntimeError('Controlled interruption before joint Assessment')
            name='Review';value={'verdict':'accept','issues':[]}
        return AIMessage(content='',tool_calls=[{'name':name,'args':value,'id':name+str(self.writer_calls+self.assess_calls)}])


def engine(tmp_path,chat):
    base,_,_=fixture();eng,_=engine_for(base,IncrementalIntegrationAgent(chat),tmp_path/'multi.sqlite')
    task=LocalTask('original-group','consolidate',{'A'},groups=[{'members':['A','B','C','D','E'],'meaning':'Initial hypothesis','reason':'Test local correction.'}])
    return eng,task,base


def test_joint_assessment_checkpoint_restores_without_replaying_writer(tmp_path):
    chat=MultiChat(interrupt=True);eng,task,base=engine(tmp_path,chat);aliases={k:'A' for k in ('A','B','C','D','E')}
    with pytest.raises(RuntimeError,match='Controlled interruption'):eng._run_task(task,'multi',aliases=aliases)
    saved=WorkspaceStore(eng.workspace_path).load('multi')
    assert saved['current']==base.model_dump(mode='json') and saved['history']==[]
    assert saved['task_state']['stage']=='assessing'
    restored,task,_=engine(tmp_path,chat)
    restored.session.current=GraphState.model_validate(saved['current']);restored.session.lineage=LineageIndex.restore(restored.session.inputs,saved['lineage'])
    restored.session.task_state=TaskState.from_dict(saved['task_state']);restored.session.contract_versions=restored.session.task_state.versions
    restored.session.storage_revision=saved['revision'];restored.session.history=deepcopy(saved['history']);restored.session.revision=saved['accepted_revision']
    restored._run_task(task,'multi',aliases=aliases)
    assert chat.writer_calls==1 and chat.assess_calls==2
    assert len(restored.session.history)==1 and restored.session.history[0]['task_id']=='original-group'
    final=WorkspaceStore(restored.workspace_path).load('multi')
    assert final['task_state']['stage']=='committed'
    assert {n.id for n in restored.session.current.graph.nodes if n.node_type=='concept'}=={'A','C'}


def test_partition_preserves_conflict_and_branch_specific_edges():
    from test_merge_identity_integration_v2 import points
    base,ctx,packet=fixture();raw=draft(packet);group=raw['integrate_groups'][1]
    normal=group.pop('content')
    group['alternatives']=[{'alternative_id':'left','content':{**deepcopy(normal),'properties':{**normal['properties'],'definition':'Left complete interpretation.'}}},
                           {'alternative_id':'right','content':{**deepcopy(normal),'properties':{**normal['properties'],'definition':'Right complete interpretation.'}}}]
    group['conflict_points']=points(['left','right'])
    units={u['unit_id']:u['node_id'] for u in packet['source_unit_manifest'][0]['source_units']}
    for route in group['source_routes']:
        key=units[route['source_unit_id']]
        route['destination']=({'kind':'common'} if key=='E' else
                              {'kind':'alternatives','alternative_ids':['left' if key=='C' else 'right']})
    from cortex.capabilities.graph_merge.consolidation.relations import NodeRelationCoordinationRequired
    with pytest.raises(NodeRelationCoordinationRequired) as caught:
        compile_raw(base,ctx,packet,raw)
    # A new conflict at the destination of existing edges still requires the
    # existing semantic coordination gate. Inspect its unaccepted preview.
    assert any(d['code']=='incoming_target_conflict_scope' for d in caught.value.diagnostics)
    candidate=caught.value.integrated_state
    owner=next(n for n in candidate.graph.nodes if n.id=='C')
    assert {a.alternative_id for a in owner.alternatives}=={'left','right'}
    assert owner.common_sources==(SourceReference(source_id='E'),)
    branches={a.alternative_id:set(a.sources) for a in owner.alternatives}
    assert branches=={'left':{SourceReference(source_id='C')},'right':{SourceReference(source_id='D')}}
    bound=next(e for e in candidate.graph.edges if e.id=='b-bind')
    assert bound.from_node_id=='C' and bound.properties['alternative_ids']==['right']
    cross=next(e for e in candidate.graph.edges if e.id=='cross')
    assert 'alternative_ids' not in cross.properties


def test_route_explanation_and_partition_order_are_not_new_facts():
    base,ctx,packet=fixture();raw=draft(packet);compiled=compile_raw(base,ctx,packet,raw)
    ctx.active_draft=raw;ctx.candidate=compiled.state;ctx.last_compiled_patch=compiled
    original=build_node_packet(ctx,{'phase':'node_assess'});altered=deepcopy(original)
    altered['candidate']['integrated_groups'].reverse()
    for group in altered['candidate']['integrated_groups']:
        group['member_ids'].reverse();group['source_routes'].reverse()
        for route in group['source_routes']:route['explanation']='Different wording, same facts.'
    assert NodeTaskDialogue('fixed',original).premise_hash(policy='p')==NodeTaskDialogue('fixed',altered).premise_hash(policy='p')


@pytest.mark.parametrize('kind',['concept','metric','event','generation_rule'])
def test_same_partition_contract_compiles_all_four_node_types(kind):
    def properties(index):
        if kind=='concept':return {'targets':[['a' if index<2 else 'b']]}
        if kind=='metric':return {'expression':{'expr':'COUNT(${input})','refs':{'input':'a' if index<2 else 'b'}}}
        if kind=='event':return {'definition':'Application submission' if index<2 else 'Application approval'}
        return {'condition':'A matching request', 'action':'Apply the first filter' if index<2 else 'Apply the second filter',
                'reason':'Preserve the original decision', 'triggers':{'question':{'phrases':['matching request']},'sql':{'columns':[],'operations':[]}}}
    originals=[GraphNode(id=k,node_type=kind,name='First identity' if i<2 else 'Second identity',
                         properties=properties(i),sources=(SourceReference(source_id=k),)) for i,k in enumerate('ABCD')]
    relations=([edge('operand-'+n.id,n.id,'a' if i<2 else 'b') for i,n in enumerate(originals)] if kind=='metric' else [])
    base=state([*catalog(),*originals],relations);ctx,_=prepare(base);ctx.task_before=base
    ctx.task_identity_groups=[{'owner_id':'A','member_ids':list('ABCD')}];ctx.node_local_compile=True
    packet=build_node_packet(ctx,{'phase':'consolidate'});raw=draft(packet,groups=(('A','B'),('C','D')))
    patch=normalize_node_result('writer',raw,payload=packet,base=base)[0]
    compiled=compile_patch(ctx,base,patch,targets={'A'},aliases={k:'A' for k in 'ABCD'})
    assert {n.id for n in compiled.state.graph.nodes if n.node_type==kind}=={'A','C'}
    assert next(n for n in compiled.state.graph.nodes if n.id=='C').sources==tuple(SourceReference(source_id=k) for k in 'CD')


def test_empty_internal_subgroup_reports_contract_error():
    from types import SimpleNamespace
    from cortex.capabilities.graph_merge.consolidation.revision import partition_groups
    with pytest.raises(ValueError,match='partition_group_too_small'):
        partition_groups({'owner_id':'A','member_ids':['A','B']},
                         [SimpleNamespace(owner_id='A',member_ids=())],['A','B'])


def test_native_invalid_json_recovers_from_one_exact_json_fence(tmp_path):
    class FencedChat(MultiChat):
        def invoke(self,messages,config=None):
            if 'submit_patch' in self.names and self.writer_calls==0:
                self.writer_calls+=1
                return AIMessage(content='',invalid_tool_calls=[{'name':'submit_patch','id':'bad-json',
                    'args':'{','error':'Invalid native JSON arguments'}])
            if 'submit_patch' in self.names and self.writer_calls==1:
                self.writer_calls+=1
                fixed=json.loads(messages[2].content.split('\n',1)[1])
                return AIMessage(content='```json\n'+json.dumps(draft(fixed))+'\n```')
            return super().invoke(messages,config)
    chat=FencedChat();eng,task,_=engine(tmp_path,chat)
    eng._run_task(task,'fenced-node-recovery',aliases={k:'A' for k in 'ABCDE'})
    assert chat.writer_calls==2 and chat.assess_calls==1
    assert len(eng.session.history)==1
    assert {n.id for n in eng.session.current.graph.nodes if n.node_type=='concept'}=={'A','C'}
