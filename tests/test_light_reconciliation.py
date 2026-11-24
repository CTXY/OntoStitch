from graph_merge_runtime_fixtures import relation_compiler_context as setup
"""Behavioral contracts for the production lightweight relation path."""

import pytest
from graph_merge_fixtures import catalog, concept, edge, state

from cortex.capabilities.graph_merge.graph.batch import ActiveBatch
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.reconciliation.context import (
    relation_context,
)
from cortex.capabilities.graph_merge.graph.edits import (
    RelationPatch,
    submit_patch,
)
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.schema import load_root_schema


def test_invalid_partial_edit_cannot_poison_next_submission():
    ctx, base, a, _b, e = setup()
    with pytest.raises(ValueError):
        submit_patch(ctx, RelationPatch(
            node_updates=[{"id": a, "name": "Changed", "explanation": "Rendering only."}],
            edge_updates=[{"id": e, "to_node_id": "missing"}],
            explanation="An invalid multi-object proposal."))
    assert ctx.active_batch.preview is None and not ctx.active_batch.pending
    submit_patch(ctx, RelationPatch(explanation="No actual change."))
    assert ctx.active_batch.preview == ctx.active_batch.seed
    assert ctx.active_batch.preview.graph.nodes == base.graph.nodes


def test_retirement_couples_actual_deletion_and_source_disposition():
    ctx, _base, a, b, e = setup()
    submit_patch(ctx, RelationPatch(
        retirements=[{"before": [{"kind": "edge", "id": e}], "after": [],
                      "explanation": "A confirmed erroneous assertion, explicitly discarded."}],
        explanation="Remove only the specified invalid assertion."))
    assert e not in {x.id for x in ctx.active_batch.preview.graph.edges}
    assert any(c.before[0].id == e and not c.after for c in ctx.active_batch.result.changes)
    assert {a, b} <= {n.id for n in ctx.active_batch.preview.graph.nodes}


def test_one_edge_replacement_preserves_exact_refs_only_on_named_successor():
    from cortex.schema import SourceReference

    original = state([*catalog(), concept("A", "a"), concept("B", "b")], [
        edge("old", "A", "B", "has_feature").model_copy(update={"sources": (SourceReference(source_id="old-evidence"),)}),
        edge("keep", "A", "B", "has_feature").model_copy(update={"properties": {"description": "A retained formulation."}}),
    ])
    ctx = IntegrationContext(state(), original, load_root_schema())
    ctx.candidate = ctx.union_state()
    a, b = [ctx.input_node_map[k] for k in ["A", "B"]]
    old, keep = [ctx.input_edge_map[k] for k in ["old", "keep"]]
    ctx.active_batch = ActiveBatch(ctx, ctx.candidate, targets={a, b})
    submit_patch(ctx, RelationPatch(retirements=[{
        "before": [{"kind": "edge", "id": old}],
        "after": [{"kind": "edge", "id": keep}],
        "explanation": "Same assertion with a named retained successor.",
    }], explanation="Consolidate explicitly equivalent assertions."))
    remaining = {e.id: e for e in ctx.active_batch.preview.graph.edges}
    assert old not in remaining
    assert SourceReference(source_id="old-evidence") in remaining[keep].sources
    assert all(not e.sources for k, e in remaining.items() if k != keep)


def test_node_replacement_can_preserve_exact_support_without_broadcasting():
    from cortex.capabilities.graph_merge.evidence import EvidenceItem
    from cortex.schema import SourceReference

    original = state([*catalog(), concept('A', 'a'), concept('B', 'b'), concept('C', 'c')])
    refs = (SourceReference(source_id='B', locator='qa'), SourceReference(source_id='B', locator='QA'))
    incoming_b = next(n for n in original.graph.nodes if n.id == 'B')
    original = original.model_copy(update={'graph': original.graph.model_copy(update={
        'nodes': tuple(incoming_b.model_copy(update={'sources': refs}) if n.id == 'B' else n for n in original.graph.nodes)})})
    evidence = [EvidenceItem(id='B-evidence', source=s, case_id=None, content={'question': 'B'}) for s in refs]
    ctx = IntegrationContext(state(), original, load_root_schema(), evidence=evidence)
    base = ctx.union_state()
    a, b, c = (ctx.input_node_map[k] for k in ('A', 'B', 'C'))
    ctx.candidate = base
    ctx.active_batch = ActiveBatch(ctx, base, targets={a, b})
    submit_patch(ctx, RelationPatch(node_updates=[{'id': a, 'add_evidence': ['B-evidence'],
        'explanation': 'Explicitly retain the relevant evidence of B on its named successor.'}],
        retirements=[{'before': [{'kind': 'node', 'id': b}], 'after': [{'kind': 'node', 'id': a}],
                      'explanation': 'The evidenced equivalent referent survives as A.'}], explanation='An explicitly attributed replacement.'))
    nodes = {n.id: n for n in ctx.active_batch.preview.graph.nodes}
    assert b not in nodes and set(refs) <= set(nodes[a].sources)
    assert nodes[c].sources == next(n.sources for n in base.graph.nodes if n.id == c)
    assert SourceReference(source_id='A') in nodes[a].sources

    ctx.active_batch = ActiveBatch(ctx, base, targets={a, b})
    ctx.candidate = base
    with pytest.raises(ValueError, match='Unknown evidence'):
        submit_patch(ctx, RelationPatch(node_updates=[{'id': a, 'add_evidence': ['invented'], 'explanation': 'Invalid.'}], explanation='Must fail.'))
    assert not ctx.active_batch.pending and ctx.active_batch.preview is None


def test_stale_scheduled_signals_do_not_expose_retired_ids_as_current():
    ctx, base, a, b, e = setup()
    current = base.model_copy(update={"graph": base.graph.model_copy(
        update={"edges": tuple(x for x in base.graph.edges if x.id != e)})})
    task = LocalTask("later", "reconcile", {a, b, e}, checks=[{
        "id": "triangle:" + e, "kind": "triangle", "node_ids": [a, b], "edge_ids": [e]}])
    payload = relation_context(ctx, current, task)
    assert e not in payload["scope"]["targets"]
    assert payload["signals"] == [{"kind": "triangle", "node_ids": sorted([a, b]), "edge_ids": []}]


def test_explicit_equivalent_edge_merge_preserves_branch_union_and_exact_sources():
    from cortex.schema import GraphNode, SourceReference

    owner = GraphNode.model_validate({**concept("A", "a").model_dump(mode="json"),
        "merge_status": "conflict", "consolidation_status": "semantic_conflict",
        "sources": [{"source_id": "first"}, {"source_id": "second"}],
        "alternatives": [
            {"alternative_id": k, "overrides": {"description": meaning},
             "sources": [{"source_id": k}]}
            for k, meaning in [("first", "First representation"), ("second", "Second representation")]]})
    links = [edge(k, "A", "B", "has_feature").model_copy(update={
        "properties": {"description": "The same business connection.", "alternative_ids": [branch]},
        "sources": (SourceReference(source_id=branch),)})
        for k, branch in [("one", "first"), ("two", "second")]]
    ctx = IntegrationContext(state([*catalog(), owner, concept("B", "b")], links), state(), load_root_schema())
    ctx.candidate = ctx.union_state()
    a, b = "A", "B"
    one, two = "one", "two"
    ctx.active_batch = ActiveBatch(ctx, ctx.candidate, targets={a, b})
    original = next(n for n in ctx.candidate.graph.nodes if n.id == a)
    submit_patch(ctx, RelationPatch(edge_merges=[{
        "survivor_id": one, "source_ids": [two],
        "explanation": "Same assertion under separately retained source branches."}], explanation="Preserve scope union."))
    final = ctx.active_batch.preview
    survivor = next(e for e in final.graph.edges if e.id == one)
    assert survivor.properties["alternative_ids"] == ["first", "second"]
    assert set(survivor.sources) == {SourceReference(source_id="first"), SourceReference(source_id="second")}
    assert two not in {e.id for e in final.graph.edges}
    assert next(n for n in final.graph.nodes if n.id == a) == original
