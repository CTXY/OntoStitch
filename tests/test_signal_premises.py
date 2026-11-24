"""One-signal complete query dependencies, independent from batching/revisions."""
from types import SimpleNamespace

import pytest

from cortex.capabilities.graph_merge.integration.signal_premises import premise_object, signal_key, signal_premise
from cortex.schema import GraphNode, GraphEdge, GraphSpace, GraphState, SourceReference, load_root_schema


def node(ident, kind='concept', **props):
    return GraphNode(id=ident,node_type=kind,name=ident,properties=props)


def edge(ident, a, b, predicate='is_a', **props):
    return GraphEdge(id=ident,from_node_id=a,to_node_id=b,edge_type=predicate,
                     properties={'description':'A sourced relationship.',**props})


def setup(nodes=(), edges=(), contracts=()):
    return SimpleNamespace(schema=load_root_schema(),evidence=[]), GraphState(graph=GraphSpace(
        nodes=tuple(nodes),edges=tuple(edges),relation_contracts=tuple(contracts)))


def add(current, *, nodes=(), edges=(), contracts=()):
    return current.model_copy(update={'graph':current.graph.model_copy(update={
        'nodes':(*current.graph.nodes,*nodes),'edges':(*current.graph.edges,*edges),
        'relation_contracts':(*current.graph.relation_contracts,*contracts)})})


def check(kind, **fields):
    return {'id':'stable-signal','kind':kind,**fields}


def exception_contract(ctx):
    return ctx.schema.edge_type_index()['applies_to'].model_copy(update={
        'id':'has_exception','name':'has exception','description':'The evidenced exceptional business scope.',
        'from_node_types':['concept','generation_rule','metric'],
        'to_node_types':['concept','generation_rule','metric']})


def test_signal_key_and_premise_ignore_grouping_graph_order_and_set_order():
    ctx,state=setup([node('A'),node('B'),node('C')],[edge('E','A','B'),edge('F','A','C')])
    s=check('same_role_neighbors',edge_ids=['E','F'])
    changed=state.model_copy(update={'graph':state.graph.model_copy(update={
        'nodes':tuple(reversed(state.graph.nodes)),'edges':tuple(reversed(state.graph.edges))})})
    assert signal_key(s)==signal_key({**s,'edge_ids':['F','E']})
    assert signal_premise(ctx,state,s)==signal_premise(ctx,changed,{**s,'edge_ids':['F','E']})
    ctx.workspace_revision=123
    ctx.active_task=SimpleNamespace(id='other-batch',targets={'unrelated'})
    assert signal_premise(ctx,state,s)==signal_premise(ctx,changed,s)


def test_sql_dialect_is_a_semantic_premise_not_a_task_grouping_detail():
    ctx,current=setup([node('A'),node('B')],[edge('E','A','B')])
    s=check('triangle',node_ids=['A','B'],edge_ids=['E'])
    ctx.sql_dialect='sqlite';before=signal_premise(ctx,current,s)
    ctx.sql_dialect='postgres'
    assert signal_premise(ctx,current,s)!=before


def test_shared_object_projection_normalizes_sets_without_dropping_sources():
    refs=(SourceReference(source_id='B'),SourceReference(source_id='A'))
    original=node('N',targets=[['second'],['first']]).model_copy(update={
        'sources':refs,'aliases':['z','a'],'tags':['z','a']})
    reordered=original.model_copy(update={'sources':tuple(reversed(refs)),
        'aliases':['a','z'],'tags':['a','z']})
    assert premise_object(original)==premise_object(reordered)
    assert premise_object(original)['properties']['targets']==[['second'],['first']]
    assert [s['source_id'] for s in premise_object(original)['sources']]==['A','B']
    original_edge=edge('E','A','B',alternative_ids=['z','a']).model_copy(update={'sources':refs})
    reordered_edge=original_edge.model_copy(update={'sources':tuple(reversed(refs)),
        'properties':{**original_edge.properties,'alternative_ids':['a','z']}})
    assert premise_object(original_edge)==premise_object(reordered_edge)
    assert len(premise_object(original_edge)['sources'])==2


def test_related_rules_do_not_expand_other_scopes_attached_to_operand_hub():
    ctx,state=setup([node('R','generation_rule',condition='all',action='rank'),
        node('R2','generation_rule'),node('Hub'),node('Elsewhere')],
        [edge('E','R','Hub','applies_to')])
    state=add(state,contracts=[exception_contract(ctx)])
    s=check('related_decisions',node_ids=['R'],shared_operands=['Hub'])
    before=signal_premise(ctx,state,s)
    assert before==signal_premise(ctx,add(state,edges=[edge('X','R2','Hub','applies_to'),
        edge('Y','Hub','Elsewhere')]),s)
    assert before!=signal_premise(ctx,add(state,edges=[edge('Z','R','Elsewhere','applies_to')]),s)
    assert before!=signal_premise(ctx,add(state,edges=[edge('Role','R','Elsewhere','has_exception')]),s)


@pytest.mark.parametrize('reverse',[False,True])
def test_source_consumer_other_direct_roles_invalidate_without_operand_crawl(reverse):
    ctx,state=setup([node('C',targets=[['P']]),node('P','column'),
        node('R','generation_rule'),node('Operand'),node('Elsewhere')],
        [edge('Input','R','C','applies_to'),edge('Scope','R','Operand','applies_to')])
    state=add(state,contracts=[exception_contract(ctx)])
    s=check('source_selection_variants',node_ids=['C'],variants=[])
    before=signal_premise(ctx,state,s)
    pair=('Elsewhere','R') if reverse else ('R','Elsewhere')
    assert before!=signal_premise(ctx,add(state,edges=[edge('Role',*pair,'has_exception')]),s)
    assert before==signal_premise(ctx,add(state,edges=[edge('Unrelated','Operand','Elsewhere')]),s)


def test_related_rule_sql_reference_does_not_import_unrelated_grounding_owner():
    ctx,state=setup([node('R','generation_rule',references=['Physical']),node('Operand'),
        node('Physical','column'),node('OtherConcept')], [edge('E','R','Operand','applies_to')])
    s=check('related_decisions',node_ids=['R'],shared_operands=['Operand'])
    before=signal_premise(ctx,state,s)
    after=add(state,edges=[edge('Owner','OtherConcept','Physical','grounded_on')])
    assert before==signal_premise(ctx,after,s)
    revised=state.model_copy(update={'graph':state.graph.model_copy(update={'nodes':tuple(
        n.model_copy(update={'properties':{'data_type':'INTEGER'}}) if n.id=='Physical' else n
        for n in state.graph.nodes)})})
    assert before!=signal_premise(ctx,revised,s)


def test_source_selection_tracks_new_consumer_even_if_required_edge_missing():
    ctx,state=setup([node('C',targets=[['P']]),node('P','column'),node('Other')])
    s=check('source_selection_variants',node_ids=['C'],variants=[])
    before=signal_premise(ctx,state,s)
    more=add(state,nodes=[node('M','metric',expression={'expression':'COUNT(C)','refs':{'c':'C'}})])
    assert before!=signal_premise(ctx,more,s)
    with_edge=add(state,nodes=[node('R','generation_rule')],edges=[edge('E','R','C','applies_to')])
    assert before!=signal_premise(ctx,with_edge,s)
    assert before==signal_premise(ctx,add(state,nodes=[node('Unused')],edges=[edge('X','Other','Unused')]),s)


def test_physical_owner_query_detects_new_claimant_not_just_known_edge_ids():
    ctx,state=setup([node('A',targets=[['P']]),node('B'),node('P','column')],
                    [edge('E','A','P','grounded_on')])
    s=check('binding_contest',node_ids=['A'],edge_ids=['E'],physical_id='P')
    assert signal_premise(ctx,state,s)!=signal_premise(ctx,
        add(state,edges=[edge('F','B','P','grounded_on')]),s)


@pytest.mark.parametrize('kind', ['parallel_assertions','triangle'])
def test_directed_pair_query_detects_new_edge_and_scope_changes(kind):
    ctx,state=setup([node('A'),node('B'),node('Other')],[edge('E','A','B')])
    s=check(kind,node_ids=['A','B'],edge_ids=['E'])
    before=signal_premise(ctx,state,s)
    assert before!=signal_premise(ctx,add(state,edges=[edge('F','A','B')]),s)
    revised=state.model_copy(update={'graph':state.graph.model_copy(update={
        'edges':(edge('E','A','B',alternative_ids=['a']),)})})
    assert before!=signal_premise(ctx,revised,s)
    assert before==signal_premise(ctx,add(state,edges=[edge('X','B','Other')]),s)


def test_same_role_membership_query_detects_new_neighbor():
    ctx,state=setup([node('A'),node('B'),node('C')],[edge('E','A','B')])
    s=check('same_role_neighbors',edge_ids=['E'])
    assert signal_premise(ctx,state,s)!=signal_premise(ctx,add(state,edges=[edge('F','A','C')]),s)


def test_value_concepts_tracks_new_catalog_value_and_preexisting_column_concept():
    ctx,state=setup([node('Column','column'),node('V','dimension_value',column_ref='Column',value='yes'),
        node('C',targets=[['V']])],[edge('E','C','V','grounded_on')])
    s=check('value_concepts',column_id='Column',members=[{'concept':'C','value':'V'}])
    before=signal_premise(ctx,state,s)
    assert before!=signal_premise(ctx,add(state,nodes=[node('V2','dimension_value',column_ref='Column',value='no')]),s)
    assert before!=signal_premise(ctx,add(state,nodes=[node('ColumnConcept',targets=[['Column']])]),s)


def test_relevant_sources_evidence_body_and_contract_changes_invalidate():
    source=SourceReference(source_id='source')
    n=node('A').model_copy(update={'sources':(source,)})
    ctx,state=setup([n,node('B')],[edge('E','A','B')])
    ctx.evidence=[SimpleNamespace(id='ev',source=source,content={'question':'original'})]
    s=check('triangle',node_ids=['A','B'],edge_ids=['E'])
    before=signal_premise(ctx,state,s)
    ctx.evidence[0].content={'question':'changed'}
    assert before!=signal_premise(ctx,state,s)
    ctx.evidence[0].content={'question':'original'}
    revised=state.model_copy(update={'graph':state.graph.model_copy(update={'nodes':(
        n.model_copy(update={'sources':()}),node('B'))})})
    assert before!=signal_premise(ctx,revised,s)
    old=ctx.schema.edge_type_index()['is_a']
    ctx.schema=ctx.schema.model_copy(update={'edge_types':tuple(
        c.model_copy(update={'description':'Different meaning'}) if c.id==old.id else c for c in ctx.schema.edge_types)})
    assert before!=signal_premise(ctx,state,s)


def test_abstract_catalog_new_applicable_predicate_changes_premise():
    ctx,state=setup([node('A'),node('B')],[edge('E','A','B','has_attribute')])
    s=check('abstract_predicate',node_ids=['A','B'],edge_ids=['E'])
    before=signal_premise(ctx,state,s)
    contract=ctx.schema.edge_type_index()['has_attribute'].model_copy(update={
        'id':'has_new_role','extends':'has_attribute'})
    assert before!=signal_premise(ctx,add(state,contracts=[contract]),s)


def test_explicit_obligation_references_and_missing_objects_are_bound():
    ctx,state=setup([node('A'),node('B')],[edge('E','A','B')])
    s=check('node_relation_followup',observed={'refs':[{'kind':'edge','id':'E'}],
        'node_ids':['A'],'required_outcome':'Preserve the stated role'})
    before=signal_premise(ctx,state,s)
    assert {n['id'] for n in before['nodes']}=={'A','B'}
    missing=state.model_copy(update={'graph':state.graph.model_copy(update={'edges':()})})
    after=signal_premise(ctx,missing,s)
    assert after['missing_edge_ids']==['E'] and after!=before


def test_source_premise_keeps_branch_body_and_honors_source_order_independence():
    from cortex.schema import ConflictAlternative
    # Use schema-level construction without normalization: the premise must bind
    # even unpublished competing content exactly, without resolving a winner.
    source=SourceReference(source_id='first');other=SourceReference(source_id='second')
    a=node('A');a=a.model_copy(update={'merge_status':'conflict',
        'consolidation_status':'unresolved','sources':(source,other),'alternatives':(
        ConflictAlternative(alternative_id='alt',overrides={'description':'First meaning'},sources=(source,)),
        ConflictAlternative(alternative_id='other',overrides={'description':'Other meaning'},sources=(other,)))})
    ctx,state=setup([a,node('B')],[edge('E','A','B')])
    s=check('triangle',node_ids=['A','B'],edge_ids=['E'])
    before=signal_premise(ctx,state,s)
    changed=a.model_copy(update={'alternatives':(a.alternatives[0].model_copy(update={
        'overrides':{'description':'Competing changed meaning'}}),a.alternatives[1])})
    more=state.model_copy(update={'graph':state.graph.model_copy(update={'nodes':(changed,node('B'))})})
    assert before!=signal_premise(ctx,more,s)
