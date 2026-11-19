"""Review regressions for the hidden scope assumptions behind thrombosis failures."""
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.source_units import source_units
from cortex.schema import GraphNode, GraphSpace, GraphState, NodeContent, SourceReference


def conflicted_owner(common_sources=()):
    owner = GraphNode(id="degree", node_type="concept", name="degree")
    def content(definition):
        return NodeContent(name="degree", description=definition, aliases=(), tags=(),
                           properties={"definition": definition, "targets": [["column"]]})
    owner, _ = GraphManager.compact_conflict(owner, [
        ("specific", content("1 is the most severe code"), ()),
        ("ordinal", content("larger codes have greater severity"), ()),
    ], common_sources=common_sources)
    return owner


def test_shared_storage_fields_do_not_invent_common_source_unit():
    owner = conflicted_owner()
    assert owner.properties["targets"] == [["column"]]  # Common semantic storage.
    assert not owner.common_sources
    ordinary = GraphNode(id="negative", node_type="concept", name="zero means negative")
    base = GraphState(graph=GraphSpace(nodes=(owner, ordinary)))
    units = source_units(base, owner.id, [owner.id, ordinary.id])
    assert {(u["node_id"], u["kind"], u.get("alternative_id")) for u in units} == {
        ("degree", "alternative", "specific"),
        ("degree", "alternative", "ordinal"),
        ("negative", "normal_content", None),
    }
    # Source-less accepted branches still carry semantic knowledge and need
    # dispositions; provenance count must not make them disappear.
    assert len(units) == 3


def test_unit_handles_are_stable_across_member_order_and_serialized_restart():
    owner = conflicted_owner()
    ordinary = GraphNode(id="negative", node_type="concept", name="zero means negative")
    base = GraphState(graph=GraphSpace(nodes=(owner, ordinary)))
    before = source_units(base, owner.id, [owner.id, ordinary.id])
    restored = GraphState.model_validate_json(base.model_dump_json())
    assert source_units(restored, owner.id, [ordinary.id, owner.id]) == before
    # A valid unit from another operation owner cannot be smuggled into this
    # group's route list merely because the source branch label is the same.
    assert {u["unit_id"] for u in source_units(base, ordinary.id, [owner.id, ordinary.id])}.isdisjoint(
        {u["unit_id"] for u in before})


def test_real_common_support_has_one_distinct_scope_in_addition_to_branches():
    common = SourceReference(source_id="accepted-common")
    owner = conflicted_owner((common,))
    base = GraphState(graph=GraphSpace(nodes=(owner,)))
    units = source_units(base, owner.id, [owner.id])
    shared = [u for u in units if u["kind"] == "common"]
    assert len(shared) == 1 and shared[0]["node_id"] == owner.id
    assert "alternative_id" not in shared[0]
    assert len([u for u in units if u["kind"] == "alternative"]) == 2
