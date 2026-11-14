from cortex.capabilities.graph_merge.integration.compiler import deduplicate_edges
from cortex.schema import GraphEdge, SourceReference


def test_reversed_physical_join_deduplicates_and_maps_both_sources():
    def edge(ident, left, right, source):
        return GraphEdge(
            id=ident, edge_type="joins_with", from_node_id=left, to_node_id=right,
            properties={"column_pairs": [{"from_column_ref": left + ".id", "to_column_ref": right + ".id"}],
                        "cardinality": "1:1", "description": f"{left} joins {right}"},
            sources=(SourceReference(source_id=source),),
        )
    a, b = edge("a", "T1", "T2", "s1"), edge("b", "T2", "T1", "s2")
    edges, aliases = deduplicate_edges([a, b])
    assert aliases == {"a": "a", "b": "a"}
    assert len(edges) == 1
    assert {s.source_id for s in edges["a"].sources} == {"s1", "s2"}


def test_semantic_property_variants_are_not_silently_merged():
    a = GraphEdge(id="a", edge_type="related_to", from_node_id="A", to_node_id="B",
                  properties={"description": "Meaning one"})
    b = a.model_copy(update={"id": "b", "properties": {"description": "Meaning two"}})
    assert len(deduplicate_edges([a, b])[0]) == 2
