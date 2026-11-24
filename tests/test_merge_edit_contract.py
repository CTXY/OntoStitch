"""Tools must edit the same fields reviewers see and retain exact evidence refs."""
import json
import pytest
from langchain_core.messages import AIMessage
from test_light_reconciliation import setup
from test_incremental_agent import Chat
from test_relation_draft_recovery import setup_engine
from cortex.capabilities.graph_merge.reconciliation.context import node_view, relation_context
from cortex.capabilities.graph_merge.graph.edits import RelationPatch, submit_patch, evidence_sources
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.graph.batch import ActiveBatch, BatchResult
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
from cortex.capabilities.graph_merge.consolidation.review import Review
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import SourceReference


def test_definition_edit_changes_the_displayed_definition_not_description():
    ctx, base, a, b, _ = setup()
    old = next(n for n in base.graph.nodes if n.id == a)
    submit_patch(ctx, RelationPatch(node_updates=[{"id": a, "properties": {"definition": "A revised business definition."}, "explanation": "Correct the actual definition."}], explanation="Correct definition."))
    updated = next(n for n in ctx.active_batch.preview.graph.nodes if n.id == a)
    assert node_view(updated)["properties"]["definition"] == "A revised business definition."
    assert updated.description == old.description
    assert "meaning" not in node_view(updated)


def test_explicit_failure_terminates_without_another_model_call():
    ctx, base, a, b, _ = setup()
    model = Chat([AIMessage(content="",tool_calls=[{"name":"submit_patch","id":"end","args":{"failure":"A required source is unavailable.","explanation":"Cannot safely finish."}}])])
    from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
    with pytest.raises(IntegrationIncomplete,match="required source"):
        IncrementalIntegrationAgent(model,max_calls=None).invoke("reconcile",ctx,relation_context(ctx,base,LocalTask("terminal","reconcile",{a,b})))
    assert len(model.requests)==1 and ctx.active_batch.preview is None


def test_evidence_resolution_keeps_case_locator_and_chunk_exact():
    ctx,*_ = setup()
    ref=SourceReference(source_id="qa-source",case_id="case",locator="original-location",chunk_id="fragment")
    ctx.evidence=(EvidenceItem(id="sql-training:qa-source",source=ref,case_id="case",content={"question":"..."}),)
    assert evidence_sources(ctx,["sql-training:qa-source"])==(ref,)
    with pytest.raises(ValueError,match="Unknown evidence_ids"):
        evidence_sources(ctx,["unknown"])


def test_same_rejected_candidate_is_not_assessed_again(tmp_path):
    class Agent:
        def __init__(self):self.reviews=0;self.writes=0
        def invoke(self,phase,ctx,payload):
            if phase=="graph_assess":
                self.reviews+=1
                ref={"kind":"edge","id":ctx.input_edge_map["keep"],"fields":["properties.description"]}
                return Review(verdict="revise",issues=[{'refs':[ref], 'basis':[ref],
                    'problem':'The edge description remains incorrect.',
                    'required_outcome':'Preserve the original supported relation meaning.'}])
            self.writes+=1
            if self.writes > 1:
                assert payload["semantic_issues"][0]["problem"] == "The edge description remains incorrect."
                assert "refs" in payload["semantic_issues"][0]
            e=next(e for e in ctx.candidate.graph.edges if e.id==ctx.input_edge_map['keep']).model_dump(mode='json');e['properties']['description']='Pending, not yet accepted.'
            result=BatchResult.model_validate({'edits':[{'operation':'update_edge','edge':e}]})
            ctx.output_validator(result);return result
    agent=Agent();engine,task=setup_engine(agent,tmp_path/'work.sqlite');engine.max_rounds=5;original=engine.current
    with pytest.raises(IntegrationIncomplete,match="Unchanged.*candidate"):
        engine._run_task(task,'same-candidate')
    # The first Review pins complete basis records not all previously visible.
    # One expanded-context review is allowed; the next exact context is cached
    # before invoking Assessment again.
    assert agent.reviews==2 and agent.writes==3
    assert engine.current==original
    saved=WorkspaceStore(engine.workspace_path).load('same-candidate')
    assert saved['task_state']['stage']=='revising'
    assert saved['task_state']['agent_recovery']['assessment_gate']['cache_reused']


def test_true_noop_skips_review(tmp_path):
    class Agent:
        def invoke(self,phase,ctx,payload):
            assert phase=='reconcile'
            result=BatchResult();ctx.output_validator(result);return result
    engine,task=setup_engine(Agent(),tmp_path/'noop.sqlite');engine._run_task(task,'noop');assert engine.history[-1]['assessment']=='skipped_no_change'


def test_complete_conflict_resolves_evidence_to_exact_branch_sources():
    from test_graph_integration import catalog, concept, state
    from cortex.capabilities.graph_merge.inputs import IntegrationContext
    from cortex.capabilities.graph_merge.graph.batch import ActiveBatch
    from cortex.application.ontology_graph import GraphManager
    from cortex.schema import load_root_schema
    refs=[SourceReference(source_id=k,case_id=k,locator='QA',chunk_id='original') for k in ['one','two']]
    node=concept('owner','a').model_copy(update={'sources':tuple(refs)})
    base=state([*catalog(),node]);ctx=IntegrationContext(base,state(),load_root_schema())
    ctx.candidate=ctx.union_state();ctx.active_batch=ActiveBatch(ctx,ctx.candidate,targets={'owner'})
    ctx.evidence=tuple(EvidenceItem(id='sql-training:'+ref.source_id,source=ref,case_id=ref.case_id,content={'question':'...'} ) for ref in refs)
    content={k:node.model_dump(mode='json')[k] for k in ['name','description','aliases','tags','properties']}
    alternatives=[]
    for i,ref in enumerate(refs):
        value={**content,'description':f'Interpretation {i}', 'properties':{**content['properties'],'definition':f'Interpretation {i}'}}
        alternatives.append({'alternative_id':f'a{i}','content':value,'evidence_ids':['sql-training:'+ref.source_id]})
    submit_patch(ctx,RelationPatch(conflicts=[{'node_id':'owner','alternatives':alternatives,
        'conflict_points':[{'point_id':'point','aspect':'interpretation','description':'Different supported interpretations.',
            'choices':[{'alternative_ids':[f'a{i}'],'statement':f'Interpretation {i}'} for i in range(2)]}]}],explanation='Preserve complete alternatives and exact attribution.'))
    final=next(n for n in ctx.active_batch.preview.graph.nodes if n.id=='owner')
    assert len(final.alternatives)==2
    for i,a in enumerate(final.alternatives):
        assert a.sources==(refs[i],)
        resolved=GraphManager.resolve_alternative(final,a)
        assert resolved.properties['targets']==node.properties['targets']
        assert resolved.properties['definition']==f'Interpretation {i}'


def test_wire_guard_does_not_count_unsent_response_metadata():
    from cortex.capabilities.graph_merge.llm.budget import check_request, RequestInputTooLarge
    from langchain_core.messages import HumanMessage
    message=AIMessage(content="small",response_metadata={"audit":"x"*200000})
    assert check_request([message],[],max_characters=1000)<1000
    with pytest.raises(RequestInputTooLarge):
        check_request([HumanMessage(content="x"*2000)],[],max_characters=1000)






def test_explicit_existing_branch_resolution_projects_edges_and_lineage(tmp_path):
    from test_graph_integration import catalog, concept, edge, state
    from cortex.schema import GraphNode, load_root_schema
    from cortex.capabilities.graph_merge.inputs import IntegrationContext
    from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
    from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
    from cortex.capabilities.graph_merge.llm.incremental import NodeReview
    refs=[SourceReference(source_id=k) for k in ['one','two']]
    owner=GraphNode.model_validate({**concept('owner','a').model_dump(mode='json'),
        'merge_status':'conflict','consolidation_status':'semantic_conflict','sources':refs,
        'alternatives':[{'alternative_id':k,'overrides':{'description':k},'sources':[ref]} for k,ref in zip(['one','two'],refs)]})
    links=[edge(k,'owner','B','has_feature').model_copy(update={'properties':{'description':k,'alternative_ids':[k]},'sources':(ref,)}) for k,ref in zip(['one','two'],refs)]
    original=state([*catalog(),owner,concept('B','b')],links)
    ctx=IntegrationContext(original,state(),load_root_schema())
    class Agent:
        def invoke(self,phase,context,payload):
            if phase=='node_assess':return NodeReview(verdict='accept')
            result=BatchResult(resolutions=[{'node_id':'owner','keep_alternative_id':'one','discard_reasons':{'two':'The second interpretation is an explicitly identified source error in this controlled fixture.'}}])
            context.output_validator(result);return result
    engine=IncrementalGraphIntegrationEngine(Agent(),max_rounds=1,model_call_budget=None)
    engine.ctx=ctx;engine.current=ctx.union_state();engine.lineage=LineageIndex(ctx);engine._revision=0;engine._storage_revision=None;engine.reviewed_sources={};engine.identity_decision={};engine.workspace_path=tmp_path/'resolution.sqlite';engine.pending_review=None
    engine._run_task(LocalTask('resolve','consolidate',{'owner'}),'resolution')
    final=engine.current
    assert not next(n for n in final.graph.nodes if n.id=='owner').alternatives
    assert 'two' not in {e.id for e in final.graph.edges}
    assert 'alternative_ids' not in next(e for e in final.graph.edges if e.id=='one').properties
    assert len(engine.history)==1


def test_alternative_tool_schema_uses_evidence_ids_not_internal_sources():
    from cortex.capabilities.graph_merge.graph.edit_contract import CompleteAlternative
    fields=CompleteAlternative.model_json_schema()['properties']
    assert 'evidence_ids' in fields and 'sources' not in fields


def test_atomic_node_submission_restores_last_valid_buffer():
    ctx,base,a,b,_=setup();batch=ctx.active_batch
    raw=next(n for n in base.graph.nodes if n.id==a).model_dump(mode='json')
    raw['properties']['invented_property']='invalid'
    with pytest.raises(ValueError):
        batch.submit_atomic(BatchResult.model_validate({'edits':[{'operation':'update_node','node':raw}]}))
    assert not batch.pending and batch.preview is None


def test_matched_batching_measures_complete_context_without_splitting_identity():
    from cortex.capabilities.graph_merge.integration.two_stage import matched_batches
    from types import SimpleNamespace
    groups = [{'members': ['a', 'b']}, {'members': ['c']}, {'members': ['d']}]
    nodes = {k: SimpleNamespace(model_dump_json=lambda: '{}') for k in 'abcd'}
    result = matched_batches(groups, nodes, budget=10, measure=lambda gs: len(gs) * 7)
    assert result == [[groups[0]], [groups[1]], [groups[2]]]
    assert result[0][0]['members'] == ['a', 'b']


def test_distinct_required_reads_are_not_blocked_after_four_rounds():
    from langchain_core.messages import AIMessage, ToolMessage
    from test_graph_integration import catalog, concept, state
    from test_incremental_agent import Chat
    from cortex.capabilities.graph_merge.inputs import IntegrationContext
    from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
    from cortex.schema import load_root_schema
    ctx = IntegrationContext(state(), state([*catalog(), concept('N', 'a')]), load_root_schema())
    ctx.candidate = ctx.union_state()
    topics = ['concept', 'metric', 'event', 'generation_rule', 'relations', 'concept']
    responses = [AIMessage(content='', tool_calls=[{'name':'read_guidance','args':{'topic':topic},'id':str(i),'type':'tool_call'}]) for i,topic in enumerate(topics)]
    responses.append(AIMessage(content='', tool_calls=[{'name':'Review','args':{'verdict':'accept'},'id':'done','type':'tool_call'}]))
    chat = Chat(responses)
    result = IncrementalIntegrationAgent(chat,max_calls=None).invoke('node_assess',ctx,{'evidence':[]})
    replies = [json.loads(m.content) for m in chat.requests[-1] if isinstance(m,ToolMessage)]
    assert result.verdict == 'accept'
    assert all('Read budget exhausted' not in str(r) for r in replies)
    assert 'targets' in replies[-1]['fields']


def test_shared_guidance_is_not_rerendered_when_requested():
    from langchain_core.messages import AIMessage, ToolMessage
    from test_graph_integration import catalog, concept, state
    from cortex.capabilities.graph_merge.inputs import IntegrationContext
    from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
    from cortex.schema import load_root_schema
    ctx=IntegrationContext(state(),state([*catalog(),concept('N','a')]),load_root_schema());ctx.candidate=ctx.union_state()
    chat=Chat([AIMessage(content='',tool_calls=[{'name':'read_guidance','args':{'topic':'concept'},'id':'read','type':'tool_call'}]),AIMessage(content='',tool_calls=[{'name':'Review','args':{'verdict':'accept'},'id':'done','type':'tool_call'}])])
    IncrementalIntegrationAgent(chat,max_calls=None).invoke('node_assess',ctx,{'node_guidance':{'types':['concept'],'guidance':'Already complete'},'evidence':[]})
    result=json.loads(next(m.content for m in chat.requests[-1] if isinstance(m,ToolMessage)))
    assert 'guidance' in result
    assert 'targets' in result['fields']  # The fixture did not supply the field schema.


def test_misplaced_edge_feedback_names_the_wrapper_and_operation():
    edge={'id':'new-edge','edge_type':'depends_on','from_node_id':'a','to_node_id':'b','properties':{}}
    with pytest.raises(ValueError,match=r"edits\[0\].node contains an EDGE.*create_edge"):
        BatchResult.model_validate({'edits':[{'operation':'create_node','node':edge}]})


def test_new_node_evidence_ids_resolve_exact_sources():
    ctx,base,a,b,_=setup();batch=ctx.active_batch
    source=next(n for n in base.graph.nodes if n.id==a)
    from cortex.capabilities.graph_merge.models import EvidenceItem
    ctx.evidence=(EvidenceItem(id='support',case_id=source.sources[0].case_id or 'case',source=source.sources[0],content={'question':'Original evidence'}),)
    raw=source.model_dump(mode='json');raw['id']='new-concept';raw['sources']=[]
    batch.submit_atomic(BatchResult.model_validate({'edits':[{'operation':'create_node','node':raw,'evidence_ids':['support']}]}))
    assert next(n for n in batch.preview.graph.nodes if n.id=='new-concept').sources==source.sources[:1]


def test_pending_created_object_keeps_sources_on_later_update():
    ctx,base,a,b,_=setup();batch=ctx.active_batch
    source=next(n for n in base.graph.nodes if n.id==a)
    raw=source.model_dump(mode='json');raw['id']='pending-new'
    batch.submit_atomic(BatchResult.model_validate({'edits':[{'operation':'create_node','node':raw}]}))
    raw.pop('sources');raw['description']='A clarified description'
    batch.submit_atomic(BatchResult.model_validate({'edits':[{'operation':'update_node','node':raw}]}))
    assert next(n for n in batch.preview.graph.nodes if n.id=='pending-new').sources==source.sources


def test_read_attribution_matches_packet_without_touching_business_properties():
    from cortex.capabilities.graph_merge.evidence import compact_attribution
    from cortex.capabilities.graph_merge.models import EvidenceItem
    ctx,base,a,b,_=setup();node=next(n for n in base.graph.nodes if n.id==a)
    ctx.evidence=(EvidenceItem(id='proof',case_id='case',source=node.sources[0],content={'question':'Q'}),)
    row=node.model_dump(mode='json');row['properties']['sources']=['business data']
    result=compact_attribution({'items':[row]},ctx)['items'][0]
    assert result['evidence_ids']==['proof'] and 'sources' not in result
    assert result['properties']['sources']==['business data']


def test_partial_update_retains_name_bindings_and_sources():
    ctx,base,a,b,_=setup();batch=ctx.active_batch
    original=next(n for n in base.graph.nodes if n.id==a)
    batch.submit_atomic(BatchResult.model_validate({'edits':[{'operation':'update_node','node':{'id':a,'description':'Revised explanation'}}]}))
    after=next(n for n in batch.preview.graph.nodes if n.id==a)
    assert after.name==original.name and after.properties==original.properties
    assert after.sources==original.sources and after.description=='Revised explanation'




def test_repair_verification_reads_current_field_not_old_finding():
    from cortex.capabilities.graph_merge.consolidation.assessment import current_repair_fields
    ctx,base,a,b,_=setup()
    nodes=[n.model_copy(update={'description':'Corrected current description'}) if n.id==a else n for n in base.graph.nodes]
    current=base.model_copy(update={'graph':base.graph.model_copy(update={'nodes':tuple(nodes)})})
    findings=[{'issues':[{'problem':'Old description was wrong','refs':[{'id':a,'kind':'node','fields':['description']}]}]}]
    rows=current_repair_fields(current,findings)
    assert rows==[{'kind':'node','id':a,'view':'candidate','field':'description','value':'Corrected current description'}]


def test_conflict_union_stays_canonical_after_replaying_pending_edits():
    from test_graph_integration import catalog, concept, state
    from cortex.capabilities.graph_merge.inputs import IntegrationContext
    from cortex.schema import load_root_schema
    refs=[SourceReference(source_id='z-source'),SourceReference(source_id='a-source')]
    a=concept('A','a').model_copy(update={'sources':(refs[0],)})
    b=concept('B','a').model_copy(update={'sources':(refs[1],)})
    base=state([*catalog(),a,b]);ctx=IntegrationContext(base,state(),load_root_schema())
    batch=ActiveBatch(ctx,base,targets={'A'},aliases={'A':'A','B':'A'})
    alternatives=[]
    for i,ref in enumerate(refs):
        content={key:getattr(a,key) for key in ['name','description','aliases','tags','properties']};content['description']=str(i)
        alternatives.append({'alternative_id':str(i),'content':content,'sources':[ref]})
    batch.submit_atomic(BatchResult.model_validate({'conflicts':[{'node_id':'A','alternatives':alternatives,'conflict_points':[{'point_id':'p','aspect':'description','description':'Fixture alternatives','choices':[{'alternative_ids':['0'],'statement':'zero'},{'alternative_ids':['1'],'statement':'one'}]}]}]}))
    batch.submit_atomic(BatchResult())
    assert next(n for n in batch.preview.graph.nodes if n.id=='A').sources==(refs[1],refs[0])


@pytest.mark.parametrize("sentinel", ["null", " NULL ", "None", "", "nil", "undefined"])
def test_failure_sentinels_are_repairable_validation_errors(sentinel):
    with pytest.raises(ValueError, match="failure must be omitted or JSON null"):
        BatchResult.model_validate({"failure": sentinel})


def test_no_change_output_can_repair_null_string_in_same_episode():
    ctx, *_ = setup()
    model = Chat([
        AIMessage(content="", tool_calls=[{"name": "submit_patch", "id": "bad", "args": {"failure": "null", "explanation": "No change."}}]),
        AIMessage(content="", tool_calls=[{"name": "submit_patch", "id": "fixed", "args": {"failure": None, "explanation": "No change."}}]),
    ])
    result = IncrementalIntegrationAgent(model, max_calls=None).invoke("consolidate", ctx, {})
    assert result.failure is None and not result.node_updates
    assert len(model.requests) == 2
    assert "failure must be omitted or JSON null" in str(model.requests[1])
