from types import SimpleNamespace

from test_graph_integration import catalog, concept, edge, state

from cortex.capabilities.graph_merge.reconciliation.receipts import dependency_facts, read_dependencies
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import ConflictAlternative, GraphSpace, GraphState, SourceReference, load_root_schema


def test_supplementary_common_source_evidence_is_a_real_dependency():
    source=SourceReference(source_id='common')
    left,right=SourceReference(source_id='left'),SourceReference(source_id='right')
    node=concept('A','a').model_copy(update={'merge_status':'conflict','consolidation_status':'unresolved','sources':(source,left,right),
        'common_sources':(source,), 'alternatives':(
            ConflictAlternative(alternative_id='left',overrides={'description':'First version'},sources=(left,)),
            ConflictAlternative(alternative_id='right',overrides={'description':'Second version'},sources=(right,)))})
    graph=GraphState(graph=GraphSpace(nodes=(*catalog(),node)))
    ctx=SimpleNamespace(schema=load_root_schema(),evidence=(EvidenceItem(
        id='ev',source=source,case_id=None,content={'question':'original'}),))
    queries=[{'name':'read_nodes','args':{'ids':['A']}}]
    original=dependency_facts(ctx,graph,queries)
    ctx.evidence=(ctx.evidence[0].model_copy(update={'content':{'question':'corrected'}}),)
    assert dependency_facts(ctx,graph,queries)!=original


def test_supplementary_query_order_does_not_create_duplicate_dependencies():
    snapshots={'one':{'runtime':{'journal':[{'name':'read_nodes','args':{'ids':['A','B']}}]}},
        'two':{'runtime':{'journal':[{'name':'read_nodes','args':{'ids':['B','A','A']}}]}}}
    assert read_dependencies(SimpleNamespace(agent_recovery=snapshots))==[
        {'name':'read_nodes','args':{'ids':['A','B']}}]


def test_supplementary_owner_absence_query_observes_new_owner():
    graph=state([*catalog(),concept('A','a')])
    ctx=SimpleNamespace(schema=load_root_schema(),evidence=())
    queries=[{'name':'counterexample','args':{'kind':'concept_owner_absent',
        'from_node_id':None,'to_node_id':'a','edge_type':'grounded_on','alternative_id':None}}]
    original=dependency_facts(ctx,graph,queries)
    added=graph.model_copy(update={'graph':graph.graph.model_copy(update={
        'edges':(*graph.graph.edges,edge('E','A','a','grounded_on'))})})
    assert dependency_facts(ctx,added,queries)!=original


def test_supplementary_node_read_ignores_catalog_storage_metadata():
    physical=next(n for n in catalog() if n.id=='a')
    physical=physical.model_copy(update={'properties':{**physical.properties,'data_type':'TEXT','sample_values':['a']}})
    graph=GraphState(graph=GraphSpace(nodes=(catalog()[0],physical)));ctx=SimpleNamespace(schema=load_root_schema(),evidence=())
    queries=[{'name':'read_nodes','args':{'ids':['a']}}]
    changed=graph.model_copy(update={'graph':graph.graph.model_copy(update={'nodes':(
        physical.model_copy(update={'properties':{**physical.properties,'sample_values':['different'],'source_metadata':{'internal':'changed'}}}),)})})
    assert dependency_facts(ctx,graph,queries)==dependency_facts(ctx,changed,queries)
