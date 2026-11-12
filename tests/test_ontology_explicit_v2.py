"""Business matches and authored adjacency have deliberately different cardinality."""
import pytest

from cortex.application.ontology_graph import GraphManager
from cortex.schema import load_root_schema


def node(key, kind, props, description=None):
    return {'operation': 'create_node', 'node': {'id': key, 'node_type': kind,
            'name': key, 'description': description, 'properties': props}}


def edge(key, source, target, kind='grounded_on', description='Uses this catalog endpoint to identify the business meaning.'):
    return {'operation': 'create_edge', 'edge': {'id': key, 'edge_type': kind,
            'from_node_id': source, 'to_node_id': target,
            'properties': {'description': description}}}


def apply(manager, *ops, **kwargs):
    return manager.apply(list(ops), expected_revision=manager.revision,
                         request_id=f'edit-{manager.revision}-{len(ops)}', **kwargs)


@pytest.fixture
def graph():
    graph = GraphManager(load_root_schema())
    result = apply(graph,
        node('table.schools','table',{'table_name':'schools'}),
        node('column.schools.id','column',{'column_name':'id','table_ref':'table.schools'}),
        node('column.schools.open_date','column',{'column_name':'open_date','table_ref':'table.schools'}))
    assert result.status == 'applied', result.errors
    return graph


def test_overlapping_targets_are_not_duplicate_edges(graph):
    result = apply(graph,
        node('concept.school','concept',{'targets':[['column.schools.id']]}),
        node('concept.opened_school','concept',{'targets':[['column.schools.id','column.schools.open_date']]}),
        edge('school-id','concept.school','column.schools.id'),
        edge('opened-date','concept.opened_school','column.schools.open_date'),
        edge('opened-school','concept.opened_school','concept.school','is_a',
             'An opened school is a School identified under the case-specific opening definition.'))
    assert result.status == 'applied', result.errors
    assert not any(e.from_node_id=='concept.opened_school' and e.to_node_id=='column.schools.id'
                   for e in graph.get_edges())


def test_duplicate_direct_grounding_is_rejected_atomically(graph):
    before=graph.revision
    result=apply(graph,
        node('concept.a','concept',{'targets':[['column.schools.id']]}),
        node('concept.b','concept',{'targets':[['column.schools.id']]}),
        edge('a-id','concept.a','column.schools.id'),
        edge('b-id','concept.b','column.schools.id'))
    assert result.status=='rejected'
    assert any(e.code=='DUPLICATE_CONCEPT_GROUNDING' for e in result.errors)
    assert graph.revision==before


def test_matching_groups_do_not_generate_semantic_edges(graph):
    result=apply(graph,node('concept.school','concept',{'targets':[['column.schools.id']]}))
    assert result.status=='applied',result.errors
    assert not graph.get_edges('concept.school')


def test_empty_concept_match_rejected(graph):
    result=apply(graph,node('concept.school','concept',{'targets':[]}))
    assert result.status=='rejected'


def test_formula_uses_concept_operands_and_explicit_relations(graph):
    result=apply(graph,
        node('concept.school','concept',{'targets':[['column.schools.id']]}),
        node('metric.school_count','metric',{'expression':{'expr':'COUNT(${school})','refs':{'school':'concept.school'}}}),
        edge('count-school','metric.school_count','concept.school','depends_on','School supplies the population being counted.'))
    assert result.status=='applied',result.errors


def test_missing_formula_relation_is_feedback_not_inferred(graph):
    result=apply(graph,node('metric.count','metric',{'expression':{'expr':'COUNT(${school})','refs':{'school':'column.schools.id'}}}))
    assert result.status=='rejected'
    assert any(e.code=='MISSING_FORMULA_RELATION' for e in result.errors)
    assert graph.get_node('metric.count') is None


def test_business_relation_needs_description(graph):
    result=apply(graph,
        node('concept.school','concept',{'targets':[['column.schools.id']]}),
        edge('bad','concept.school','column.schools.id',description=''))
    assert result.status=='rejected'


def test_distinct_event_role_predicates_are_preserved(graph):
    from cortex.schema.models import EdgeType

    result=apply(graph,
        node('concept.school','concept',{'targets':[['column.schools.id']]}),
        node('event.opening','event',{'definition':'The establishment of a school, with its recorded opening time.','targets':[]}),
        edge('opens','event.opening','concept.school','opens','School is the entity established by the opening.'),
        edge('date','event.opening','column.schools.open_date','recorded_at','The column records the opening time; missing dates do not establish nonoccurrence.'),
        relation_contracts=(
            EdgeType(id="opens", name="Opens", description="An opening occurrence points to the entity opened.",
                from_node_types=["event"], to_node_types=["concept"],
                direction_semantics="Occurrence toward the entity opened.",
                evidence_requirement="The entity and occurrence are supported as corresponding."),
            EdgeType(id="recorded_at", name="Recorded at", description="An event points to its recorded occurrence-time column; absence of a recorded value does not imply nonoccurrence.",
                from_node_types=["event"], to_node_types=["column"],
                direction_semantics="Occurrence toward its recorded time input.",
                evidence_requirement="The column describes the time of the same occurrence."),
        ))
    assert result.status=='applied',result.errors
    assert {e.edge_type for e in graph.get_edges('event.opening')}=={'opens','recorded_at'}


def test_delete_authored_edge_does_not_restore_it(graph):
    result=apply(graph,node('concept.school','concept',{'targets':[['column.schools.id']]}),edge('school-id','concept.school','column.schools.id'))
    assert result.status=='applied',result.errors
    result=apply(graph,{'operation':'delete_edge','edge_id':'school-id'})
    assert result.status=='applied',result.errors
    assert not graph.get_edges('concept.school')
