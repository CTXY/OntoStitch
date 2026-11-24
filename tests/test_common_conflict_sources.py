"""Common support survives branch reads, choices and provenance audits."""
import pytest

from ontology_v2_fixtures import discussion_state
from test_graph_manager import apply

from cortex.application.merge_graph import GraphMergeResult
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.publication import Preview
from cortex.capabilities.graph_merge.consolidation.conflicts import ConflictResolution, resolution_edits
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.graph.edit_contract import Change, Element, IntegrationProposal
from cortex.capabilities.graph_merge.graph.provenance import SourceCoverageError, validate_rewrite_sources
from cortex.capabilities.online_context.output.projection import (
    build_downstream_guidance, project_graph_state, semantic_asset_payload,
)
from cortex.capabilities.retrieve_db.retrieval.index import RepresentationIndex
from cortex.schema import GraphSpace, GraphState, SourceReference, alternative_sources, export_inline_graph, load_root_schema
from scripts.audit_incremental_merge import audit_result
from test_online_projection import choice_point


COMMON = SourceReference(source_id="shared", locator="qa", case_id="shared")


def shared_state():
    state = discussion_state()
    owner = state.conflict_nodes[0]
    merged, conflict = GraphManager.compact_conflict(owner, [
        (a.alternative_id, GraphManager.resolve_alternative(owner, a), a.sources)
        for a in owner.alternatives
    ], common_sources=(COMMON,), conflict_points=owner.conflict_points)
    return state.model_copy(update={"graph": state.graph.model_copy(update={
        "nodes": tuple(merged if n.id == owner.id else n for n in state.graph.nodes)
    })}), conflict


def test_empty_common_attribution_keeps_legacy_serialization_and_roundtrips():
    original = discussion_state()
    assert "common_sources" not in original.model_dump_json()
    owner = original.conflict_nodes[0]
    assert "common_sources" not in owner.as_conflict().model_dump_json()
    assert GraphState.model_validate_json(original.model_dump_json()) == original

    state, conflict = shared_state()
    GraphManager(load_root_schema()).validate_state(state)
    assert state.conflict_nodes[0].as_conflict() == conflict
    assert GraphState.model_validate(export_inline_graph(state)) == state
    assert state.conflict_nodes[0].without_conflict().common_sources == ()
    assert "common_sources" not in state.conflict_nodes[0].without_conflict().model_dump_json()


def test_common_coverage_is_required_and_branch_specific_attribution_is_unchanged():
    original = discussion_state().conflict_nodes[0]
    state, _ = shared_state()
    owner = state.conflict_nodes[0]
    assert owner.alternatives == original.alternatives
    assert COMMON in owner.sources
    for alternative in owner.alternatives:
        assert COMMON not in alternative.sources
        assert set(alternative_sources(owner, alternative)) == {COMMON, *alternative.sources}
    broken = state.model_copy(update={"graph": state.graph.model_copy(update={
        "nodes": tuple(n.model_copy(update={"sources": tuple(s for s in n.sources if s != COMMON)})
                       if n.id == owner.id else n for n in state.graph.nodes)
    })})
    with pytest.raises(ValueError, match="common and alternatives"):
        GraphState.model_validate(broken.model_dump(mode="json"))


def test_recompaction_preserves_common_unless_explicitly_replaced():
    state, _ = shared_state()
    owner = state.conflict_nodes[0]
    candidates = [(a.alternative_id, GraphManager.resolve_alternative(owner, a), a.sources)
                  for a in owner.alternatives]
    kept, _ = GraphManager.compact_conflict(owner, candidates)
    assert kept.common_sources == (COMMON,)
    cleared, _ = GraphManager.compact_conflict(owner, candidates, common_sources=())
    assert not cleared.common_sources
    assert COMMON not in cleared.sources


def test_extraction_schema_does_not_advertise_code_managed_common_sources():
    from cortex.capabilities.sql_training.learning.inputs import conflict_operation_schema
    schema = conflict_operation_schema()["properties"]
    assert "common_sources" not in schema["node"]["properties"]
    assert "common_sources" not in schema["conflict"]["properties"]


def test_legacy_group_keeps_existing_common_conflict_but_refuses_reassignment():
    from cortex.capabilities.graph_merge.models import FinalNodeGroup, NodeRepresentation, RepresentationRef
    from cortex.capabilities.graph_merge.reconciliation.service import NodeGroupReconciler
    state, _ = shared_state()
    owner = state.conflict_nodes[0]
    member = NodeRepresentation(ref=RepresentationRef(origin="existing_node", ref_id=owner.id),
        node_type=owner.node_type, content=GraphManager.resolve_alternative(owner, owner.alternatives[0]), sources=owner.sources)
    group = FinalNodeGroup(group_id="legacy", node_type=owner.node_type, existing_node_id=owner.id, members=(member,))
    reconciler = NodeGroupReconciler()
    kept = reconciler.reconcile_group(group, schema=load_root_schema(), historical_conflict=owner)
    assert kept.node == owner
    assert kept.conflict.common_sources == (COMMON,)
    with pytest.raises(ValueError, match="Legacy group consolidation.*incremental"):
        reconciler.reconcile_group(group, schema=load_root_schema(), historical_conflict=owner, force_reconcile=True)


def test_public_branch_reads_and_resolution_keep_only_common_and_selected_support():
    state, _ = shared_state()
    manager = GraphManager(load_root_schema(), state)
    owner = state.conflict_nodes[0]
    selected = owner.alternatives[0]
    materialized = manager.get_node(owner.id, alternative_id=selected.alternative_id)
    assert materialized.sources == alternative_sources(owner, selected)
    assert materialized.common_sources == ()
    result = apply(manager, {"operation": "resolve_conflict", "node_id": owner.id,
                             "alternative_id": selected.alternative_id})
    assert result.status == "applied", result.errors
    assert manager.get_node(owner.id) == materialized


def test_retrieval_and_guidance_keep_common_support_without_repeating_branch_attribution():
    state, _ = shared_state()
    owner = state.conflict_nodes[0]
    index = RepresentationIndex.build(state)
    for alternative in owner.alternatives:
        doc = next(d for d in index.documents if d.alternative_id == alternative.alternative_id)
        assert doc.structured_signals["sources"] == [s.model_dump(mode="json") for s in alternative_sources(owner, alternative)]
        assert doc.structured_signals["common_sources"] == [COMMON.model_dump(mode="json")]
    detail = semantic_asset_payload(owner)
    assert detail["common_sources"] == [COMMON.model_dump(mode="json")]
    assert all(COMMON.model_dump(mode="json") not in a["sources"] for a in detail["alternatives"])

    selected = owner.alternatives[0]
    projected, _ = project_graph_state(schema=load_root_schema(), state=state,
        selected_node_ids={n.id for n in state.graph.nodes},
        choice_points=(choice_point(owner.alternatives),),
        selected_by_point={"definition": (selected.alternative_id,)})
    resolved = next(n for n in projected.graph.nodes if n.id == owner.id)
    assert resolved.sources == alternative_sources(owner, selected)
    assert not resolved.common_sources
    guidance = build_downstream_guidance(question="How many users?", initial_sql=None,
        graph=projected.graph, interaction_guidance=(), resolved_guidance_content="")
    asset = next(a for a in guidance.ontology_assets if a["node_id"] == owner.id)
    assert asset["status"] == "reference"
    assert asset["action"] == GraphManager.resolve_alternative(owner, selected).properties["action"]
    assert state.conflict_nodes[0].alternatives == owner.alternatives


def test_projecting_a_subset_recompacts_with_common_support_intact():
    state, _ = shared_state()
    owner = state.conflict_nodes[0]
    third = GraphManager.resolve_alternative(owner, owner.alternatives[0]).model_copy(deep=True)
    third.properties["action"] = "Count only qualifying active users."
    expanded, _ = GraphManager.compact_conflict(owner,
        [(a.alternative_id, GraphManager.resolve_alternative(owner, a), a.sources) for a in owner.alternatives]
        + [("third", third, (SourceReference(source_id="third"),))], conflict_points=owner.conflict_points)
    state = state.model_copy(update={"graph": state.graph.model_copy(update={
        "nodes": tuple(expanded if n.id == owner.id else n for n in state.graph.nodes)})})
    retained = tuple(a.alternative_id for a in owner.alternatives)
    projected, _ = project_graph_state(schema=load_root_schema(), state=state,
        selected_node_ids={n.id for n in state.graph.nodes}, choice_points=(choice_point(expanded.alternatives),),
        selected_by_point={"definition": retained})
    result = projected.conflict_nodes[0]
    assert result.common_sources == (COMMON,)
    assert {a.alternative_id for a in result.alternatives} == set(retained)
    assert SourceReference(source_id="third") not in result.sources
    GraphManager(load_root_schema()).validate_state(projected)


def test_explicit_branch_discard_matches_runtime_and_public_source_audits():
    source, _ = shared_state()
    owner = source.conflict_nodes[0]
    selected = owner.alternatives[0]
    edits, changes = resolution_edits(source, [ConflictResolution(node_id=owner.id,
        keep_alternative_id=selected.alternative_id,
        discard_reasons={a.alternative_id: "Explicit supported correction." for a in owner.alternatives[1:]})])
    resolved = type(owner).model_validate(edits[0]["node"])
    output = source.model_copy(update={"graph": source.graph.model_copy(update={
        "nodes": tuple(resolved if n.id == owner.id else n for n in source.graph.nodes)
    })})
    changes = [Change.model_validate(c) for c in changes] + [Change(
        before=(Element(kind="node", id=owner.id),), after=(Element(kind="node", id=owner.id),),
        explanation="Resolve the owner while retaining common knowledge.")]
    empty = GraphState(graph=GraphSpace())
    context = IntegrationContext(source, empty, load_root_schema())
    node_map = {n.id: n.id for n in source.graph.nodes}
    edge_map = {e.id: e.id for e in source.graph.edges}
    preview = Preview(output, node_map, edge_map, [])
    validate_rewrite_sources(context, IntegrationProposal(changes=tuple(changes)), preview)
    rewrites = [{"before": [{**r.model_dump(exclude_none=True), "view": "existing"} for r in c.before],
                 "after": [{**r.model_dump(exclude_none=True), "view": "result"} for r in c.after],
                 "explanation": c.explanation, "base_revision": "before", "result_revision": "after"}
                for c in changes]
    result = GraphMergeResult(state=output, node_mappings=(), edge_mappings=(), diagnostics=(),
        existing_node_mappings=[{"input_id": k, "canonical_id": k} for k in node_map],
        existing_edge_mappings=[{"input_id": k, "canonical_id": k} for k in edge_map], rewrites=rewrites)
    report = audit_result(source, empty, result, load_root_schema())
    assert report["status"] == "passed", report["errors"]

    lost = resolved.model_copy(update={"sources": selected.sources})
    invalid = output.model_copy(update={"graph": output.graph.model_copy(update={
        "nodes": tuple(lost if n.id == owner.id else n for n in output.graph.nodes)
    })})
    with pytest.raises(SourceCoverageError):
        validate_rewrite_sources(context, IntegrationProposal(changes=tuple(changes)), Preview(invalid, node_map, edge_map, []))
    report = audit_result(source, empty, result.model_copy(update={"state": invalid}), load_root_schema())
    assert any(e["code"] == "rewrite_source_loss" for e in report["errors"])


def test_discard_cannot_drop_evidence_shared_with_a_retained_branch():
    source, _ = shared_state()
    owner = source.conflict_nodes[0]
    selected, discarded = owner.alternatives
    updated = selected.model_copy(update={"sources": (*selected.sources, *discarded.sources)})
    owner = owner.model_copy(update={"alternatives": (updated, discarded)})
    source = source.model_copy(update={"graph": source.graph.model_copy(update={
        "nodes": tuple(owner if n.id == owner.id else n for n in source.graph.nodes)})})
    resolved = owner.without_conflict().model_copy(update={
        **GraphManager.resolve_alternative(owner, updated).model_dump(mode="json"),
        "sources": alternative_sources(owner, selected)})
    candidate = source.model_copy(update={"graph": source.graph.model_copy(update={
        "nodes": tuple(resolved if n.id == owner.id else n for n in source.graph.nodes)})})
    proposal = IntegrationProposal(changes=(
        Change(before=(Element(kind="node", id=owner.id, alternative_id=discarded.alternative_id),), after=(), explanation="Discard this branch only."),
        Change(before=(Element(kind="node", id=owner.id),), after=(Element(kind="node", id=owner.id),), explanation="Resolve the owner.")))
    ctx = IntegrationContext(source, GraphState(graph=GraphSpace()), load_root_schema())
    with pytest.raises(SourceCoverageError):
        validate_rewrite_sources(ctx, proposal, Preview(candidate,
            {n.id: n.id for n in source.graph.nodes}, {e.id: e.id for e in source.graph.edges}, []))
    rewrites = [{"before": [{**r.model_dump(exclude_none=True), "view": "existing"} for r in c.before],
                 "after": [{**r.model_dump(exclude_none=True), "view": "result"} for r in c.after],
                 "explanation": c.explanation, "base_revision": "before", "result_revision": "after"}
                for c in proposal.changes]
    result = GraphMergeResult(state=candidate, node_mappings=(), edge_mappings=(), diagnostics=(), rewrites=rewrites,
        existing_node_mappings=[{"input_id": n.id, "canonical_id": n.id} for n in source.graph.nodes],
        existing_edge_mappings=[{"input_id": e.id, "canonical_id": e.id} for e in source.graph.edges])
    report = audit_result(source, GraphState(graph=GraphSpace()), result, load_root_schema())
    assert any(e["code"] == "rewrite_source_loss" for e in report["errors"])
