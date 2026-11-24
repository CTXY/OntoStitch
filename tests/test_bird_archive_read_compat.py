"""Old immutable relation groups need an explicit schema, not a weaker validator."""
from copy import deepcopy

import pytest

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.schema import GraphState, load_root_schema
from scripts.bird_ablation_compat import archived_graph_read_schema


def legacy_state():
    nodes = [
        {"id": "table.t", "node_type": "table", "name": "t", "properties": {"table_name": "t"}},
        {"id": "column.t.x", "node_type": "column", "name": "x",
         "properties": {"table_ref": "table.t", "column_name": "x", "data_type": "INTEGER"}},
        *[{"id": f"concept.{name}", "node_type": "concept", "name": name,
           "properties": {"definition": name, "targets": [["column.t.x"]]}}
          for name in ("a", "b", "c")],
    ]
    nodes[2]["properties"]["relation_metadata"] = [
        {"id": "group", "edges": ["e1", "e2"], "description": "These two categories may overlap."}
    ]
    edges = [{"id": f"e{i}", "edge_type": "is_a", "from_node_id": "concept.a",
              "to_node_id": f"concept.{name}", "properties": {"description": "Recorded category."}}
             for i, name in [(1, "b"), (2, "c")]]
    return {"graph": {"nodes": nodes, "edges": edges}}


def test_explicit_archive_schema_preserves_source_and_default_schema_stays_strict():
    raw = legacy_state()
    before = deepcopy(raw)
    state = GraphState.model_validate(raw)
    schema = load_root_schema()
    with pytest.raises(GraphRejected):
        GraphManager(schema, state)
    compatible = archived_graph_read_schema(schema)
    manager = GraphManager(compatible, state)
    assert manager.get_snapshot() == state
    assert raw == before
    assert "relation_metadata" not in schema.effective_node_fields("concept")


@pytest.mark.parametrize("members", [["e1", "missing"], ["e1", "e1"], ["e1"]])
def test_legacy_group_edge_invariants_remain_strict(members):
    raw = legacy_state()
    raw["graph"]["nodes"][2]["properties"]["relation_metadata"][0]["edges"] = members
    with pytest.raises(GraphRejected):
        GraphManager(archived_graph_read_schema(load_root_schema()), GraphState.model_validate(raw))


def test_legacy_group_does_not_allow_mixed_relation_direction():
    raw = legacy_state()
    raw["graph"]["edges"][1].update(from_node_id="concept.c", to_node_id="concept.a")
    with pytest.raises(GraphRejected):
        GraphManager(archived_graph_read_schema(load_root_schema()), GraphState.model_validate(raw))
