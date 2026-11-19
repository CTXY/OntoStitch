"""Behavioral contracts for the production lightweight relation path."""
import json

import pytest
from langchain_core.messages import AIMessage
from test_graph_integration import catalog, concept, edge, state
from test_incremental_agent import Chat

from cortex.capabilities.graph_merge.integration.batch import ActiveBatch
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.relation_context import (
    relation_context,
    relation_signature,
)
from cortex.capabilities.graph_merge.integration.relation_edits import (
    RelationPatch,
    submit_patch,
)
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
from cortex.schema import load_root_schema


def setup():
    original = state([*catalog(), concept("A", "a"), concept("B", "b")],
                     [edge("e", "A", "B", "has_feature")])
    ctx = IntegrationContext(state(), original, load_root_schema())
    base = ctx.union_state()
    a, b, e = ctx.input_node_map["A"], ctx.input_node_map["B"], ctx.input_edge_map["e"]
    ctx.candidate = base
    ctx.active_batch = ActiveBatch(ctx, base, targets={a, b})
    ctx.output_validator = ctx.active_batch.submit
    return ctx, base, a, b, e


def test_light_payload_and_runtime_use_real_partial_tool_without_metadata():
    ctx, base, a, b, e = setup()
    payload = relation_context(ctx, base, LocalTask("region", "reconcile", {a, b}))
    assert "physical_facts" not in payload and "node_guidance" not in payload
    assert "candidate_nodes" not in payload and "sources" not in json.dumps(payload)
    model = Chat([AIMessage(content="", tool_calls=[{
        "name": "submit_patch", "id": "patch",
        "args": {"edge_updates": [{"id": e, "properties": {"description": "A precise business feature."}}],
                           "explanation": "Clarify the existing business relation."}
    }])])
    result = IncrementalIntegrationAgent(model).invoke("reconcile", ctx, payload)
    assert len(model.requests) == 1
    tools = {getattr(t, "name", None) for t in model.tools}
    assert tools == {"inspect_graph", "lookup_catalog", "read_evidence", "read_guidance", "read_source_units", "inspect_draft", "submit_patch"}
    assert result.edge_updates
    current = ctx.active_batch.preview
    assert current.graph.nodes == ctx.active_batch.seed.graph.nodes
    changed = next(x for x in current.graph.edges if x.id == e)
    old = next(x for x in base.graph.edges if x.id == e)
    assert changed.sources == old.sources
    review = relation_context(ctx, current, LocalTask("review", "graph_assess", {a, b}),
                              before=base, assessment=True)
    assert review["actual_changes"]["edges"]["updated"] == [e]
    assert not review["before_nodes"] and "changed_contents" not in review
    assert "nodes" in review and not any("targets" in n for n in review["nodes"])


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


def test_region_coverage_ignores_task_names_but_not_semantic_inputs():
    ctx, base, a, b, _e = setup()
    one = relation_signature(ctx, base, LocalTask("one", "reconcile", {a, b}))
    two = relation_signature(ctx, base, LocalTask("two", "reconcile", {a, b}))
    assert one == two
    original = next(n for n in base.graph.nodes if n.id == a)
    revised = original.model_copy(update={"description": "Different business meaning"})
    current = base.model_copy(update={"graph": base.graph.model_copy(
        update={"nodes": tuple(revised if n.id == a else n for n in base.graph.nodes)})})
    assert one != relation_signature(ctx, current, LocalTask("one", "reconcile", {a, b}))


def test_selective_reads_reject_full_properties_and_preserve_requested_fields():
    ctx, base, a, b, _e = setup()
    payload = relation_context(ctx, base, LocalTask("read", "reconcile", {a, b}))
    model = Chat([
        AIMessage(content="", tool_calls=[{"name": "inspect_graph", "id": "read",
                                           "args": {"mode": "items", "ids": [a], "fields": ["properties.targets"]}}]),
        AIMessage(content='{"explanation":"No justified change."}')
    ])
    IncrementalIntegrationAgent(model).invoke("reconcile", ctx, payload)
    response = json.loads(model.requests[1][-1].content)
    assert set(response["items"][0]) == {"id", "properties"}
    assert len(model.requests) == 2



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
    from cortex.capabilities.graph_merge.models import EvidenceItem
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


def test_selective_reads_use_the_same_edge_fields_as_the_light_view():
    ctx, base, a, b, e = setup()
    payload = relation_context(ctx, base, LocalTask("read", "reconcile", {a, b}))
    model = Chat([
        AIMessage(content="", tool_calls=[{"name": "inspect_graph", "id": "read",
            "args": {"mode": "items", "ids": [e], "fields": ["from_node_id", "to_node_id", "edge_type", "evidence_ids"]}}]),
        AIMessage(content='{"explanation":"No change."}')
    ])
    IncrementalIntegrationAgent(model).invoke("reconcile", ctx, payload)
    value = json.loads(model.requests[1][-1].content)["items"][0]
    assert value == {"id": e, "from_node_id": a, "to_node_id": b, "edge_type": "has_feature", "evidence_ids": []}


def test_stale_scheduled_signals_do_not_expose_retired_ids_as_current():
    ctx, base, a, b, e = setup()
    current = base.model_copy(update={"graph": base.graph.model_copy(
        update={"edges": tuple(x for x in base.graph.edges if x.id != e)})})
    task = LocalTask("later", "reconcile", {a, b, e}, checks=[{
        "id": "triangle:" + e, "kind": "triangle", "node_ids": [a, b], "edge_ids": [e]}])
    payload = relation_context(ctx, current, task)
    assert e not in payload["scope"]["targets"]
    assert payload["signals"] == [{"kind": "triangle", "node_ids": sorted([a, b]), "edge_ids": []}]


def test_grounding_endpoints_have_readable_names_without_physical_metadata():
    ctx, base, a, b, _e = setup()
    physical = next(n for n in base.graph.nodes if n.node_type == "column")
    link = edge("anchor", a, physical.id, "grounded_on")
    current = base.model_copy(update={"graph": base.graph.model_copy(
        update={"edges": (*base.graph.edges, link)})})
    payload = relation_context(ctx, current, LocalTask("grounding", "reconcile", {a, b, link.id}))
    shown = next(n for n in payload["nodes"] if n["id"] == physical.id)
    assert shown["id"] == physical.id and shown["node_type"] == "column"
    assert shown["table"] == "objects" and shown["column"] == physical.name
    assert shown["owners"][0]["id"] == a


def test_metadata_shape_is_rejected_before_scope_expansion_without_poisoning_draft():
    ctx, _base, a, _b, e = setup()
    with pytest.raises(TypeError, match="must be a list"):
        submit_patch(ctx, RelationPatch(node_updates=[{
            "id": a, "properties": {"relation_metadata": {"edges": [e]}},
            "explanation": "Malformed group."}], explanation="Invalid group."))
    assert ctx.active_batch.preview is None and not ctx.active_batch.pending
    with pytest.raises(ValueError, match="not an edge"):
        submit_patch(ctx, RelationPatch(edge_updates=[{
            "id": e, "properties": {"relation_metadata": []}}], explanation="Invalid owner."))


def test_absent_optional_group_and_branch_fields_have_clear_empty_values():
    ctx, base, a, b, e = setup()
    payload = relation_context(ctx, base, LocalTask("read", "reconcile", {a, b}))
    model = Chat([
        AIMessage(content="", tool_calls=[
            {"name": "inspect_graph", "id": "groups", "args": {"mode": "items", "ids": [a], "fields": ["properties.relation_metadata"]}},
            {"name": "inspect_graph", "id": "branches", "args": {"mode": "items", "ids": [e], "fields": ["properties.alternative_ids"]}}]),
        AIMessage(content='{"explanation":"No change."}')])
    IncrementalIntegrationAgent(model).invoke("reconcile", ctx, payload)
    replies = {m.tool_call_id: json.loads(m.content) for m in model.requests[1] if m.type == "tool"}
    assert replies["groups"]["items"][0]["properties"]["relation_metadata"] is None
    assert replies["branches"]["items"][0]["properties"]["alternative_ids"] is None


def test_read_batch_keeps_valid_results_when_one_id_or_field_is_invalid():
    ctx, base, a, b, e = setup()
    payload = relation_context(ctx, base, LocalTask("read", "reconcile", {a, b}))
    model = Chat([
        AIMessage(content="", tool_calls=[{"name": "inspect_graph", "id": "read",
            "args": {"mode": "items", "ids": [a, e, "retired-id"], "fields": ["properties.targets", "properties"]}}]),
        AIMessage(content='{"explanation":"No change."}')])
    IncrementalIntegrationAgent(model).invoke("reconcile", ctx, payload)
    result = json.loads(model.requests[1][-1].content)
    assert result["items"][0]["properties"]["targets"]
    assert any(x.get("id") == "retired-id" for x in result["read_errors"])
    assert any(x.get("field") == "properties" for x in result["read_errors"])
    assert "targets" in result["items"][0]["properties"]


def test_last_call_reserves_completion_without_turning_failure_into_empty_success():
    ctx, base, a, b, _e = setup()
    payload = relation_context(ctx, base, LocalTask("bounded", "reconcile", {a, b}))
    model = Chat([
        AIMessage(content="", tool_calls=[{"name": "inspect_graph", "id": "read",
            "args": {"mode": "items", "ids": [a], "fields": ["properties.targets"]}}]),
        AIMessage(content='{"explanation":"Completed regional review; no justified change."}')])
    IncrementalIntegrationAgent(model, max_calls=2).invoke("reconcile", ctx, payload)
    assert [t.name for t in model.tools] == ["submit_patch"]
    assert ctx.active_batch.preview == base
    from cortex.capabilities.graph_merge.integration.relation_edits import RegionIncomplete
    with pytest.raises(RegionIncomplete, match="Missing a required relation contract"):
        submit_patch(ctx, RelationPatch(failure="Missing a required relation contract", explanation="Cannot safely complete."))
    fresh, state_before, x, y, _edge = setup()
    reader = Chat([AIMessage(content="", tool_calls=[{
        "name": "inspect_graph", "id": "late-read", "args": {"mode": "items", "ids": [x], "fields": ["properties.targets"]}}])])
    with pytest.raises(RuntimeError, match="exhausted.*local call budget"):
        IncrementalIntegrationAgent(reader, max_calls=1).invoke(
            "reconcile", fresh, relation_context(fresh, state_before, LocalTask("late", "reconcile", {x, y})))
    assert fresh.active_batch.preview is None


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
