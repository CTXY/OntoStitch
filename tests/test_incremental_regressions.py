"""Local repair composition keeps current handles and source ownership separate."""


import pytest
from test_graph_integration import catalog, concept, edge, state

from cortex.capabilities.graph_merge.integration.batch import ActiveBatch, BatchResult
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.models import Change, Element
from cortex.schema import SourceReference, load_root_schema


def context(edges):
    return IntegrationContext(
        state(),
        state([*catalog(), concept("A", "a"), concept("B", "b")], edges),
        load_root_schema(),
    )


def disposition(before, after):
    return Change(
        before=tuple(Element(kind="edge", id=k) for k in before),
        after=tuple(Element(kind="edge", id=k) for k in after),
        explanation="The accepted current assertion replaces these representations.",
    )


def test_mechanical_alias_then_semantic_rewrite_are_two_lineage_steps():
    ctx = context([edge("Y", "A", "B", "has_feature")])
    before = ctx.union_state()
    a = ctx.input_node_map["A"]
    y, x = ctx.input_edge_map["Y"], "duplicate_source"
    # Such duplicates can appear after a preceding accepted identity alignment.
    duplicate = ctx.raw_edges[y].model_copy(update={"id": x})
    ctx.raw_edges[x] = duplicate
    before = before.model_copy(
        update={
            "graph": before.graph.model_copy(
                update={
                    "edges": (*before.graph.edges, duplicate),
                }
            )
        }
    )
    batch = ActiveBatch(ctx, before, targets={a})
    assert batch.edge_aliases[x] == y
    replacement = next(e for e in batch.seed.graph.edges if e.id == y).model_dump(
        mode="json"
    )
    replacement["id"] = "replacement"
    batch.submit(
        BatchResult.model_validate(
            {
                "edits": [
                    {"operation": "delete_edge", "edge_id": y},
                    {"operation": "create_edge", "edge": replacement},
                ],
                "changes": [disposition([y], ["replacement"]).model_dump(mode="json")],
            }
        )
    )
    # The alias destination exists in the seed, not in the final local repair.
    wrong = LineageIndex(ctx, initial_node_map=ctx.physical_map)
    with pytest.raises(ValueError, match="destination|target"):
        wrong.observe(
            before, batch.preview, batch.result.changes, edge_aliases=batch.edge_aliases
        )
    staged = LineageIndex(ctx, initial_node_map=ctx.physical_map)
    staged.observe(before, batch.seed, edge_aliases=batch.edge_aliases)
    staged.observe(batch.seed, batch.preview, batch.result.changes)
    exported = staged.source_changes(batch.preview)
    for original in (x, y):
        assert any(
            Element(kind="edge", id=original) in c.before
            and c.after == (Element(kind="edge", id="replacement"),)
            for c in exported
        )


def test_archived_edge_support_can_be_restored_without_mixing_before_domains():
    first = edge("old", "A", "B", "has_feature").model_copy(
        update={
            "sources": (SourceReference(source_id="first"),),
        }
    )
    second = edge("live", "A", "B", "has_feature").model_copy(
        update={
            "properties": {
                "description": "A complementary wording of the same feature."
            },
            "sources": (SourceReference(source_id="second"),),
        }
    )
    ctx = context([first, second])
    initial = ctx.union_state()
    old, live = (ctx.input_edge_map[k] for k in ("old", "live"))
    current = initial.model_copy(
        update={
            "graph": initial.graph.model_copy(
                update={
                    "edges": tuple(e for e in initial.graph.edges if e.id != old),
                }
            )
        }
    )
    lineage = LineageIndex(ctx, initial_node_map=ctx.physical_map)
    lineage.observe(initial, current, [disposition([old], [])])
    batch = ActiveBatch(ctx, current, targets={ctx.input_node_map["A"]})
    batch.submit(
        BatchResult.model_validate(
            {
                "edge_merges": [
                    {
                        "source_ids": [old],
                        "survivor_id": live,
                        "explanation": "Both source assertions support this same business relation.",
                    }
                ]
            }
        )
    )
    assert {tuple(e.id for e in c.before) for c in batch.result.changes} == {
        (old,),
        (live,),
    }
    lineage.observe(current, batch.preview, batch.result.changes)
    retained = next(e for e in batch.preview.graph.edges if e.id == live)
    assert set(retained.sources) == set(first.sources + second.sources)
    assert any(
        Element(kind="edge", id=old) in c.before
        and c.after == (Element(kind="edge", id=live),)
        for c in lineage.source_changes(batch.preview)
    )


def test_invalid_pending_writes_and_edge_merges_can_be_withdrawn():
    ctx = context([edge("relation", "A", "B", "has_feature")])
    a = ctx.input_node_map["A"]
    batch = ActiveBatch(ctx, ctx.union_state(), targets={a})
    batch.submit(BatchResult())
    valid = batch.preview
    malformed = ctx.nodes[a].model_dump(mode="json")
    malformed["properties"]["targets"] = [[]]
    with pytest.raises(ValueError):
        batch.submit(
            BatchResult.model_validate(
                {"edits": [{"operation": "update_node", "node": malformed}]}
            )
        )
    assert batch.preview == valid
    batch.submit(BatchResult(remove_edits=(Element(kind="node", id=a),)))
    assert not batch.pending and batch.preview == valid
    with pytest.raises(ValueError, match="survivor"):
        batch.submit(
            BatchResult.model_validate(
                {
                    "edge_merges": [
                        {
                            "source_ids": [ctx.input_edge_map["relation"]],
                            "survivor_id": "missing",
                            "explanation": "An invalid pending merge.",
                        }
                    ]
                }
            )
        )
    assert batch.preview == valid
    batch.submit(BatchResult(remove_edge_merges=("missing",)))
    assert not batch.edge_merges and batch.preview == valid
