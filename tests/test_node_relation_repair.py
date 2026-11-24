"""Coupled relation work is independently assessed and resumes without rewrite."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.consolidation.coordination import coordinate_node_relations
from cortex.capabilities.graph_merge.consolidation.relations import NodeRelationCoordinationRequired
from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch
from cortex.capabilities.graph_merge.consolidation.review import Review
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.schema import GraphNode, load_root_schema
from test_graph_integration import catalog, edge, state


def setup(agent):
    metric = GraphNode(id='metric',node_type='metric',name='count',
        properties={'expression':{'expr':'COUNT(${value})','refs':{'value':'a'}}})
    base = state([*catalog(),metric],[edge('operand','metric','a')])
    updated = metric.model_copy(update={'properties':{'expression':{'expr':'COUNT(${value})','refs':{'value':'b'}}}})
    intermediate = base.model_copy(update={'graph':base.graph.model_copy(update={
        'nodes':tuple(updated if node.id=='metric' else node for node in base.graph.nodes)})})
    error = NodeRelationCoordinationRequired(intermediate,owner_ids=['metric'],
        diagnostics=[{'code':'metric_operand_adjacency','owner_id':'metric','missing':[{'edge_type':'grounded_on','to_node_id':'b'}]}],
        followups=[{'edge_ids':['operand'],'problem':'Formula now uses the other confirmed operand.',
            'required_outcome':'Keep exact direct operand adjacency.'}],edge_aliases={'operand':'operand'})
    ctx = IntegrationContext(base,state(),load_root_schema())
    ctx.task_draft_id='node-draft';ctx.active_draft={'node':'original draft'}
    ctx.node_local_raw={'node':'raw'};ctx.node_local_compile=True
    ctx.active_task=LocalTask('node','consolidate',{'metric'})
    ctx.task_before=base;ctx.active_batch=object();ctx.candidate=base
    engine=SimpleNamespace(ctx=ctx,agent=agent,current=base)
    snapshots=[]
    def save(record):
        snapshots.append(deepcopy(record))
        # Simulate the real outer persist()/sync_draft_context() interference.
        ctx.active_draft={'node':'original draft'};ctx.task_draft_id='node-draft'
        ctx.task_draft_errors=('outer error',)
    return engine,base,error,snapshots,save


class Agent:
    def __init__(self,review='accept',interrupt=False):
        self.calls=[];self.review=review;self.interrupt=interrupt
    def invoke(self,phase,ctx,payload):
        self.calls.append(phase)
        assert ctx.node_local_compile is False
        assert ctx.task_identity_groups == []
        assert ctx.active_task.phase == phase
        assert payload['node_relation_obligations']['relation_followups'][0]['edge_ids']==['operand']
        if phase=='reconcile':
            patch=MergePatch.model_validate({'edge_updates':[{'id':'operand','to_node_id':'b'}],
                'explanation':'Align the direct relation with the confirmed formula operand.'})
            ctx.record_task_draft(patch.model_dump(mode='json',exclude_unset=True))
            assert ctx.task_draft_id != 'node-draft'
            ctx.compile_task_patch(patch)
            return patch
        if self.interrupt:
            raise ConnectionError('Test interrupted after the valid repair Patch was saved')
        if self.review=='accept':return Review(verdict='accept')
        return Review.model_validate({'verdict':'revise','issues':[{
            'refs':[{'kind':'edge','id':'operand','view':'candidate','fields':['properties.description']}],
            'basis':[{'kind':'edge','id':'operand','view':'before','fields':['properties.description']}],
            'problem':'A material restriction is not yet reflected in the relation description.',
            'required_outcome':'Preserve the original restriction.'}]})


def test_real_relation_compile_and_independent_assessment_restore_outer_context():
    agent=Agent();engine,base,error,snapshots,save=setup(agent)
    before=dict(engine.ctx.__dict__)
    result,record=coordinate_node_relations(engine,engine.ctx.active_task,base,error,
        save_record=save,run_id='bounded-test')
    assert agent.calls==['reconcile','graph_assess']
    assert engine.ctx.__dict__==before and engine.current==base
    assert next(e for e in result.state.graph.edges if e.id=='operand').to_node_id=='b'
    assert result.integrated==error.integrated_state
    assert record['stage']=='accepted' and record['review']['verdict']=='accept'
    assert record['review_candidate_hash']==canonical_hash(result.state)
    assert any(row.get('patch') and not row.get('review') for row in snapshots)
    assert result.coupled_relation_edge_aliases['operand']=='operand'


def test_saved_accepted_patch_recompiles_without_either_model_call():
    agent=Agent();engine,base,error,_,save=setup(agent)
    result,record=coordinate_node_relations(engine,engine.ctx.active_task,base,error,save_record=save)
    class NoCalls:
        def invoke(self,*args):raise AssertionError('Completed nested model work must not rerun')
    engine.agent=NoCalls()
    restored,_=coordinate_node_relations(engine,engine.ctx.active_task,base,error,saved=record,save_record=save)
    assert restored.state==result.state


def test_network_interruption_retains_patch_and_resume_calls_only_assessment():
    engine,base,error,snapshots,save=setup(Agent(interrupt=True))
    before=dict(engine.ctx.__dict__)
    with pytest.raises(ConnectionError):
        coordinate_node_relations(engine,engine.ctx.active_task,base,error,save_record=save)
    assert engine.ctx.__dict__==before
    saved=snapshots[-1]
    assert saved['patch'] and saved['stage']=='assessing' and 'review' not in saved
    next_agent=Agent();engine.agent=next_agent
    _,record=coordinate_node_relations(engine,engine.ctx.active_task,base,error,saved=saved,save_record=save)
    assert next_agent.calls==['graph_assess'] and record['stage']=='accepted'


def test_repeated_unchanged_feedback_stops_and_saved_rejection_is_not_replayed():
    engine,base,error,snapshots,save=setup(Agent(review='revise'))
    with pytest.raises(IntegrationIncomplete,match='checkpoint retained'):
        coordinate_node_relations(engine,engine.ctx.active_task,base,error,save_record=save)
    assert engine.agent.calls==['reconcile','graph_assess','reconcile','graph_assess']
    saved=snapshots[-1]
    assert saved['stage']=='assessment_rejected' and saved['review']['verdict']=='revise'
    engine.agent=SimpleNamespace(invoke=lambda *args:pytest.fail('Rejected identical saved candidate must not repeat model work'))
    with pytest.raises(IntegrationIncomplete):
        coordinate_node_relations(engine,engine.ctx.active_task,base,error,saved=saved,save_record=save)


def test_saved_binding_and_candidate_hash_are_verified_before_reuse():
    engine,base,error,_,save=setup(Agent())
    _,record=coordinate_node_relations(engine,engine.ctx.active_task,base,error,save_record=save)
    for field in ('draft_id','intermediate_hash','candidate_hash','patch_hash','review_candidate_hash'):
        altered={**record,field:'wrong'}
        with pytest.raises(ValueError):
            coordinate_node_relations(engine,engine.ctx.active_task,base,error,saved=altered,save_record=save)


def test_recovery_callback_persists_nested_scope_without_overwriting_local_draft():
    engine,base,error,snapshots,save=setup(Agent())
    records={}
    def persist_recovery(key,value):
        records[key]=value
        engine.ctx.active_draft={'node':'outer sync'};engine.ctx.task_draft_id='node-draft'
    engine.ctx.record_task_recovery=persist_recovery
    engine.ctx.load_task_recovery=lambda key:records.get(key)
    original_agent=engine.agent
    class RecoveryAgent:
        def invoke(self,phase,ctx,payload):
            if phase=='reconcile':
                ctx.record_task_draft({'explanation':'a local attempted patch'})
                draft_id=ctx.task_draft_id
                ctx.record_task_recovery('round',{'preserved':True})
                assert ctx.task_draft_id==draft_id and ctx.active_draft['explanation']=='a local attempted patch'
                assert ctx.load_task_recovery('round')=={'preserved':True}
            return original_agent.invoke(phase,ctx,payload)
    engine.agent=RecoveryAgent()
    coordinate_node_relations(engine,engine.ctx.active_task,base,error,save_record=save)
    assert list(records)==['node-relation:node-draft:round']
    assert engine.ctx.record_task_recovery is persist_recovery


def test_incoming_target_versions_reach_relation_agents_and_noop_review_keeps_focus():
    from cortex.application.ontology_graph import GraphManager
    from cortex.capabilities.graph_merge.consolidation.relations import sync_node_relations
    from cortex.schema import NodeContent, ConflictPoint
    from test_graph_integration import concept
    owner=concept('owner','a',name='category').model_copy(update={'properties':{'targets':[['a']],'definition':'First complete definition.'}})
    other=concept('other','a',name='category').model_copy(update={'properties':{'targets':[['a']],'definition':'Second complete definition.'}})
    source=concept('source','b',name='context')
    base=state([*catalog(),owner,other,source],[edge('incoming','source','other','has_feature')])
    def content(node):
        return NodeContent.model_validate(node.model_dump(mode='json',include={'name','description','aliases','tags','properties'}))
    point=ConflictPoint.model_validate({'point_id':'definition','aspect':'definition','description':'Different accepted definitions.',
        'choices':[{'alternative_ids':['first'],'statement':'First definition.'},
                   {'alternative_ids':['second'],'statement':'Second definition.'}]})
    conflict,_=GraphManager.compact_conflict(owner,[('first',content(owner),owner.sources),('second',content(other),other.sources)],conflict_points=(point,))
    intermediate=base.model_copy(update={'graph':base.graph.model_copy(update={
        'nodes':tuple(conflict if node.id=='owner' else node for node in base.graph.nodes if node.id!='other'),
        'edges':tuple(e.model_copy(update={'to_node_id':'owner'}) if e.id=='incoming' else e for e in base.graph.edges)})})
    try:
        sync_node_relations(base,intermediate,node_aliases={'owner':'owner','other':'owner'},edge_aliases={},owner_ids=['owner'])
    except NodeRelationCoordinationRequired as caught:
        error=caught
    else:pytest.fail('Controller must identify changed target-version semantics')
    calls=[]
    class NoopAgent:
        def invoke(self,phase,ctx,payload):
            calls.append(phase)
            obligation=payload['node_relation_obligations']
            assert 'original_targets' not in obligation
            assert 'original_edges' not in obligation
            assert obligation['diagnostics'][0]['target_owner_id']=='owner'
            assert 'candidate_target' not in obligation['diagnostics'][0]
            assert 'before_target' not in obligation['diagnostics'][0]
            if phase=='reconcile':return MergePatch(explanation='Test an independently reviewed no-op.')
            assert ctx.active_task.feedback==[{'kind':'node_relation_obligations'}]
            assert {'owner','source'} <= {node['id'] for node in payload['nodes']}
            assert any(edge['id']=='incoming' for edge in payload['edges'])
            assert any(row['node_id']=='owner' and len(row['alternatives'])==2 for row in payload['current_conflict_views'])
            return Review(verdict='accept')
    ctx=IntegrationContext(base,state(),load_root_schema())
    ctx.task_draft_id='node-draft';ctx.active_task=LocalTask('node','consolidate',{'owner'})
    engine=SimpleNamespace(ctx=ctx,agent=NoopAgent(),current=base)
    records=[]
    result,_=coordinate_node_relations(engine,ctx.active_task,base,error,save_record=lambda record:records.append(deepcopy(record)))
    assert calls==['reconcile','graph_assess'] and result.state==intermediate
    assert records[-1]['stage']=='accepted'


def test_retired_identity_edge_map_uses_disposition_not_alias_to_absent_edge():
    """Regress the real toxicology 204->266 accepted retirement Patch shape."""
    from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
    from cortex.capabilities.graph_merge.graph.edit_contract import Change, Element
    old='case204::edge.carcinogenic_count_depends_on_carcinogenic_molecule'
    survivor='case266::edge.carcinogenic_count_depends'
    owner='case266::metric.carcinogenic_molecule_count'
    other='case204::metric.carcinogenic_molecule_count'
    first=GraphNode(id=owner,node_type='metric',name='count',properties={'expression':{'expr':'COUNT(${value})','refs':{'value':'a'}}})
    second=first.model_copy(update={'id':other,'properties':{'expression':{'expr':'COUNT(${value})','refs':{'value':'b'}}}})
    base=state([*catalog(),first,second],[edge(old,owner,'a'),edge(survivor,other,'b')])
    merged=first.model_copy(update={'properties':second.properties})
    intermediate=base.model_copy(update={'graph':base.graph.model_copy(update={
        'nodes':tuple(merged if n.id==owner else n for n in base.graph.nodes if n.id!=other),
        'edges':tuple(e.model_copy(update={'from_node_id':owner}) if e.id==survivor else e for e in base.graph.edges)})})
    error=NodeRelationCoordinationRequired(intermediate,owner_ids=[owner],
        diagnostics=[{'code':'metric_operand_adjacency','owner_id':owner}],
        node_aliases={owner:owner,other:owner},edge_aliases={old:old,survivor:survivor},
        followups=[{'edge_ids':[old,survivor],'problem':'Stale nonoperand relation.',
                    'required_outcome':'Retain its knowledge through the actual correct-operand successor.'}])
    error.integration_changes=(Change(before=(Element(kind='node',id=owner),Element(kind='node',id=other)),
        after=(Element(kind='node',id=owner),),explanation='Integrate complete node contents.'),)
    class RetirementAgent:
        def invoke(self,phase,ctx,payload):
            if phase=='reconcile':return MergePatch.model_validate({'retirements':[{
                'before':[{'kind':'edge','id':old}], 'after':[{'kind':'edge','id':survivor}],
                'explanation':'Preserve the original input through the retained direct operand relation.'}],
                'explanation':'Accepted toxicology source retirement regression.'})
            return Review(verdict='accept')
    ctx=IntegrationContext(base,state(),load_root_schema())
    ctx.task_draft_id='real-retirement-draft';ctx.active_task=LocalTask('node','consolidate',{owner,other})
    engine=SimpleNamespace(ctx=ctx,agent=RetirementAgent(),current=base)
    result,saved=coordinate_node_relations(engine,ctx.active_task,base,error,save_record=lambda record:None)
    assert old not in result.coupled_relation_edge_aliases
    assert result.final_edge_aliases[old]==survivor
    lineage=LineageIndex(ctx)
    lineage.observe(base,result.integrated,result.integration_changes,
        node_aliases=result.node_aliases,edge_aliases=result.edge_aliases)
    lineage.observe(result.integrated,result.state,result.changes,
        node_aliases=result.coupled_relation_node_aliases,edge_aliases=result.coupled_relation_edge_aliases)
    assert any(ref.id==old for ref in lineage.origin_refs('edge',survivor))
    class NoCalls:
        def invoke(self,*args):pytest.fail('Accepted saved relation work must not be repeated')
    engine.agent=NoCalls()
    restored,_=coordinate_node_relations(engine,ctx.active_task,base,error,saved=saved,save_record=lambda record:None)
    assert restored.state==result.state


def test_real_saved_toxicology_retirement_record_preserves_lineage_and_sources():
    """Replay the exact failed live Patch and accepted repair with zero models."""
    import json
    from pathlib import Path
    from cortex.capabilities.graph_merge.graph.publication import Preview
    from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
    from cortex.capabilities.graph_merge.graph.edit_contract import IntegrationProposal
    from cortex.capabilities.graph_merge.consolidation.compiler import compile_patch
    from cortex.capabilities.graph_merge.graph.provenance import validate_rewrite_sources
    from cortex.capabilities.graph_merge.consolidation.contract import normalize_node_result
    from cortex.capabilities.graph_merge.models import EvidenceItem
    from cortex.schema import GraphSpace, GraphState
    fixture=json.loads((Path(__file__).parent/'fixtures/node_local_metric_retirement.json').read_text())
    base=GraphState.model_validate(fixture['before'])
    evidence=tuple(EvidenceItem.model_validate(row) for row in fixture['evidence'])
    ctx=IntegrationContext(base,GraphState(graph=GraphSpace()),load_root_schema(),evidence=evidence)
    group=fixture['group']
    ctx.node_local_compile=True;ctx.task_before=ctx.candidate=base
    ctx.task_draft_id=fixture['node_draft_id'];ctx.active_draft=fixture['node_draft']
    ctx.task_identity_groups=[group]
    ctx.task_visible_ids={node.id for node in base.graph.nodes}|{edge.id for edge in base.graph.edges}
    node_patch,_=normalize_node_result('writer',fixture['node_draft'],allowed_edge_ids={edge.id for edge in base.graph.edges})
    with pytest.raises(NodeRelationCoordinationRequired) as caught:
        compile_patch(ctx,base,node_patch,targets=set(group['member_ids']),
            aliases={member:group['owner_id'] for member in group['member_ids']},payload={})
    class NoCalls:
        def invoke(self,*args):pytest.fail('The exact accepted real relation Patch and Review must be reused')
    engine=SimpleNamespace(ctx=ctx,agent=NoCalls(),current=base)
    task=LocalTask('real-toxicology-retirement','consolidate',set(group['member_ids']))
    result,record=coordinate_node_relations(engine,task,base,caught.value,saved=fixture['coupled_record'],save_record=lambda record:None)
    lineage=LineageIndex(ctx)
    lineage.observe(base,result.integrated,result.integration_changes,
        node_aliases=result.node_aliases,edge_aliases=result.edge_aliases)
    lineage.observe(result.integrated,result.state,result.changes,
        node_aliases=result.coupled_relation_node_aliases,edge_aliases=result.coupled_relation_edge_aliases)
    proposal=IntegrationProposal(changes=lineage.source_changes(result.state))
    validate_rewrite_sources(ctx,proposal,Preview(result.state,lineage.node_map,lineage.edge_map,[]))
    old='case204::edge.carcinogenic_count_depends_on_carcinogenic_molecule'
    survivor='case266::edge.carcinogenic_count_depends'
    assert old not in {edge.id for edge in result.state.graph.edges}
    assert survivor in {edge.id for edge in result.state.graph.edges}
    assert result.final_edge_aliases[old]==survivor
    assert any(ref.id==old for ref in lineage.origin_refs('edge',survivor))
    original_edges={edge.id:edge for edge in base.graph.edges}
    kept=next(edge for edge in result.state.graph.edges if edge.id==survivor)
    assert set(original_edges[old].sources)|set(original_edges[survivor].sources) <= set(kept.sources)
    assert record['review']['verdict']=='accept'
