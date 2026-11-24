"""A dependency repair is assessed against its actual retained operands."""
from graph_merge_fixtures import catalog, concept, edge, state

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.reconciliation.context import relation_context
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.schema import GraphNode, GraphSpace, GraphState, load_root_schema


def test_removed_physical_binding_keeps_existing_semantic_dependency_visible():
    base = state([*catalog(), concept('A', 'a'), concept('B', 'b')],
                 [edge('canonical-a', 'A', 'a'), edge('canonical-b', 'B', 'b')])
    metrics = [GraphNode(id=k, node_type='metric', name=k, properties={
        'pattern': 'The measured value.', 'expression': {'expr': '${value}', 'refs': {'value': operand}}})
        for k, operand in [('M', 'A'), ('unrelated', 'B')]]
    direct = edge('retained-direct-input', 'M', 'A', 'depends_on')
    irrelevant = edge('unrelated-input', 'unrelated', 'B', 'depends_on')
    wrong = edge('incorrect-physical-input', 'M', 'a', 'grounded_on')
    before = base.model_copy(update={'graph': base.graph.model_copy(update={
        'nodes': base.graph.nodes + tuple(metrics), 'edges': base.graph.edges + (direct, irrelevant, wrong)})})
    current = before.model_copy(update={'graph': before.graph.model_copy(update={
        'edges': tuple(e for e in before.graph.edges if e.id != wrong.id)})})
    ctx = IntegrationContext(before, GraphState(graph=GraphSpace()), load_root_schema())
    snapshot = current.model_dump_json()
    packet = relation_context(ctx, current, LocalTask('review', 'graph_assess', {'M'}), before=before, assessment=True)
    visible = {e['id']: e for e in packet['edges']}
    assert direct.id in visible and visible[direct.id]['to_node_id'] == 'A'
    assert 'canonical-a' in visible
    assert irrelevant.id not in visible and wrong.id not in visible
    assert packet['actual_changes']['edges']['deleted'] == [wrong.id]
    assert not packet['actual_changes']['edges']['created']
    assert not packet['actual_changes']['nodes']['updated']
    assert current.model_dump_json() == snapshot
