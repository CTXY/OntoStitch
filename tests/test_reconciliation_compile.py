"""Exact source movement, safe alternatives and atomic local compilation."""
from types import SimpleNamespace
import pytest
from test_graph_integration import catalog, concept, edge, state
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.reconciliation_compile import compile_draft
from cortex.capabilities.graph_merge.integration.reconciliation_contract import DraftDocument
from cortex.capabilities.graph_merge.integration.task_state import canonical_hash
from cortex.schema import GraphSpace, GraphState, NodeContent, SourceReference, load_root_schema


def setup():
    base=state([*catalog(),concept('A','a'),concept('B','b')],[edge('ea','A','a').model_copy(update={'sources':(SourceReference(source_id='case-a'),)}),
        edge('eb','B','b').model_copy(update={'sources':(SourceReference(source_id='case-b'),)})])
    ctx=IntegrationContext(base,GraphState(graph=GraphSpace()),load_root_schema());ctx.candidate=base
    return ctx,base


def run(ctx,base,changes=(),*,verify=True):
    doc=DraftDocument(task_id='test',before_id=canonical_hash(base),version=1,changes=changes)
    result=compile_draft(ctx,base,doc,targets={n.id for n in base.graph.nodes},payload={'task_id':'test'})
    if verify:
        LineageIndex(ctx).observe(base,result.state,result.changes,node_aliases=result.node_aliases,edge_aliases=result.edge_aliases)
    return result


def row(kind,**values):
    return {'change_id':values.pop('change_id','c'),'kind':kind,'reason':'Retain the actual business assertion.',**values}


def evidence(ctx):
    ctx.evidence=(SimpleNamespace(id='known',source=SourceReference(source_id='extra'),content={}),)


def content(node): return {k:getattr(node,k) for k in NodeContent.model_fields}


def test_noop_keeps_real_lineage():
    ctx,base=setup();assert run(ctx,base).state==base


def test_node_and_relation_update_are_atomic_and_keep_sources():
    ctx,base=setup()
    output=run(ctx,base,[row('node_update',node_id='A',set={'name':'Objects'}),
        row('edge_update',change_id='d',edge_id='ea',set={'properties':{'description':'The object is represented by a.'}})])
    assert next(n for n in output.state.graph.nodes if n.id=='A').name=='Objects'
    assert next(e for e in output.state.graph.edges if e.id=='ea').sources==next(e for e in base.graph.edges if e.id=='ea').sources
    assert next(n for n in base.graph.nodes if n.id=='A').name=='A'


def test_complete_creation_requires_real_evidence():
    ctx,base=setup();evidence(ctx)
    output=run(ctx,base,[row('node_create',node_id='C',node_type='concept',content=content(concept('C','c')),evidence_ids=['known']),
        row('edge_create',change_id='d',edge_id='ec',edge_type='grounded_on',from_node_id='C',to_node_id='c',
            properties={'description':'The feature is represented by c.'},evidence_ids=['known'])])
    assert next(n for n in output.state.graph.nodes if n.id=='C').sources==(ctx.evidence[0].source,)
    with pytest.raises(ValueError,match='UNKNOWN_EVIDENCE'):
        run(ctx,base,[row('node_create',node_id='C',node_type='concept',content=content(concept('C','c')),evidence_ids=['unknown'])])


def test_failed_compile_restores_candidate_and_batch():
    ctx,base=setup();batch=object();ctx.active_batch=batch
    with pytest.raises(ValueError): run(ctx,base,[row('edge_update',edge_id='ea',set={'to_node_id':'missing'})])
    assert ctx.candidate is base and ctx.active_batch is batch


def test_replacement_has_exact_relation_source_union():
    ctx,base=setup()
    # Two different representations can have an explicit semantic successor;
    # replacement does not claim node identity or broadcast sources.
    out=run(ctx,base,[row('edge_replace',edge_id='eb',replacement_edge_id='ea')])
    survivor=next(e for e in out.state.graph.edges if e.id=='ea')
    assert set(survivor.sources)=={s for e in base.graph.edges for s in e.sources}
    assert not out.edge_aliases and out.final_edge_aliases=={'eb':'ea'}
    assert 'eb' not in {e.id for e in out.state.graph.edges}


def test_missing_successor_rejected_before_source_loss():
    ctx,base=setup()
    with pytest.raises(ValueError,match='MISSING_EDGE_SUCCESSOR'):
        run(ctx,base,[row('edge_replace',edge_id='eb',replacement_edge_id='absent')])
    assert ctx.candidate==base


def test_wrong_assertion_removal_has_visible_before_basis_and_audit():
    ctx,base=setup()
    out=run(ctx,base,[row('edge_remove',edge_id='eb',refs=[{'kind':'node','view':'before','id':'B','field':'properties.targets'}])])
    assert 'eb' not in {e.id for e in out.state.graph.edges}
    audit=next(x for x in out.extensions if x.get('type')=='edge_removal')
    assert audit['removed_sources'] and audit['refs'][0]['field']=='properties.targets'


def test_removal_cannot_cite_private_source_storage():
    ctx,base=setup()
    with pytest.raises(ValueError,match='NONPUBLIC_REFERENCE_FIELD'):
        run(ctx,base,[row('edge_remove',edge_id='eb',refs=[{'kind':'edge','view':'before','id':'eb','field':'sources'}])])


def test_wrong_fixed_baseline_is_rejected():
    ctx,base=setup()
    with pytest.raises(ValueError,match='BEFORE_MISMATCH'):
        compile_draft(ctx,base,DraftDocument(task_id='test',before_id='old',version=0),targets={'A'},payload={'task_id':'test'})


def test_conflict_content_is_readonly_and_removed_operation_is_rejected():
    from test_merge_identity_integration_v2 import conflicted
    owner=conflicted('A',['one','two'])
    base=state([*catalog(),owner,concept('B','b')],[])
    ctx=IntegrationContext(base,GraphState(graph=GraphSpace()),load_root_schema());ctx.candidate=base
    with pytest.raises(ValueError,match='CONFLICT_CONTENT_READONLY'):
        run(ctx,base,[row('node_update',node_id='A',set={'name':'chosen'})])
    with pytest.raises(ValueError,match='union_tag_invalid'):
        run(ctx,base,[row('alternative_update',node_id='A',alternative_id='one',set={'description':'Changed'})])
    assert ctx.candidate==base


def test_real_toxicology_fixture_retains_exact_edge_sources():
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
    from reconciliation_probe_fixtures import real_metric_fixture,semantic_oracle
    data=real_metric_fixture(Path('tests/fixtures/node_local_metric_retirement.json'))
    ctx=IntegrationContext(data.before,GraphState(graph=GraphSpace()),load_root_schema(),evidence=data.evidence);ctx.candidate=data.before
    result=run(ctx,data.before,data.changes)
    semantic_oracle(result.state,data)


def test_conflict_cannot_be_bypassed_by_redirecting_to_ordinary_branch_clone():
    from cortex.application.ontology_graph import GraphManager
    from test_merge_identity_integration_v2 import conflicted
    owner=conflicted('A',['one','two'])
    base=state([*catalog(),owner,concept('B','b')],[edge('consumer','B','A','has_feature')])
    ctx=IntegrationContext(base,GraphState(graph=GraphSpace()),load_root_schema());ctx.candidate=base;evidence(ctx)
    branch=GraphManager.resolve_alternative(owner,owner.alternatives[0])
    with pytest.raises(ValueError,match='CONFLICT_BRANCH_CLONE_FORBIDDEN'):
        run(ctx,base,[row('node_create',node_id='clone',node_type='concept',content=branch.model_dump(),evidence_ids=['known']),
            row('edge_update',change_id='redirect',edge_id='consumer',set={'to_node_id':'clone'})])
    assert ctx.candidate==base


def test_branch_grounding_must_match_every_applicable_source_version():
    from test_merge_identity_integration_v2 import conflicted
    owner=conflicted('A',['one','two'])
    scoped=edge('scope','A','a').model_copy(update={'properties':{
        'description':'This representation belongs to the first version.','alternative_ids':['one']}})
    base=state([*catalog(),owner],[scoped])
    ctx=IntegrationContext(base,GraphState(graph=GraphSpace()),load_root_schema());ctx.candidate=base
    with pytest.raises(ValueError):
        run(ctx,base,[row('edge_update',edge_id='scope',set={'to_node_id':'b'})])
    assert ctx.candidate==base


@pytest.mark.parametrize('scope',[['one'],['one','two']])
def test_relation_branch_scope_is_editable_while_full_alternatives_stay_readonly(scope):
    from test_merge_identity_integration_v2 import conflicted
    owner=conflicted('A',['one','two'])
    scoped=edge('scope','A','a').model_copy(update={'properties':{
        'description':'The specified source version uses physical matching a.','alternative_ids':['one']}})
    base=state([*catalog(),owner],[scoped])
    ctx=IntegrationContext(base,GraphState(graph=GraphSpace()),load_root_schema());ctx.candidate=base
    out=run(ctx,base,[row('edge_update',edge_id='scope',set={'properties':{
        'description':'Physical matching a applies to the explicitly named source versions.','alternative_ids':scope}})])
    assert next(n for n in out.state.graph.nodes if n.id=='A')==owner
    assert next(e for e in out.state.graph.edges if e.id=='scope').properties['alternative_ids']==scope


def test_unknown_source_branch_is_rejected_without_modifying_graph():
    from test_merge_identity_integration_v2 import conflicted
    owner=conflicted('A',['one','two'])
    base=state([*catalog(),owner],[edge('scope','A','a').model_copy(update={'properties':{
        'description':'Physical matching for the source version.','alternative_ids':['one']}})])
    ctx=IntegrationContext(base,GraphState(graph=GraphSpace()),load_root_schema());ctx.candidate=base
    with pytest.raises(ValueError):
        run(ctx,base,[row('edge_update',edge_id='scope',set={'properties':{'alternative_ids':['missing']}})])
    assert ctx.candidate==base
