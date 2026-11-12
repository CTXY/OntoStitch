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
