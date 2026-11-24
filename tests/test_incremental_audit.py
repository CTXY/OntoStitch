"""Public-result audits detect provenance loss without an engine recompile."""

from test_graph_integration import catalog, concept, edge, state

from cortex.application.merge_graph import GraphMergeResult
from cortex.application.ontology_graph import GraphManager
from cortex.schema import (
    GraphNode,
    GraphSpace,
    GraphState,
    SourceReference,
    load_root_schema,
)
from scripts.audit_incremental_merge import audit_result


def identity_result(source, *, output=None, rewrites=(), omitted=()):
    return GraphMergeResult(
        state=output or source,
        node_mappings=[
            {"input_id": n.id, "canonical_id": n.id}
            for n in source.graph.nodes
            if n.id not in omitted
        ],
        edge_mappings=[
            {"input_id": e.id, "canonical_id": e.id} for e in source.graph.edges
        ],
        diagnostics=(),
        rewrites=rewrites,
    )


def rewrite(key):
    return {
        "before": [{"kind": "node", "id": key, "view": "incoming"}],
        "after": [{"kind": "node", "id": key, "view": "result"}],
        "explanation": "Correct a source definition while retaining its object handle.",
        "base_revision": "before",
        "result_revision": "after",
    }


def check(source, result):
    return audit_result(state(), source, result, load_root_schema())


def fixture():
    return state([*catalog(), concept("A", "a")], [edge("binding", "A", "a")])


def test_complete_public_result_and_exact_source_coverage_pass():
    source = fixture()
    report = check(source, identity_result(source))
    assert report["status"] == "passed", report["errors"]
    assert report["coverage"]["incoming_node"]["mapped"] == len(source.graph.nodes)


def test_catalog_default_keeps_sources_when_its_child_gains_evidence():
    first, second = SourceReference(source_id="first"), SourceReference(source_id="second")
    nodes = [n.model_copy(update={"sources": (first,)}) if n.id == "a" else n for n in catalog()]
    source = state(nodes)
    updated = source.model_copy(update={"graph": source.graph.model_copy(update={
        "nodes": tuple(n.model_copy(update={"sources": (first, second)}) if n.id == "a" else n
                       for n in source.graph.nodes)
    })})
    output, _ = GraphManager(load_root_schema()).prepare_state(updated)
    owner = next(e for e in output.graph.edges if e.from_node_id == "a")
    assert set(owner.sources) == {first, second}
    assert all(second not in e.sources for e in output.graph.edges if e.from_node_id != "a")


def test_audit_accepts_only_exact_reversed_physical_join_pairs_and_cardinality():
    from cortex.schema import GraphEdge
    nodes = [
        GraphNode(id=t, node_type="table", name=t, properties={"table_name": t})
        for t in ("left", "right")
    ] + [
        GraphNode(id=f"{t}.{c}", node_type="column", name=c,
                  properties={"table_ref": t, "column_name": c})
        for t in ("left", "right") for c in ("id", "other")
    ]
    join = GraphEdge(id="join", edge_type="joins_with", from_node_id="left", to_node_id="right",
        properties={"column_pairs": [{"from_column_ref": "left.id", "to_column_ref": "right.id"}],
                    "description": "Recorded equality", "cardinality": "1:N"})
    source = state(nodes, [join])
    reversed_join = join.model_copy(update={"from_node_id": "right", "to_node_id": "left",
        "properties": {**join.properties, "column_pairs": [{"from_column_ref": "right.id", "to_column_ref": "left.id"}],
                       "cardinality": "N:1"}})
    def report(edge):
        output = source.model_copy(update={"graph": source.graph.model_copy(update={
            "edges": tuple(edge if e.id == "join" else e for e in source.graph.edges)
        })})
        return check(source, identity_result(source, output=output))
    assert report(reversed_join)["status"] == "passed"
    for properties in (
        {**reversed_join.properties, "cardinality": "1:N"},
        {**reversed_join.properties, "column_pairs": [{"from_column_ref": "right.other", "to_column_ref": "left.id"}]},
    ):
        assert any(e["code"] == "unsafe_edge_alias" for e in report(reversed_join.model_copy(update={"properties": properties}))["errors"])


def test_missing_source_disposition_is_rejected():
    source = fixture()
    report = check(source, identity_result(source, omitted={"A"}))
    assert any(e["code"] == "unaccounted_source_objects" for e in report["errors"])


def test_exact_locator_loss_cannot_hide_in_a_rewrite():
    source = fixture()
    original = next(n for n in source.graph.nodes if n.id == "A")
    source_ref = SourceReference(source_id="evidence", locator="QA")
    source = source.model_copy(
        update={
            "graph": source.graph.model_copy(
                update={
                    "nodes": tuple(
                        original.model_copy(update={"sources": (source_ref,)})
                        if n.id == "A"
                        else n
                        for n in source.graph.nodes
                    )
                }
            )
        }
    )
    output = source.model_copy(
        update={
            "graph": source.graph.model_copy(
                update={
                    "nodes": tuple(
                        n.model_copy(
                            update={
                                "sources": (
                                    source_ref.model_copy(update={"locator": "qa"}),
                                )
                            }
                        )
                        if n.id == "A"
                        else n
                        for n in source.graph.nodes
                    )
                }
            )
        }
    )
    report = check(
        source, identity_result(source, output=output, rewrites=[rewrite("A")])
    )
    assert any(e["code"] == "rewrite_source_loss" for e in report["errors"])


def test_in_place_content_rewrite_keeps_unchanged_edge_endpoints_auditable():
    source = fixture()
    output = source.model_copy(
        update={
            "graph": source.graph.model_copy(
                update={
                    "nodes": tuple(
                        n.model_copy(
                            update={
                                "description": "The same object, with clearer prose."
                            }
                        )
                        if n.id == "A"
                        else n
                        for n in source.graph.nodes
                    )
                }
            )
        }
    )
    result = identity_result(
        source, output=output, rewrites=[rewrite("A")], omitted={"A"}
    )
    report = check(source, result)
    assert report["status"] == "passed", report["errors"]


def test_coincident_id_is_not_an_implicit_missing_node_mapping():
    source = fixture()
    report = check(source, identity_result(source, omitted={"A"}))
    assert any(e["code"] == "unsafe_edge_alias" for e in report["errors"])


def test_real_conflict_resolves_each_complete_alternative():
    from ontology_v2_fixtures import discussion_state

    source = discussion_state()
    report = check(source, identity_result(source))
    assert report["status"] == "passed", report["errors"]
    branches = report["resolved_conflicts"][0]["alternatives"]
    assert len(branches) == 2
    assert len({b["resolved_content"]["properties"]["action"] for b in branches}) == 2
    assert all(b["sources"] for b in branches)


def test_publication_all_branch_scope_equals_unscoped_but_subset_does_not():
    from ontology_v2_fixtures import discussion_state
    from cortex.capabilities.graph_merge.graph.publication import assessed_semantic_content

    original = discussion_state()
    owner = original.conflict_nodes[0]
    target = next(n for n in original.graph.nodes if n.node_type == "concept")
    relation = edge("shared", owner.id, target.id, "applies_to")
    def variant(scope):
        props = dict(relation.properties)
        if scope is not None:
            props["alternative_ids"] = scope
        return original.model_copy(update={"graph": original.graph.model_copy(update={
            "edges": (*original.graph.edges, relation.model_copy(update={"properties": props}))
        })})
    all_ids = [a.alternative_id for a in owner.alternatives]
    assert assessed_semantic_content(variant(None)) == assessed_semantic_content(variant(all_ids))
    assert assessed_semantic_content(variant(None)) != assessed_semantic_content(variant(all_ids[:1]))


def test_whole_owner_rewrite_does_not_silently_discard_conflict_branches():
    from ontology_v2_fixtures import discussion_state

    source = discussion_state()
    owner = source.conflict_nodes[0]
    manager = GraphManager(load_root_schema())
    content = manager.resolve_alternative(owner, owner.alternatives[0])
    sources = tuple(
        dict.fromkeys(
            (*owner.sources, *(s for a in owner.alternatives for s in a.sources))
        )
    )
    normal = GraphNode(
        id=owner.id,
        node_type=owner.node_type,
        sources=sources,
        **content.model_dump(mode="json"),
    )
    output = GraphState(
        graph=GraphSpace(
            nodes=tuple(normal if n.id == owner.id else n for n in source.graph.nodes),
            edges=source.graph.edges,
            relation_contracts=source.graph.relation_contracts,
        )
    )
    report = check(
        source,
        identity_result(
            source, output=output, rewrites=[rewrite(owner.id)], omitted={owner.id}
        ),
    )
    assert any(e["code"] == "source_alternative_lost" for e in report["errors"])


def test_labels_and_reasons_do_not_make_two_distinct_rule_actions():
    from ontology_v2_fixtures import discussion_state

    source = discussion_state()
    owner = source.conflict_nodes[0]
    manager = GraphManager(load_root_schema())
    left = manager.resolve_alternative(owner, owner.alternatives[0])
    right = left.model_copy(
        update={
            "name": "Another label",
            "properties": {**left.properties, "reason": "Another explanation only."},
        }
    )
    compact, _ = manager.compact_conflict(
        owner,
        [
            (owner.alternatives[0].alternative_id, left, owner.alternatives[0].sources),
            (
                owner.alternatives[1].alternative_id,
                right,
                owner.alternatives[1].sources,
            ),
        ],
        conflict_points=owner.conflict_points,
    )
    output = source.model_copy(
        update={
            "graph": source.graph.model_copy(
                update={
                    "nodes": tuple(
                        compact if n.id == owner.id else n for n in source.graph.nodes
                    )
                }
            )
        }
    )
    report = check(
        source, identity_result(source, output=output, rewrites=[rewrite(owner.id)])
    )
    assert any(
        e["code"] == "duplicate_substantive_alternatives" for e in report["errors"]
    )
