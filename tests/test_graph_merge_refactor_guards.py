"""Current sharing and final publication guards survive legacy-engine removal."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from graph_merge_fixtures import catalog, concept, edge, state
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.llm.packing import expand_payload, pack_payload
from cortex.schema import load_root_schema


def test_shared_payload_roundtrip_preserves_changed_whole_fields_and_deleted_objects():
    a = concept('A', 'a').model_copy(update={
        'description': 'Original evidenced definition. ' * 50,
        'properties': {'targets': [['a']], 'definition': 'The original bounded category.'},
    })
    b, c = concept('B', 'b'), concept('C', 'c')
    changed = a.model_copy(update={
        'description': 'Revised evidenced definition.',
        'properties': {'targets': [['a'], ['b']]},
    })
    relation = edge('retained', 'A', 'C', 'is_a')
    revised_relation = relation.model_copy(update={
        'properties': {'description': 'The category has this narrower evidenced role.'},
    })
    removed_relation = edge('removed', 'B', 'C', 'is_a')
    payload = {
        'nodes': [node.model_dump(mode='json') for node in (a, b, c)],
        'candidate_nodes': [node.model_dump(mode='json') for node in (changed, c)],
        'deleted_nodes': [b.model_dump(mode='json')],
        'relations': [row.model_dump(mode='json') for row in (relation, removed_relation)],
        'candidate_relations': [revised_relation.model_dump(mode='json')],
        'deleted_relations': [removed_relation.model_dump(mode='json')],
    }
    original = deepcopy(payload)
    packed = pack_payload(payload)
    assert packed['candidate_nodes'][0] == {
        'input_ref': 'A', 'overrides': {
            'description': changed.description, 'properties': changed.properties,
        },
    }
    assert packed['candidate_nodes'][1] == {'input_ref': 'C'}
    assert packed['deleted_nodes'] == [{'input_ref': 'B'}]
    assert packed['candidate_relations'] == [{
        'input_ref': 'retained', 'overrides': {'properties': revised_relation.properties},
    }]
    assert packed['deleted_relations'] == [{'input_ref': 'removed'}]
    expanded = expand_payload(packed)
    assert expanded == original and payload == original
    assert 'definition' not in expanded['candidate_nodes'][0]['properties']
    assert expanded['deleted_nodes'][0]['sources'] == original['nodes'][1]['sources']


def test_final_publication_rejects_readable_abstract_predicates_without_mutating_graph():
    predicate = 'has_attribute'
    schema = load_root_schema()
    current = state([*catalog(), concept('A', 'a'), concept('B', 'b')],
                    [edge('historical-role', 'A', 'B', predicate)])
    # Historical abstract assertions are readable. Final publication has an
    # additional concrete-role guard beyond ordinary graph/schema validity.
    GraphManager(schema).validate_state(current)
    engine = IncrementalGraphIntegrationEngine(None)
    engine.ctx = SimpleNamespace(schema=schema)
    engine.current = current
    before = current.model_dump(mode='json')
    with pytest.raises(ValueError, match=rf'{predicate} is an abstract relation base'):
        engine._publish_checks()
    assert engine.current.model_dump(mode='json') == before
    assert engine.history == [] and engine.last_session is None
