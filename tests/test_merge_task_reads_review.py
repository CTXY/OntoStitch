"""Shared task views and review premises against authoritative graph snapshots."""
from types import SimpleNamespace

import pytest

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.evidence_refs import EvidencePresentation, compact_attribution
from cortex.capabilities.graph_merge.integration.relation_context import relation_context
from cortex.capabilities.graph_merge.integration.review import (
    AbsenceClaim, Review, ReviewFactError, absence_counterexamples, validate_review,
)
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.llm.relation_reads import RelationReads
from cortex.capabilities.graph_merge.llm.task_tools import build_read_tools
from cortex.schema import GraphNode, SourceReference, load_root_schema
from test_graph_integration import catalog, concept, edge, state


def context():
    first = concept("A", "a").model_copy(update={"properties": {
        "targets": [["a"], ["b"]], "definition": "An object matched by either physical representation."}})
    other_table = GraphNode(id="other", node_type="table", name="other", properties={"table_name": "other"})
    other_column = GraphNode(id="other.a", node_type="column", name="a", properties={"table_ref": "other", "column_name": "a"})
    graph = state([*catalog(), first, concept("B", "a"), other_table, other_column],
                  [edge("first", "A", "a"), edge("second", "A", "b")])
    ctx = IntegrationContext(state(), graph, load_root_schema())
    ctx.candidate = ctx.union_state()
    return ctx


def reader(ctx, **kwargs):
    return RelationReads(ctx, EvidencePresentation(ctx), **kwargs)


def test_catalog_resolves_same_column_name_by_table_and_reports_owner():
    ctx = context()
    current = reader(ctx)
    original = ctx.candidate.model_dump_json()
    first = current.lookup_catalog("objects", ["a"])
    second = current.lookup_catalog("other", ["a"])
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert first["items"][0]["table"] == "objects"
    assert first["items"][0]["owners"][0]["id"] == ctx.input_node_map["A"]
    assert second["items"][0]["table"] == "other"
    assert "owners" not in second["items"][0]
    assert current.lookup_catalog("objects", ["missing"])["missing_columns"] == ["missing"]
    assert ctx.candidate.model_dump_json() == original


def test_deleted_reference_reads_before_without_substituting_candidate():
    ctx = context()
    baseline = ctx.candidate
    ident = ctx.input_node_map["A"]
    ctx.candidate = baseline.model_copy(update={"graph": baseline.graph.model_copy(update={
        "nodes": tuple(node for node in baseline.graph.nodes if node.id != ident),
        "edges": tuple(edge for edge in baseline.graph.edges if edge.from_node_id != ident),
    })})
    reads = reader(ctx, before=baseline)
    assert reads.read("items", [ident], ["content"], view="before")["items"][0]["content"]["name"] == "A"
    assert reads.read("items", [ident])["missing_ids"] == [ident]
    review = Review.model_validate({"verdict": "revise", "issues": [{
        "refs": [{"kind": "node", "id": ident, "view": "before"}],
        "basis": [{"kind": "node", "id": ident, "view": "input"}],
        "problem": "The deleted object has no preserved meaning.",
        "required_outcome": "Preserve its recorded meaning in an explicit successor.",
    }]})
    checked = validate_review(review, ctx, before=baseline)
    assert checked.candidate_hash
    assert "candidate_hash" not in Review.model_json_schema()["properties"]
    bad = review.model_copy(update={"issues": (review.issues[0].model_copy(update={
        "refs": (review.issues[0].refs[0].model_copy(update={"view": "candidate"}),)}),)})
    with pytest.raises(ReviewFactError, match="does not exist"):
        validate_review(bad, ctx, before=baseline)


def test_absence_claim_checks_full_graph_even_when_page_does_not_show_edge():
    ctx = context()
    ident = ctx.input_node_map["A"]
    target = ctx.input_node_map["b"]
    page = reader(ctx).read("relations", [ident], limit=1)
    assert not page["complete"]
    assert all(item["to_node_id"] != target for item in page["items"])
    claim = AbsenceClaim(kind="concept_owner_absent", edge_type="grounded_on", to_node_id=target)
    found = absence_counterexamples(ctx, claim)
    assert found and found[0]["from_node_id"] == ident
    issue = {"refs": [{"kind": "node", "id": ident}], "basis": [{"kind": "node", "id": target}],
             "problem": "There is no existing owner.", "required_outcome": "Preserve a supported binding.",
             "claim": claim.model_dump()}
    with pytest.raises(ReviewFactError) as failure:
        validate_review(Review(verdict="revise", issues=[issue]), ctx)
    assert failure.value.counterexamples[0]["to_node_id"] == target


def test_unknown_or_branchless_claim_does_not_become_absence_proof():
    ctx = context()
    with pytest.raises(ReviewFactError, match="exact existing"):
        absence_counterexamples(ctx, AbsenceClaim(kind="relation_absent", edge_type="grounded_on", to_node_id="unknown"))
    with pytest.raises(ValueError, match="source node"):
        AbsenceClaim(kind="relation_absent", edge_type="grounded_on", to_node_id=ctx.input_node_map["a"], alternative_id="branch")


def test_absence_checks_branch_scope_without_treating_other_branches_as_present():
    from ontology_v2_fixtures import discussion_state
    graph = discussion_state()
    owner = next(node for node in graph.conflict_nodes if node.node_type == "generation_rule")
    target = next(node for node in graph.graph.nodes if node.node_type == "concept")
    branches = [alternative.alternative_id for alternative in owner.alternatives]
    scoped = edge("scoped", owner.id, target.id, "applies_to").model_copy(update={
        "properties": {"description": "Only this branch uses this operand.", "alternative_ids": branches[:1]}})
    graph = graph.model_copy(update={"graph": graph.graph.model_copy(update={
        "edges": tuple(edge for edge in graph.graph.edges if edge.from_node_id != owner.id) + (scoped,)})})
    ctx = IntegrationContext(graph, state(), load_root_schema())
    ctx.candidate = ctx.union_state()
    claim = AbsenceClaim(kind="relation_absent", edge_type="applies_to", to_node_id=target.id,
                         from_node_id=owner.id, alternative_id=branches[0])
    assert absence_counterexamples(ctx, claim)[0]["id"] == scoped.id
    assert not absence_counterexamples(ctx, claim.model_copy(update={"alternative_id": branches[1]}))


def test_packet_preserves_focus_and_physical_dependencies_without_duplicate_properties():
    ctx = context()
    ident = ctx.input_node_map["A"]
    payload = relation_context(ctx, ctx.candidate, LocalTask("local", "reconcile", {ident}))
    focus = next(node for node in payload["nodes"] if node["id"] == ident)
    assert focus["properties"]["targets"] == [[ctx.input_node_map["a"]], [ctx.input_node_map["b"]]]
    columns = [node for node in payload["nodes"] if node["node_type"] == "column"]
    assert {node["column"] for node in columns} == {"a", "b"}
    assert all(node["table"] == "objects" for node in columns)
    assert "revision" in payload and all("revision" not in node for node in payload["nodes"])
    assert "changed_contents" not in payload
    assert len({node["id"] for node in payload["nodes"]}) == len(payload["nodes"])


def test_explicit_rereads_are_complete_and_common_support_is_not_broadcast():
    ctx = context()
    shared, branch = SourceReference(source_id="shared"), SourceReference(source_id="branch")
    ctx.evidence = (SimpleNamespace(id="Qcommon", source=shared, content={"question": "shared"}),
                    SimpleNamespace(id="Qbranch", source=branch, content={"question": "branch"}))
    presentation = EvidencePresentation(ctx)
    presentation.read(["Qcommon"])
    assert presentation.read(["Qcommon"], refresh=True)["items"][0]["content"]
    transport = compact_attribution({"id": "owner", "node_type": "concept", "sources": [shared.model_dump(), branch.model_dump()],
        "common_sources": [shared.model_dump()], "alternatives": [{"alternative_id": "one", "sources": [branch.model_dump()]}]}, ctx)
    assert transport["common_evidence_ids"] == ["Qcommon"]
    assert transport["alternatives"][0]["evidence_ids"] == ["Qbranch"]
    tools = build_read_tools(ctx, {})
    ident = ctx.input_node_map["A"]
    args = {"mode": "items", "ids": [ident], "fields": ["properties.targets"]}
    assert tools["inspect_graph"].invoke(args) == tools["inspect_graph"].invoke(args)
    assert tools["inspect_draft"].invoke({}) == {"status": "no_draft"}


def test_repair_fields_keep_before_and_candidate_values_separate():
    from cortex.capabilities.graph_merge.integration.edit_feedback import current_repair_fields
    ctx = context()
    baseline = ctx.candidate
    ident = ctx.input_node_map["A"]
    ctx.candidate = baseline.model_copy(update={"graph": baseline.graph.model_copy(update={
        "nodes": tuple(node.model_copy(update={"name": "Revised object"}) if node.id == ident else node
                       for node in baseline.graph.nodes)})})
    finding = {"issues": [{"refs": [{"kind": "node", "id": ident, "view": view, "fields": ["name"]}
                                     for view in ("before", "candidate")]}]}
    fields = current_repair_fields(ctx.candidate, [finding], context=ctx, before=baseline)
    assert {row["view"]: row["value"] for row in fields} == {"before": "A", "candidate": "Revised object"}


def test_review_rejects_a_nonexistent_field_instead_of_accepting_a_typo():
    ctx = context()
    ident = ctx.input_node_map["A"]
    issue = {"refs": [{"kind": "node", "id": ident, "fields": ["meaning"]}],
             "basis": [{"kind": "node", "id": ident}], "problem": "A field is wrong.",
             "required_outcome": "Preserve the recorded definition."}
    with pytest.raises(ReviewFactError, match="Unknown review field"):
        validate_review(Review(verdict="revise", issues=[issue]), ctx)


def test_current_conflict_guidance_uses_public_patch_not_storage_schema():
    tools = build_read_tools(context(), {})
    result = tools['read_guidance'].invoke({'topic': 'conflicts'})
    assert 'MergePatch.conflicts' in result['guidance']
    assert 'common_evidence_ids' in result['guidance']
    assert 'node_schema' not in result
    assert 'Code compacts storage' in result['guidance']
