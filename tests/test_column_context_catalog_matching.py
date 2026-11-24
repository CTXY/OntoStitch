"""External CSV identifiers attach only to unambiguous catalog columns."""

from cortex.application.retrieval import ColumnSemanticContext
from cortex.capabilities.retrieve_db.retrieval.index import RepresentationIndex
from cortex.schema import GraphNode, GraphSpace, GraphState


def index(columns, descriptions):
    table = GraphNode(
        id="opaque-table",
        node_type="table",
        name="drivers",
        properties={"table_name": "drivers"},
    )
    graph = GraphState(
        graph=GraphSpace(
            nodes=(
                table,
                *(
                    GraphNode(
                        id=f"col-{i}",
                        node_type="column",
                        name=name,
                        properties={"table_ref": table.id, "column_name": name},
                    )
                    for i, name in enumerate(columns)
                ),
            )
        )
    )
    contexts = tuple(
        ColumnSemanticContext(
            column_id=f"column.drivers.{name}",
            table_name="drivers",
            column_name=name,
            description=description,
        )
        for name, description in descriptions
    )
    result = RepresentationIndex.build(graph, column_context=contexts)
    return {
        d.canonical_node_id: d.structured_signals["column_contexts"]
        for d in result.documents
        if d.node_type == "column"
    }, contexts


def test_csv_padding_resolves_to_actual_column_without_mutating_input():
    documents, original = index(["driverId"], [("driverId ", "Driver identity")])
    context = documents["col-0"][0]
    assert context["column_name"] == "driverId"
    assert context["column_id"] == "col-0"
    assert context["description"] == "Driver identity"
    assert original[0].column_name == "driverId "


def test_exact_whitespace_identifier_keeps_its_own_meaning():
    documents, _ = index(["name", "name "], [("name ", "Literal space")])
    assert not documents["col-0"]
    assert documents["col-1"][0]["description"] == "Literal space"


def test_ambiguous_physical_identifiers_do_not_receive_padded_context():
    documents, _ = index(["name", "name "], [("name  ", "Ambiguous")])
    assert not documents["col-0"]
    assert not documents["col-1"]


def test_ambiguous_descriptions_are_not_arbitrarily_selected():
    documents, _ = index(["name"], [("name ", "First"), ("name  ", "Second")])
    assert not documents["col-0"]


def test_exact_context_takes_precedence_over_padded_context():
    documents, _ = index(["name"], [("name", "Exact"), ("name ", "Padded")])
    assert [c["description"] for c in documents["col-0"]] == ["Exact"]
