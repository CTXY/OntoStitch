"""Identity integration preserves complete branch scopes and fixed-base drafts."""
from copy import deepcopy

import pytest

from graph_merge_fixtures import catalog, concept, edge, state

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.publication import Preview
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.graph.edit_contract import IntegrationProposal
from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch, compile_patch
from graph_merge_archive_fixtures import convert_legacy_patch

from cortex.capabilities.graph_merge.graph.provenance import validate_rewrite_sources
from cortex.capabilities.graph_merge.evidence import EvidenceItem
from cortex.schema import ConflictChoice, ConflictPoint, GraphNode, GraphSpace, GraphState, NodeContent, SourceReference, load_root_schema


def parse_legacy(raw, base):
    return MergePatch.model_validate(convert_legacy_patch(raw, base))


def content(node, **properties):
    return NodeContent(name=node.name, description=node.description, aliases=tuple(node.aliases),
                       tags=tuple(node.tags), properties={**node.properties, **properties}).model_dump(mode="json")


def points(ids):
    return [ConflictPoint(point_id="definition", aspect="meaning", description="Different complete interpretations.",
        choices=tuple(ConflictChoice(alternative_ids=(key,), statement=key) for key in ids)).model_dump(mode="json")]


def prepare(base):
    refs = {s for n in base.graph.nodes for s in n.sources} | {s for e in base.graph.edges for s in e.sources}
    evidence = tuple(EvidenceItem(id=f"Q{index}", source=s, case_id=s.case_id, content={"question": "Accepted source knowledge", "sql": "SELECT a FROM objects"})
                     for index, s in enumerate(sorted(refs, key=lambda s: s.model_dump_json())))
    ctx = IntegrationContext(base, GraphState(graph=GraphSpace()), load_root_schema(), evidence=evidence)
    ctx.task_visible_ids = {n.id for n in base.graph.nodes} | {e.id for e in base.graph.edges}
    ids = {e.source: e.id for e in evidence}
    return ctx, ids


def compile_group(ctx, base, group, aliases):
    patch = parse_legacy({"integrate_groups": [group], "explanation": "Integrate this confirmed identity atomically."}, base)
    payload = {"candidate_nodes": [n.model_dump(mode="json") for n in base.graph.nodes],
               "candidate_relations": [e.model_dump(mode="json") for e in base.graph.edges]}
    compiled = compile_patch(ctx, base, patch, targets=set(aliases), aliases=aliases, payload=payload)
    GraphManager(load_root_schema()).validate_state(compiled.state)
    lineage = LineageIndex(ctx)
    lineage.observe(base, compiled.integrated, compiled.integration_changes,
                    node_aliases=compiled.node_aliases, edge_aliases=compiled.edge_aliases)
    lineage.observe(compiled.integrated, compiled.state, compiled.changes)
    validate_rewrite_sources(ctx, IntegrationProposal(changes=lineage.source_changes(compiled.state)),
                            Preview(compiled.state, lineage.node_map, lineage.edge_map, []))
    return compiled


def test_normal_members_join_conflict_with_common_support_without_source_broadcast():
    a, b, c = concept("A", "a"), concept("B", "a"), concept("C", "a")
    base = state([*catalog(), a, b, c])
    ctx, evidence = prepare(base)
    group = {"owner_id": "A", "member_ids": ["A", "B", "C"],
        "alternatives": [{"alternative_id": key, "content": content(n, definition=key),
                          "evidence_ids": [evidence[n.sources[0]]]} for key, n in [("left", a), ("right", b)]],
        "conflict_points": points(["left", "right"]),
        "branch_dispositions": [
            {"source_node_id": "A", "source_alternative_id": None, "target_alternative_ids": ["left"], "explanation": "Retain first interpretation."},
            {"source_node_id": "B", "source_alternative_id": None, "target_alternative_ids": ["right"], "explanation": "Retain second interpretation."},
            {"source_node_id": "C", "source_alternative_id": None, "retained_in_common": True, "explanation": "Shared accepted knowledge is preserved by both complete branches."}],
        "explanation": "Preserve complete competing meanings and common facts."}
    before = base.model_dump_json()
    compiled = compile_group(ctx, base, group, {"A": "A", "B": "A", "C": "A"})
    owner = next(n for n in compiled.state.graph.nodes if n.id == "A")
    assert owner.common_sources == c.sources
    assert set(owner.sources) == {*a.sources, *b.sources, *c.sources}
    assert {n.id for n in compiled.state.graph.nodes}.isdisjoint({"B", "C"})
    assert all(not set(c.sources) & set(alt.sources) for alt in owner.alternatives)
    assert base.model_dump_json() == before


def test_normal_metric_edges_do_not_expand_to_every_new_alternative():
    a, b = [GraphNode(id=key, node_type="metric", name="Amount", properties={
        "expression": {"expr": "SUM(${value})", "refs": {"value": col}}},
        sources=(SourceReference(source_id=key),)) for key, col in [("A", "a"), ("B", "b")]]
    base = state([*catalog(), a, b], [edge("ea", "A", "a"), edge("eb", "B", "b")])
    ctx, evidence = prepare(base)
    group = {"owner_id": "A", "member_ids": ["A", "B"],
        "alternatives": [{"alternative_id": key, "content": content(n), "evidence_ids": [evidence[n.sources[0]]]}
                         for key, n in [("left", a), ("right", b)]],
        "conflict_points": points(["left", "right"]),
        "branch_dispositions": [{"source_node_id": n.id, "source_alternative_id": None,
            "target_alternative_ids": [key], "explanation": "This formula input belongs only to its retained definition."}
             for key, n in [("left", a), ("right", b)]], "explanation": "Retain two complete formulas."}
    compiled = compile_group(ctx, base, group, {"A": "A", "B": "A"})
    edges = {e.id: e for e in compiled.state.graph.edges}
    assert edges["ea"].properties["alternative_ids"] == ["left"]
    assert edges["eb"].properties["alternative_ids"] == ["right"]


def conflicted(node_id, branch_ids, target="a"):
    owner = concept(node_id, target)
    node, _ = GraphManager.compact_conflict(owner, [(key, content(owner, definition=f"{node_id}:{key}"),
        (SourceReference(source_id=f"{node_id}:{key}"),)) for key in branch_ids], conflict_points=tuple(ConflictPoint.model_validate(p) for p in points(branch_ids)))
    return node


def test_two_owners_same_branch_names_require_owner_qualified_dispositions():
    a, b = conflicted("A", ["one", "two"]), conflicted("B", ["one", "two"], target="b")
    base = state([*catalog(), a, b], [edge("ea", "A", "a").model_copy(update={"properties": {
        "description": "A's first definition only", "alternative_ids": ["one"]}}),
        edge("eb", "B", "b").model_copy(update={"properties": {"description": "B's first definition only", "alternative_ids": ["one"]}})])
    ctx, evidence = prepare(base)
    ids = ["A_one", "A_two", "B_one", "B_two"]
    group = {"owner_id": "A", "member_ids": ["A", "B"], "alternatives": [],
             "conflict_points": points(ids), "branch_dispositions": [], "explanation": "Keep every original complete version."}
    for node in [a, b]:
        for alt in node.alternatives:
            key = f"{node.id}_{alt.alternative_id}"
            group["alternatives"].append({"alternative_id": key,
                "content": GraphManager.resolve_alternative(node, alt).model_dump(mode="json"),
                "evidence_ids": [evidence[s] for s in alt.sources]})
            group["branch_dispositions"].append({"source_node_id": node.id, "source_alternative_id": alt.alternative_id,
                "target_alternative_ids": [key], "explanation": "Preserve the exact owner-qualified branch."})
    compiled = compile_group(ctx, base, group, {"A": "A", "B": "A"})
    edges = {e.id: e for e in compiled.state.graph.edges}
    assert edges["ea"].properties["alternative_ids"] == ["A_one"]
    assert edges["eb"].properties["alternative_ids"] == ["B_one"]
    broken = deepcopy(group)
    broken["branch_dispositions"][2]["source_node_id"] = "A"
    with pytest.raises(ValueError, match="source_routes_mismatch"):
        compile_group(ctx, base, broken, {"A": "A", "B": "A"})


def test_old_common_support_and_unscoped_edges_do_not_expand_to_a_new_branch():
    a, b = conflicted("A", ["one", "two"]), concept("B", "b")
    common = SourceReference(source_id="old-common")
    a, _ = GraphManager.compact_conflict(a, [(alt.alternative_id, GraphManager.resolve_alternative(a, alt), alt.sources)
                                           for alt in a.alternatives], common_sources=(common,), conflict_points=a.conflict_points)
    base = state([*catalog(), a, b], [edge("old_fact", "A", "a"), edge("new_fact", "B", "b")])
    ctx, evidence = prepare(base)
    group = {"owner_id": "A", "member_ids": ["A", "B"], "alternatives": [],
        "conflict_points": points(["left", "right", "new"]), "branch_dispositions": [
            {"source_node_id": "A", "source_alternative_id": None, "target_alternative_ids": ["left", "right"],
             "explanation": "Former common knowledge only applies to the two preserved source definitions."},
            {"source_node_id": "B", "source_alternative_id": None, "target_alternative_ids": ["new"],
             "explanation": "The new complete definition preserves the new member."}],
        "explanation": "Preserve old definitions and the independently scoped new one."}
    for key, alt in zip(["left", "right"], a.alternatives, strict=True):
        group["alternatives"].append({"alternative_id": key,
            "content": GraphManager.resolve_alternative(a, alt).model_dump(mode="json"),
            "evidence_ids": [evidence[s] for s in alt.sources]})
        group["branch_dispositions"].append({"source_node_id": "A", "source_alternative_id": alt.alternative_id,
            "target_alternative_ids": [key], "explanation": "Retain this original complete meaning."})
    group["alternatives"].append({"alternative_id": "new", "content": content(b), "evidence_ids": [evidence[b.sources[0]]]})
    compiled = compile_group(ctx, base, group, {"A": "A", "B": "A"})
    edges = {e.id: e for e in compiled.state.graph.edges}
    assert edges["old_fact"].properties["alternative_ids"] == ["left", "right"]
    assert edges["new_fact"].properties["alternative_ids"] == ["new"]
    owner = next(n for n in compiled.state.graph.nodes if n.id == "A")
    assert not owner.common_sources
    assert common not in next(a for a in owner.alternatives if a.alternative_id == "new").sources
    assert all(common in a.sources for a in owner.alternatives if a.alternative_id != "new")


@pytest.mark.parametrize("mode", ["rename", "split", "discard", "normal"])
def test_branch_rewrites_preserve_or_retire_scoped_edges(mode):
    a, b = conflicted("A", ["one", "two"]), concept("B", "a")
    base = state([*catalog(), a, b], [edge("first", "A", "a").model_copy(update={"properties": {
        "description": "First branch grounding", "alternative_ids": ["one"]}}),
        edge("second", "A", "a").model_copy(update={"properties": {"description": "Second branch grounding", "alternative_ids": ["two"]}})])
    ctx, evidence = prepare(base)
    new_ids = ["new_one", "new_two"] if mode != "split" else ["new_one", "new_one_extra", "new_two"]
    group = {"owner_id": "A", "member_ids": ["A", "B"], "explanation": "Explicitly rewrite each existing branch.",
             "branch_dispositions": []}
    if mode == "normal":
        group["content"] = content(b, definition="Preserved accepted first definition.")
    else:
        group["alternatives"] = [{"alternative_id": key, "content": content(b, definition=key),
            "evidence_ids": [evidence[b.sources[0]]]} for key in new_ids]
        group["conflict_points"] = points(new_ids)
        group["branch_dispositions"].append({"source_node_id": "B", "source_alternative_id": None,
            "retained_in_common": True, "explanation": "Compatible common content."})
    group["branch_dispositions"] += [
        {"source_node_id": "A", "source_alternative_id": "one", "explanation": "Retain first complete meaning.",
         **({"retained_in_common": True} if mode == "normal" else {"target_alternative_ids": ["new_one", "new_one_extra"] if mode == "split" else ["new_one"]})},
        {"source_node_id": "A", "source_alternative_id": "two", "explanation": "Explicit supported correction or preservation.",
         **({} if mode in {"normal", "discard"} else {"target_alternative_ids": ["new_two"]})}]
    compiled = compile_group(ctx, base, group, {"A": "A", "B": "A"})
    edges = {e.id: e for e in compiled.state.graph.edges}
    if mode == "normal":
        assert "alternative_ids" not in edges["first"].properties
        assert "second" not in edges
    else:
        assert edges["first"].properties["alternative_ids"] == (["new_one", "new_one_extra"] if mode == "split" else ["new_one"])
        if mode == "discard":
            assert "second" not in edges
        else:
            assert edges["second"].properties["alternative_ids"] == ["new_two"]


def test_integration_remaps_declared_consumer_refs_without_integrating_future_groups():
    a, b, c, d = concept("A", "a"), concept("B", "a"), concept("C", "b"), concept("D", "b")
    metric = GraphNode(id="metric", node_type="metric", name="Amount", properties={
        "expression": {"expr": "SUM(${value})", "refs": {"value": "B"}}}, sources=(SourceReference(source_id="metric"),))
    base = state([*catalog(), a, b, c, d, metric], [edge("depends", "metric", "B", "depends_on")])
    ctx, _ = prepare(base)
    compiled = compile_group(ctx, base, {"owner_id": "A", "member_ids": ["A", "B"],
        "content": content(a), "explanation": "Integrate only this current group."}, {"A": "A", "B": "A"})
    nodes = {n.id: n for n in compiled.state.graph.nodes}
    assert {"C", "D"} <= nodes.keys()
    assert nodes["metric"].properties["expression"]["refs"] == {"value": "A"}
    assert next(e for e in compiled.state.graph.edges if e.id == "depends").to_node_id == "A"


def test_unchanged_consumer_of_new_conflict_owner_is_in_semantic_review_scope():
    from cortex.capabilities.graph_merge.graph.impact import semantic_review_targets
    a, b = concept("A", "a"), concept("B", "a")
    metric = GraphNode(id="metric", node_type="metric", name="Amount", properties={
        "expression": {"expr": "SUM(${value})", "refs": {"value": "A"}}}, sources=(SourceReference(source_id="metric"),))
    base = state([*catalog(), a, b, metric], [edge("depends", "metric", "A", "depends_on")])
    ctx, evidence = prepare(base)
    group = {"owner_id": "A", "member_ids": ["A", "B"], "alternatives": [
        {"alternative_id": key, "content": content(n, definition=key), "evidence_ids": [evidence[n.sources[0]]]}
        for key, n in [("left", a), ("right", b)]], "conflict_points": points(["left", "right"]),
        "branch_dispositions": [{"source_node_id": n.id, "source_alternative_id": None,
            "target_alternative_ids": [key], "explanation": "Retain this complete definition."}
            for key, n in [("left", a), ("right", b)]], "explanation": "Preserve competing interpretations."}
    compiled = compile_group(ctx, base, group, {"A": "A", "B": "A"})
    assert next(n for n in compiled.state.graph.nodes if n.id == "metric") == metric
    impact = semantic_review_targets(base, compiled.state, {"A", "B"})
    assert "metric" in impact["targets"]


def test_complete_draft_replacement_retires_prior_created_objects_and_scope():
    base = state([*catalog(), concept("A", "a")])
    ctx, evidence = prepare(base)
    extra = {"id": "new", "node_type": "concept", "name": "Additional meaning", "description": "A local abstraction.",
             "properties": {"targets": [["a"]]}, "evidence_ids": [next(iter(evidence.values()))]}
    first = compile_patch(ctx, base, parse_legacy({"create_nodes": [extra], "explanation": "Create a local node."}, base), targets={"A"})
    assert any(n.id == "new" for n in first.state.graph.nodes)
    second = compile_patch(ctx, base, MergePatch(explanation="Withdraw the complete earlier proposal."), targets={"A"})
    assert second.state == base
    assert not any(n.id == "new" for n in second.state.graph.nodes)
    assert "new" not in second.batch.allowed


def test_same_owner_conflict_rewrite_maps_branches_and_inherited_edges():
    owner = conflicted('A', ['one', 'two'])
    base = state([*catalog(), owner], [edge('first', 'A', 'a').model_copy(update={
        'properties': {'description': 'Only first interpretation', 'alternative_ids': ['one']}})])
    ctx, evidence = prepare(base)
    patch = parse_legacy({'conflicts': [{'node_id': 'A',
        'alternatives': [{'alternative_id': key, 'content': GraphManager.resolve_alternative(owner, alt).model_dump(mode='json'),
                          'evidence_ids': [evidence[s] for s in alt.sources]}
                         for key, alt in zip(['renamed_one', 'renamed_two'], owner.alternatives, strict=True)],
        'conflict_points': points(['renamed_one', 'renamed_two']),
        'branch_dispositions': [{'source_node_id': 'A', 'source_alternative_id': alt.alternative_id,
            'target_alternative_ids': [key], 'explanation': 'Preserve this complete branch under its new ID.'}
            for key, alt in zip(['renamed_one', 'renamed_two'], owner.alternatives, strict=True)]}],
        'explanation': 'Rewrite one existing owner without an Identity group.'}, base)
    payload = {'candidate_nodes': [n.model_dump(mode='json') for n in base.graph.nodes]}
    result = compile_patch(ctx, base, patch, targets={'A'}, payload=payload)
    lineage = LineageIndex(ctx)
    lineage.observe(base, result.integrated, result.integration_changes,
                    node_aliases=result.node_aliases, edge_aliases=result.edge_aliases)
    lineage.observe(result.integrated, result.state, result.changes)
    validate_rewrite_sources(ctx, IntegrationProposal(changes=lineage.source_changes(result.state)),
                            Preview(result.state, lineage.node_map, lineage.edge_map, []))
    GraphManager(load_root_schema()).validate_state(result.state)
    assert next(e for e in result.state.graph.edges if e.id == 'first').properties['alternative_ids'] == ['renamed_one']


def test_same_owner_new_branch_does_not_inherit_old_common_or_unscoped_edge():
    owner, added = conflicted('A', ['one', 'two']), concept('B', 'a')
    common = SourceReference(source_id='shared-old')
    owner, _ = GraphManager.compact_conflict(owner, [(a.alternative_id, GraphManager.resolve_alternative(owner, a), a.sources)
        for a in owner.alternatives], common_sources=(common,), conflict_points=owner.conflict_points)
    base = state([*catalog(), owner, added], [edge('old', 'A', 'a')])
    ctx, evidence = prepare(base)
    alternatives = [{'alternative_id': a.alternative_id, 'content': GraphManager.resolve_alternative(owner, a).model_dump(mode='json'),
                     'evidence_ids': [evidence[s] for s in a.sources]} for a in owner.alternatives]
    alternatives.append({'alternative_id': 'new', 'content': content(added), 'evidence_ids': [evidence[added.sources[0]]]})
    conflict = {'node_id': 'A', 'alternatives': alternatives, 'conflict_points': points(['one', 'two', 'new']),
        'branch_dispositions': [{'source_node_id': 'A', 'source_alternative_id': a.alternative_id,
            'target_alternative_ids': [a.alternative_id], 'explanation': 'Retain the old meaning.'} for a in owner.alternatives]
            + [{'source_node_id': 'A', 'target_alternative_ids': ['one', 'two'],
                'explanation': 'The previous common assertion has not been established for the new branch.'}]}
    patch = parse_legacy({'conflicts': [conflict], 'explanation': 'Add an explicitly scoped new alternative.'}, base)
    result = compile_patch(ctx, base, patch, targets={'A'}, payload={'candidate_nodes': [n.model_dump(mode='json') for n in base.graph.nodes]})
    current = next(n for n in result.state.graph.nodes if n.id == 'A')
    assert not current.common_sources
    assert common not in next(a for a in current.alternatives if a.alternative_id == 'new').sources
    assert next(e for e in result.state.graph.edges if e.id == 'old').properties['alternative_ids'] == ['one', 'two']
    conflict.pop('branch_dispositions')
    with pytest.raises(ValueError, match='source_routes_mismatch'):
        compile_patch(ctx, base, parse_legacy({'conflicts': [conflict], 'explanation': 'Missing explicit old scopes.'}, base), targets={'A'})
