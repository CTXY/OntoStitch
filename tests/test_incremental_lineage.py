"""Sequential lineage preserves source dispositions without manufacturing support."""

import json
from types import SimpleNamespace

import pytest

from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.graph.edit_contract import Change, Element
from cortex.schema import (
    ConflictAlternative,
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    SourceReference,
)


def node(key, *, source=None, **updates):
    return GraphNode(
        id=key,
        node_type="concept",
        name=key,
        sources=(SourceReference(source_id=source),) if source else (),
        **updates,
    )


def edge(key, start, end, *, source=None):
    return GraphEdge(
        id=key,
        edge_type="related_to",
        from_node_id=start,
        to_node_id=end,
        properties={"description": "Recorded relation."},
        sources=(SourceReference(source_id=source),) if source else (),
    )


def graph(nodes=(), edges=()):
    return GraphState(graph=GraphSpace(nodes=tuple(nodes), edges=tuple(edges)))


def context(state, physical_map=None):
    return SimpleNamespace(
        raw_nodes={n.id: n for n in state.graph.nodes},
        raw_edges={e.id: e for e in state.graph.edges},
        physical_map=physical_map or {},
    )


def ref(key, kind="node", alternative_id=None):
    return Element(kind=kind, id=key, alternative_id=alternative_id)


def change(before, after, reason="Explicit knowledge reorganization."):
    return Change(before=tuple(before), after=tuple(after), explanation=reason)


def exported(index, final):
    return {tuple(c.before): set(c.after) for c in index.source_changes(final)}


def test_create_then_update_and_explicit_delete_never_become_original_handles():
    initial = graph([node("a")])
    index = LineageIndex(context(initial))
    created = graph([*initial.graph.nodes, node("new", source="evidence")])
    index.observe(initial, created)
    updated = graph(
        [node("a"), node("new", source="evidence", description="Supported refinement.")]
    )
    index.observe(created, updated)
    assert index.source_changes(updated) == ()
    with pytest.raises(ValueError, match="no explicit disposition"):
        index.observe(updated, initial)
    index.observe(
        updated,
        initial,
        [change([ref("new")], [], "Discard unsupported derived object.")],
    )
    assert index.source_changes(initial) == ()
    with pytest.raises(ValueError, match="cannot be reused"):
        index.observe(initial, created)


def test_same_id_updates_track_current_content_and_keep_original_refs():
    initial = graph([node("a", source="one")])
    index = LineageIndex(context(initial))
    first = graph([node("a", source="one", description="First accepted change.")])
    index.observe(initial, first)
    second = graph([node("a", source="one", description="Second accepted change.")])
    index.observe(first, second)
    assert exported(index, second) == {(ref("a"),): {ref("a")}}
    assert index.origin_refs("node", "a") == (ref("a"),)


def test_true_aliases_compose_separately_from_structural_rewrites():
    initial = graph([node("a"), node("b")])
    index = LineageIndex(context(initial))
    first = graph([node("a")])
    index.observe(initial, first, node_aliases={"b": "a"})
    assert index.node_map["b"] == "a"
    second = graph([node("new")])
    index.observe(first, second, [change([ref("a")], [ref("new")])])
    assert index.node_map["b"] == "a"  # A rewrite is not a new identity assertion.
    assert index.origin_refs("node", "new") == (ref("a"), ref("b"))
    assert all(ref("new") in c.after for c in index.source_changes(second))


def test_physical_aliases_can_be_present_before_first_observation():
    original = graph([node("p"), node("duplicate")])
    ctx = context(original, {"duplicate": "p"})
    current = graph([node("p")])
    index = LineageIndex(ctx)
    index.observe(current, current)
    assert index.node_map["duplicate"] == "p"
    assert index.source_changes(current) == ()


def test_split_then_merge_keeps_layout_sources_and_final_refs_only():
    initial = graph(
        [node("compound", source="source_a"), node("other", source="source_b")]
    )
    index = LineageIndex(context(initial))
    first = graph(
        [
            node("selection", source="source_a"),
            node("layout", source="source_a"),
            node("other", source="source_b"),
        ]
    )
    index.observe(
        initial, first, [change([ref("compound")], [ref("selection"), ref("layout")])]
    )
    policy = node("policy", source="source_b")
    final = graph([policy, node("layout", source="source_a")])
    index.observe(
        first, final, [change([ref("selection"), ref("other")], [ref("policy")])]
    )
    out = index.source_changes(final)
    assert all(
        e.id not in {"compound", "selection", "other"} for c in out for e in c.after
    )
    assert exported(index, final) == {
        (ref("compound"),): {ref("policy"), ref("layout")},
        (ref("other"),): {ref("policy")},
    }
    assert final.graph.nodes[1].sources == first.graph.nodes[1].sources
    assert final.graph.nodes[0].sources == (SourceReference(source_id="source_b"),)


def test_many_to_many_stays_collective_and_never_adds_result_sources():
    initial = graph([node("a", source="left"), node("b", source="right")])
    index = LineageIndex(context(initial))
    final = graph([node("x", source="left"), node("y", source="right")])
    index.observe(initial, final, [change([ref("a"), ref("b")], [ref("x"), ref("y")])])
    out = index.source_changes(final)
    assert len(out) == 1 and set(out[0].before) == {ref("a"), ref("b")}
    assert set(out[0].after) == {ref("x"), ref("y")}
    assert index.node_map == {"a": "a", "b": "b"}
    assert index.origin_refs("node", "x") == (ref("a"), ref("b"))
    assert final.graph.nodes[0].sources == (SourceReference(source_id="left"),)


def test_deleted_original_edge_disposition_can_be_corrected_without_touching_root():
    initial = graph(
        [node("a"), node("b")], [edge("old", "a", "b"), edge("survivor", "a", "b")]
    )
    index = LineageIndex(context(initial))
    first = graph(initial.graph.nodes, [initial.graph.edges[1]])
    index.observe(initial, first, [change([ref("old", "edge")], [ref("a")])])
    assert ref("old", "edge") in index.origin_refs("node", "a")
    index.observe(
        first,
        first,
        [
            change(
                [ref("old", "edge")],
                [ref("survivor", "edge")],
                "Correct the surviving assertion reference.",
            )
        ],
    )
    assert exported(index, first)[(ref("old", "edge"),)] == {ref("survivor", "edge")}
    assert ref("old", "edge") not in index.origin_refs("node", "a")


def test_discard_is_explicit_and_may_be_corrected_later():
    initial = graph([node("a"), node("b")])
    index = LineageIndex(context(initial))
    final = graph([node("b")])
    index.observe(
        initial, final, [change([ref("a")], [], "Reject unsupported input assertion.")]
    )
    assert exported(index, final)[(ref("a"),)] == set()
    index.observe(
        final,
        final,
        [change([ref("a")], [ref("b")], "Correct the earlier disposition.")],
    )
    assert exported(index, final)[(ref("a"),)] == {ref("b")}


def conflicted(key, keys=("one", "two")):
    return node(
        key,
        merge_status="conflict",
        consolidation_status="semantic_conflict",
        alternatives=tuple(
            ConflictAlternative(alternative_id=k, overrides={"description": k})
            for k in keys
        ),
    )


def test_branch_rename_needs_explicit_disposition_and_survives_snapshot():
    initial = graph([conflicted("a")])
    ctx = context(initial)
    index = LineageIndex(ctx)
    final = graph([conflicted("a", ("one", "replacement"))])
    snapshot = index.snapshot()
    with pytest.raises(ValueError, match="no explicit disposition"):
        index.observe(initial, final)
    assert index.snapshot() == snapshot
    index.observe(
        initial,
        final,
        [
            change(
                [ref("a", alternative_id="two")],
                [ref("a", alternative_id="replacement")],
            )
        ],
    )
    assert exported(index, final)[(ref("a", alternative_id="two"),)] == {
        ref("a", alternative_id="replacement")
    }
    restored = LineageIndex.restore(ctx, json.loads(json.dumps(index.snapshot())))
    assert restored.source_changes(final) == index.source_changes(final)


def test_whole_conflict_discard_records_its_original_branches():
    initial = graph([conflicted("a")])
    index = LineageIndex(context(initial))
    final = graph()
    index.observe(
        initial, final, [change([ref("a")], [], "Discard erroneous complete conflict.")]
    )
    assert {c.before[0] for c in index.source_changes(final)} == {
        ref("a"),
        ref("a", alternative_id="one"),
        ref("a", alternative_id="two"),
    }
    assert all(not c.after for c in index.source_changes(final))


def test_alias_remaps_existing_branch_ids_and_rejects_cycles_atomically():
    initial = graph([conflicted("a"), conflicted("b")])
    index = LineageIndex(context(initial))
    final = graph([conflicted("a")])
    index.observe(initial, final, node_aliases={"b": "a"})
    assert ref("b", alternative_id="one") in index.origin_refs("node", "a")
    snapshot = index.snapshot()
    with pytest.raises(ValueError, match="cycle"):
        index.observe(final, final, node_aliases={"a": "b", "b": "a"})
    assert index.snapshot() == snapshot


def test_final_export_rejects_unobserved_deletion():
    initial = graph([node("a")])
    index = LineageIndex(context(initial))
    with pytest.raises(ValueError, match="surviving reference missing"):
        index.source_changes(graph())


def test_branch_collision_during_owner_alias_keeps_each_original_variant():
    a = conflicted("a")
    b = conflicted("b").model_copy(
        update={
            "alternatives": (
                ConflictAlternative(
                    alternative_id="one", overrides={"description": "different"}
                ),
                ConflictAlternative(
                    alternative_id="two", overrides={"description": "two"}
                ),
            )
        }
    )
    initial = graph([a, b])
    index = LineageIndex(context(initial))
    combined = a.model_copy(
        update={
            "alternatives": (
                *a.alternatives,
                ConflictAlternative(
                    alternative_id="other_one", overrides={"description": "different"}
                ),
            )
        }
    )
    final = graph([combined])
    snap = index.snapshot()
    with pytest.raises(ValueError, match="Aliased branch content changed"):
        index.observe(initial, final, node_aliases={"b": "a"})
    assert index.snapshot() == snap
    index.observe(
        initial,
        final,
        [
            change(
                [ref("b", alternative_id="one")], [ref("a", alternative_id="other_one")]
            )
        ],
        node_aliases={"b": "a"},
    )
    out = exported(index, final)
    assert out[(ref("b", alternative_id="one"),)] == {
        ref("a", alternative_id="other_one")
    }
    assert ref("a", alternative_id="one") not in out[(ref("b", alternative_id="one"),)]


def test_source_groups_advance_together_for_two_independent_split_children():
    initial = graph([node("a")])
    index = LineageIndex(context(initial))
    middle = graph([node("x"), node("y")])
    index.observe(initial, middle, [change([ref("a")], [ref("x"), ref("y")])])
    final = graph([node("left"), node("right")])
    index.observe(
        middle,
        final,
        [change([ref("x")], [ref("left")]), change([ref("y")], [ref("right")])],
    )
    assert exported(index, final) == {(ref("a"),): {ref("left"), ref("right")}}


def test_chained_aliases_only_require_final_survivor():
    initial = graph([node("a"), node("b"), node("c")])
    index = LineageIndex(context(initial))
    final = graph([node("c")])
    index.observe(initial, final, node_aliases={"a": "b", "b": "c"})
    assert index.node_map == {"a": "c", "b": "c", "c": "c"}


def test_many_to_many_origin_correction_requires_whole_group():
    initial = graph([node("a"), node("b")])
    index = LineageIndex(context(initial))
    current = graph([node("x"), node("y")])
    index.observe(
        initial, current, [change([ref("a"), ref("b")], [ref("x"), ref("y")])]
    )
    with pytest.raises(ValueError, match="all original references"):
        index.observe(current, current, [change([ref("a")], [ref("x")])])
    index.observe(current, current, [change([ref("a"), ref("b")], [ref("x")])])
    assert set(index.source_changes(current)[0].after) == {ref("x")}
