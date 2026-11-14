"""Current-relative edits and pending-object repair have different lifecycles."""

import pytest
from test_graph_integration import catalog, concept, state

from cortex.capabilities.graph_merge.integration.batch import ActiveBatch, BatchResult
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.schema import load_root_schema


def setup_batch():
    ctx = IntegrationContext(
        state(),
        state([*catalog(), concept("A", "a"), concept("B", "b")]),
        load_root_schema(),
    )
    a, b = (ctx.input_node_map[k] for k in ("A", "B"))
    return ctx, ActiveBatch(ctx, ctx.union_state(), targets={a, b}), a, b


def write(node, operation="update_node"):
    return {"operation": operation, "node": node}


@pytest.mark.parametrize("scope", [["foreign-branch"], [], "foreign-branch", [None]])
def test_batch_rejects_branch_scope_on_a_normal_source_before_acceptance(scope):
    _ctx, batch, a, b = setup_batch()
    raw = {
        "id": "bad_scope",
        "edge_type": "has_feature",
        "from_node_id": a,
        "to_node_id": b,
        "properties": {"description": "An explicit feature relationship.", "alternative_ids": scope},
    }
    with pytest.raises(ValueError, match="alternative_ids"):
        batch.submit(BatchResult.model_validate({"edits": [{"operation": "create_edge", "edge": raw}]}))
    assert batch.preview is None


def test_invalid_pending_repair_does_not_apply_partial_objects():
    ctx, batch, a, b = setup_batch()
    good = ctx.nodes[a].model_dump(mode="json")
    good["description"] = "Clarified supported meaning."
    bad = ctx.nodes[b].model_dump(mode="json")
    bad["properties"]["targets"] = [[]]
    before = batch.base
    with pytest.raises(ValueError):
        batch.submit(BatchResult.model_validate({"edits": [write(good), write(bad)]}))
    assert batch.preview is None and batch.base == before
    repaired = ctx.nodes[b].model_dump(mode="json")
    batch.submit(BatchResult.model_validate({"edits": [write(repaired)]}))
    assert (
        next(n for n in batch.preview.graph.nodes if n.id == a).description
        == good["description"]
    )
    assert ctx.candidate == batch.preview


def test_next_batch_updates_and_deletes_previously_created_object():
    ctx, batch, a, _b = setup_batch()
    node = ctx.nodes[a].model_dump(mode="json")
    node["id"] = "new_concept"
    batch.submit(BatchResult.model_validate({"edits": [write(node, "create_node")]}))
    next_batch = ActiveBatch(ctx, batch.preview, targets={"new_concept"})
    node["description"] = "Current-relative second accepted version."
    next_batch.submit(BatchResult.model_validate({"edits": [write(node)]}))
    assert (
        next(
            n for n in next_batch.preview.graph.nodes if n.id == "new_concept"
        ).description
        == node["description"]
    )
    last = ActiveBatch(ctx, next_batch.preview, targets={"new_concept"})
    with pytest.raises(ValueError, match="disposition"):
        last.submit(
            BatchResult.model_validate(
                {"edits": [{"operation": "delete_node", "node_id": "new_concept"}]}
            )
        )
    last.submit(
        BatchResult.model_validate(
            {
                "changes": [
                    {
                        "before": [{"kind": "node", "id": "new_concept"}],
                        "after": [],
                        "explanation": "Explicit redundant representation discard.",
                    }
                ]
            }
        )
    )
    assert "new_concept" not in {n.id for n in last.preview.graph.nodes}


def test_scope_rejects_read_only_neighbour_node_and_keeps_last_valid_draft():
    ctx, batch, _a, b = setup_batch()
    batch.targets.remove(b)
    batch.allowed.remove(b)
    batch.submit(BatchResult())
    accepted = batch.preview
    raw = ctx.nodes[b].model_dump(mode="json")
    raw["description"] = "Not authorized by this task."
    with pytest.raises(ValueError, match="Outside task"):
        batch.submit(BatchResult.model_validate({"edits": [write(raw)]}))
    assert batch.preview == accepted


def test_rejected_installed_contract_is_not_a_poisoned_pending_operation():
    from test_graph_integration import edge

    schema = load_root_schema()
    incoming = state(
        [*catalog(), concept("A", "a"), concept("B", "b")],
        [edge("feature", "A", "B", "has_feature")],
    )
    ctx = IntegrationContext(state(), incoming, schema)
    batch = ActiveBatch(ctx, ctx.union_state(), targets={ctx.input_node_map["A"]})
    original = batch.seed.graph.relation_contracts[0]
    changed = original.model_copy(
        update={"description": "A restyled definition is not a new predicate."}
    )
    with pytest.raises(ValueError, match="NOT saved"):
        batch.submit(BatchResult(relation_contracts=(changed,)))
    batch.submit(BatchResult())
    assert batch.preview.graph.relation_contracts == batch.seed.graph.relation_contracts


def test_deleting_an_owner_before_retargeting_its_edge_is_atomic():
    from test_graph_integration import edge

    incoming = state(
        [*catalog(), concept("A", "a"), concept("B", "b"), concept("C", "c")],
        [edge("feature", "A", "B", "has_feature")],
    )
    ctx = IntegrationContext(state(), incoming, load_root_schema())
    a, b, c = (ctx.input_node_map[k] for k in ["A", "B", "C"])
    eid = ctx.input_edge_map["feature"]
    changed = (
        ctx.edges[eid].model_copy(update={"from_node_id": c}).model_dump(mode="json")
    )
    edits = [
        {"operation": "delete_node", "node_id": a},
        {"operation": "update_edge", "edge": changed},
    ]
    outputs = []
    for ordering in (edits, list(reversed(edits))):
        batch = ActiveBatch(ctx, ctx.union_state(), targets={a, b, c})
        batch.submit(
            BatchResult.model_validate(
                {
                    "edits": ordering,
                    "changes": [
                        {
                            "before": [{"kind": "node", "id": a}],
                            "after": [],
                            "explanation": "Discard a mistaken source object; retain the corrected feature relation.",
                        }
                    ],
                }
            )
        )
        outputs.append(batch.preview)
    assert outputs[0] == outputs[1]


def test_new_conflict_owner_survives_replaying_its_saved_patch():
    from cortex.schema import NodeContent

    ctx, batch, a, b = setup_batch()
    owner = ctx.nodes[a].model_dump(mode="json")
    owner["id"] = "combined"
    owner["sources"] = []
    alts = []
    for key, label in [(a, "first"), (b, "second")]:
        n = ctx.nodes[key]
        alts.append(
            {
                "alternative_id": label,
                "content": NodeContent(
                    name="Shared term",
                    description=None,
                    aliases=[],
                    tags=[],
                    properties=n.properties,
                ).model_dump(mode="json"),
                "sources": [s.model_dump(mode="json") for s in n.sources],
            }
        )
    result = BatchResult.model_validate(
        {
            "edits": [
                {"operation": "delete_node", "node_id": a},
                {"operation": "delete_node", "node_id": b},
                write(owner, "create_node"),
            ],
            "conflicts": [
                {
                    "node_id": "combined",
                    "alternatives": alts,
                    "conflict_points": [
                        {
                            "point_id": "mapping",
                            "aspect": "Representation choice",
                            "description": "Competing source implementations",
                            "choices": [
                                {
                                    "alternative_ids": ["first"],
                                    "statement": "Use the first representation.",
                                },
                                {
                                    "alternative_ids": ["second"],
                                    "statement": "Use the second representation.",
                                },
                            ],
                        }
                    ],
                }
            ],
            "changes": [
                {
                    "before": [{"kind": "node", "id": a}, {"kind": "node", "id": b}],
                    "after": [{"kind": "node", "id": "combined"}],
                    "explanation": "Preserve the supplied choices as complete sourced alternatives.",
                }
            ],
        }
    )
    batch.submit(result)
    assert (
        len(
            next(
                n for n in batch.preview.graph.nodes if n.id == "combined"
            ).alternatives
        )
        == 2
    )
    before = batch.preview
    batch.submit(BatchResult())
    assert batch.preview == before


def test_new_edge_survivor_copies_current_sources_without_inventing_before_ref():
    from test_graph_integration import edge

    from cortex.capabilities.graph_merge.integration.lineage import LineageIndex

    first = edge("e1", "A", "B", "has_feature").model_copy(
        update={"sources": concept("A", "a").sources}
    )
    second = edge("e2", "A", "B", "has_feature").model_copy(
        update={
            "properties": {"description": "The supported feature from another source."},
            "sources": concept("B", "b").sources,
        }
    )
    ctx = IntegrationContext(
        state(),
        state([*catalog(), concept("A", "a"), concept("B", "b")], [first, second]),
        load_root_schema(),
    )
    a, b = (ctx.input_node_map[k] for k in ["A", "B"])
    e1, e2 = (ctx.input_edge_map[k] for k in ["e1", "e2"])
    batch = ActiveBatch(ctx, ctx.union_state(), targets={a, b})
    new = (
        ctx.edges[e1]
        .model_copy(update={"id": "new_edge", "sources": ()})
        .model_dump(mode="json")
    )
    result = batch.submit(
        BatchResult.model_validate(
            {
                "edits": [{"operation": "create_edge", "edge": new}],
                "edge_merges": [
                    {
                        "source_ids": [e1, e2, "new_edge"],
                        "survivor_id": "new_edge",
                        "explanation": "These are the same compatible feature assertion.",
                    }
                ],
            }
        )
    )
    ledger = LineageIndex(ctx, initial_edge_map=batch.edge_aliases)
    ledger.observe(batch.seed, batch.preview, result.changes)
    assert all(r.id != "new_edge" for change in result.changes for r in change.before)
    final = next(e for e in batch.preview.graph.edges if e.id == "new_edge")
    assert set(final.sources) == set(first.sources) | set(second.sources)
    assert not {e1, e2} & {e.id for e in batch.preview.graph.edges}


def test_alias_projection_does_not_assign_new_evidence_to_an_unselected_conflict_branch():
    from cortex.application.ontology_graph import GraphManager
    from cortex.capabilities.graph_merge.integration.batch import alias_view
    from cortex.schema import (
        ConflictPoint,
        GraphSpace,
        GraphState,
        NodeContent,
        SourceReference,
    )

    ctx, _batch, a, b = setup_batch()
    variants = []
    for key, label in [(a, "first"), (b, "second")]:
        n = ctx.nodes[key]
        variants.append(
            (
                label,
                NodeContent(
                    name="Shared term",
                    description=None,
                    aliases=[label],
                    tags=[],
                    properties=n.properties,
                ),
                n.sources,
            )
        )
    point = ConflictPoint.model_validate(
        {
            "point_id": "choice",
            "aspect": "Representation",
            "description": "The supported implementations differ.",
            "choices": [
                {"alternative_ids": ["first"], "statement": "Use first."},
                {"alternative_ids": ["second"], "statement": "Use second."},
            ],
        }
    )
    owner, _ = GraphManager.compact_conflict(
        ctx.nodes[a],
        variants,
        consolidation_status="semantic_conflict",
        conflict_points=(point,),
    )
    incoming = ctx.nodes[a].model_copy(
        update={"id": "new_input", "sources": (SourceReference(source_id="third"),)}
    )
    base = GraphState(
        graph=GraphSpace(
            nodes=(
                *[n for n in ctx.nodes.values() if n.id not in {a, b}],
                owner,
                incoming,
            ),
            edges=tuple(ctx.edges.values()),
        )
    )
    preview, _ = alias_view(base, {"new_input": a})
    current = next(n for n in preview.graph.nodes if n.id == a)
    assert current.sources == owner.sources and current.aliases == []
    assert current.alternatives == owner.alternatives


def test_correspondence_is_not_a_graph_operation_and_archived_remaps_remain_valid():
    ctx, batch, a, b = setup_batch()
    mapping = {
        "before": [{"kind": "node", "id": a}],
        "after": [{"kind": "node", "id": b}],
        "explanation": "Consolidate the former representation into the survivor.",
    }
    with pytest.raises(ValueError, match="Correspondence-only replacement"):
        batch.submit(BatchResult.model_validate({"changes": [mapping]}))
    assert batch.preview is None and any(n.id == a for n in batch.base.graph.nodes)
    batch.submit(
        BatchResult.model_validate(
            {"edits": [{"operation": "delete_node", "node_id": a}]}
        )
    )
    assert a not in {n.id for n in batch.preview.graph.nodes}
    # A removed original can still have its historical source correspondence
    # corrected without pretending that a current graph object was deleted.
    next_batch = ActiveBatch(ctx, batch.preview, targets={b})
    next_batch.submit(BatchResult.model_validate({"changes": [mapping]}))
    assert next_batch.preview == batch.preview
