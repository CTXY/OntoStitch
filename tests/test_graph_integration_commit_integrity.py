"""Compile-time type safety and complete catalog provenance at commit."""

import pytest
from test_graph_integration import business_contract

from cortex.application.ontology_graph import GraphManager
import cortex.capabilities.graph_merge.graph.publication as compiler
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.graph.edit_contract import (
    IdentityDecision,
    IntegrationProposal,
)
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState, load_root_schema


def catalog(prefix=""):
    return [
        GraphNode(
            id=prefix + "table",
            node_type="table",
            name="objects",
            properties={"table_name": "objects"},
        ),
        GraphNode(
            id=prefix + "column",
            node_type="column",
            name="category",
            properties={"table_ref": prefix + "table", "column_name": "category"},
        ),
        GraphNode(
            id=prefix + "value",
            node_type="dimension_value",
            name="selected",
            properties={"column_ref": prefix + "column", "value": "selected"},
        ),
    ]


def concept(key, target="column"):
    return GraphNode(
        id=key, node_type="concept", name=key, properties={"targets": [[target]]}
    )


def event(key):
    return GraphNode(
        id=key,
        node_type="event",
        name="occurrence",
        properties={"definition": "A supported occurrence."},
    )


def state(nodes=(), edges=()):
    return GraphManager(load_root_schema()).prepare_state(
        GraphState(graph=GraphSpace(nodes=tuple(nodes), edges=tuple(edges), relation_contracts=(business_contract(),) if any(e.edge_type == "has_feature" for e in edges) else ()))
    )[0]


def context(old, new):
    result = IntegrationContext(old, new, load_root_schema())
    result.payload("identity")
    return result


@pytest.mark.parametrize("existing", [False, True])
def test_existing_semantic_id_cannot_change_type_during_compile(existing):
    source = state([*catalog(), concept("object")])
    ctx = context(source if existing else state(), state() if existing else source)
    key = "object" if existing else ctx.input_node_map["object"]
    ctx.read_ids.add(key)
    proposal = IntegrationProposal(
        edits=[
            {"operation": "update_node", "node": event(key).model_dump(mode="json")}
        ],
        changes=[
            {
                "before": [{"kind": "node", "id": key}],
                "after": [{"kind": "node", "id": key}],
                "explanation": "Correct the category.",
            }
        ],
    )
    with pytest.raises(ValueError, match="Existing node type cannot change"):
        compiler.compile_proposal(ctx, IdentityDecision(), proposal)


def test_type_correction_with_new_identity_has_rewrite_not_alias():
    ctx = context(state([*catalog(), concept("old")]), state())
    ctx.read_ids.add("old")
    proposal = IntegrationProposal(
        edits=[
            {
                "operation": "create_node",
                "node": event("corrected").model_dump(mode="json"),
            },
            {"operation": "delete_node", "node_id": "old"},
        ],
        changes=[
            {
                "before": [{"kind": "node", "id": "old"}],
                "after": [{"kind": "node", "id": "corrected"}],
                "explanation": "Source meaning is an occurrence, not an object category.",
            }
        ],
    )
    preview = compiler.compile_proposal(ctx, IdentityDecision(), proposal)
    result = compiler.commit_result(ctx, proposal, preview, run_id="type-correction")
    assert "old" not in {n.id for n in result.state.graph.nodes}
    assert (
        next(n for n in result.state.graph.nodes if n.id == "corrected").node_type
        == "event"
    )
    assert "old" not in {m.input_id for m in result.existing_node_mappings}
    assert result.rewrites[0].after[0].id == "corrected"


@pytest.mark.parametrize("prefix", ["", "other_"])
def test_all_catalog_source_edges_map_after_physical_alignment_and_dedup(prefix):
    old, incoming = state(catalog()), state(catalog(prefix))
    ctx = context(old, incoming)
    proposal = IntegrationProposal()
    preview = compiler.compile_proposal(ctx, IdentityDecision(), proposal)
    result = compiler.commit_result(ctx, proposal, preview, run_id="catalog-mapping")
    incoming_maps = {m.input_id: m.canonical_id for m in result.edge_mappings}
    existing_maps = {m.input_id: m.canonical_id for m in result.existing_edge_mappings}
    assert set(incoming_maps) == {e.id for e in incoming.graph.edges}
    assert set(existing_maps) == {e.id for e in old.graph.edges}
    assert set(incoming_maps.values()) == set(existing_maps.values())
    assert set(incoming_maps.values()) == {e.id for e in result.state.graph.edges}


def test_identity_alias_cannot_be_retyped_into_another_semantic_class():
    ctx = context(state(), state([*catalog(), concept("A"), concept("B")]))
    a, b = (ctx.input_node_map[key] for key in ("A", "B"))
    decision = IdentityDecision(
        groups=[
            {
                "members": [a, b],
                "meaning": "Same category",
                "reason": "Source identity evidence.",
            }
        ]
    )
    proposal = IntegrationProposal(
        edits=[{"operation": "update_node", "node": event(a).model_dump(mode="json")}],
        changes=[
            {
                "before": [{"kind": "node", "id": a}, {"kind": "node", "id": b}],
                "after": [{"kind": "node", "id": a}],
                "explanation": "Attempt to reinterpret an identity alias as an occurrence.",
            }
        ],
    )
    with pytest.raises(ValueError, match="Existing node type cannot change"):
        compiler.compile_proposal(ctx, decision, proposal)


def test_default_source_catalog_edge_maps_to_authored_ownership_when_it_suppresses_default():
    authored = GraphEdge(
        id="authored-owner",
        edge_type="belongs_to",
        from_node_id="column",
        to_node_id="table",
        properties={"description": "The category column belongs to the objects table."},
    )
    old, incoming = state(catalog(), [authored]), state(catalog("other_"))
    assert (
        next(e for e in old.graph.edges if e.edge_type == "belongs_to").reference_origin
        != "default"
    )
    ctx = context(old, incoming)
    proposal = IntegrationProposal()
    preview = compiler.compile_proposal(ctx, IdentityDecision(), proposal)
    result = compiler.commit_result(
        ctx, proposal, preview, run_id="authored-catalog-owner"
    )
    source_edge = next(e for e in incoming.graph.edges if e.edge_type == "belongs_to")
    mappings = {m.input_id: m.canonical_id for m in result.edge_mappings}
    assert mappings[source_edge.id] == authored.id
    assert next(e for e in result.state.graph.edges if e.id == authored.id) == authored


def test_business_edges_with_same_triple_keep_distinct_source_correspondences():
    edges = [
        GraphEdge(
            id=key,
            edge_type="has_feature",
            from_node_id="A",
            to_node_id="B",
            properties={"description": description},
        )
        for key, description in (
            ("first", "The association applies during the first reporting scope."),
            ("second", "The association applies during the second reporting scope."),
        )
    ]
    ctx = context(
        state(), state([*catalog(), concept("A"), concept("B", "value")], edges)
    )
    proposal = IntegrationProposal()
    preview = compiler.compile_proposal(ctx, IdentityDecision(), proposal)
    result = compiler.commit_result(
        ctx, proposal, preview, run_id="business-correspondence"
    )
    mappings = {m.input_id: m.canonical_id for m in result.edge_mappings}
    assert mappings["first"] != mappings["second"]
    final = {e.id: e for e in result.state.graph.edges}
    assert final[mappings["first"]].properties == edges[0].properties
    assert final[mappings["second"]].properties == edges[1].properties


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_ambiguous_catalog_commit_correspondence_is_not_silent(
    monkeypatch, duplicate
):
    ctx = context(state(), state(catalog()))
    proposal = IntegrationProposal()
    preview = compiler.compile_proposal(ctx, IdentityDecision(), proposal)
    real_apply = compiler.apply_merge_state

    def corrupt_catalog(*args, **kwargs):
        actual = real_apply(*args, **kwargs)
        edges = list(actual.graph.edges)
        if duplicate:
            edges.append(edges[0].model_copy(update={"id": "duplicate-catalog-edge"}))
        else:
            edges.pop(0)
        return actual.model_copy(
            update={"graph": actual.graph.model_copy(update={"edges": tuple(edges)})}
        )

    monkeypatch.setattr(compiler, "apply_merge_state", corrupt_catalog)
    with pytest.raises(ValueError, match="Expected one committed catalog edge"):
        compiler.commit_result(ctx, proposal, preview, run_id="invalid-catalog-commit")


def test_conflict_name_placeholder_survives_handle_publication_and_repository(tmp_path):
    from cortex.infrastructure.artifacts.graph_state import (
        FileSystemGraphStateRepository,
    )
    from cortex.schema import ConflictAlternative, SourceReference

    sources = tuple(SourceReference(source_id=key) for key in ("source_a", "source_b"))
    owner = event("occurrence").model_copy(
        update={
            "name": "occurrence",
            "merge_status": "conflict",
            "consolidation_status": "semantic_conflict",
            "sources": sources,
            "alternatives": tuple(
                ConflictAlternative(
                    alternative_id=key, overrides={"name": title}, sources=(source,)
                )
                for key, title, source in zip(
                    ("a", "b"),
                    ("Recorded occurrence", "Business occurrence"),
                    sources,
                    strict=True,
                )
            ),
        }
    )
    ctx = context(
        state(), state([event(owner.id).model_copy(update={"sources": sources})])
    )
    handle = ctx.input_node_map[owner.id]
    ctx.read_ids.add(handle)
    from cortex.capabilities.graph_merge.inputs import remap_node

    candidate = remap_node(owner, {owner.id: handle})
    assert candidate.name == handle
    proposal = IntegrationProposal(
        edits=[
            {"operation": "set_conflict", "node": candidate.model_dump(mode="json")}
        ],
        changes=[
            {
                "before": [{"kind": "node", "id": handle}],
                "after": [{"kind": "node", "id": handle}],
                "explanation": "Preserve competing supported descriptions.",
            }
        ],
    )
    preview = compiler.compile_proposal(ctx, IdentityDecision(), proposal)
    result = compiler.commit_result(
        ctx, proposal, preview, run_id="conflict-publication"
    )
    published = next(n for n in result.state.graph.nodes if n.id == owner.id)
    assert published.name == published.id
    assert published.alternatives == owner.alternatives
    repository = FileSystemGraphStateRepository(tmp_path, load_root_schema())
    repository.save(result.state)
    assert repository.load() == result.state


def test_remapping_does_not_change_ordinary_name_equal_to_old_id():
    from cortex.capabilities.graph_merge.inputs import remap_node

    ordinary = event("occurrence").model_copy(update={"name": "occurrence"})
    renamed = remap_node(ordinary, {"occurrence": "canonical"})
    assert renamed.id == "canonical"
    assert renamed.name == "occurrence"


def test_abstract_relation_is_readable_but_must_be_replaced_in_incoming_merge():
    legacy = GraphEdge(id='legacy', edge_type='has_attribute', from_node_id='A', to_node_id='B', properties={'description': 'The object has this recorded feature.'})
    incoming = state([*catalog(), concept('A'), concept('B', 'value')], [legacy])
    ctx = context(state(), incoming)
    with pytest.raises(ValueError, match='abstract relation base'):
        compiler.compile_proposal(ctx, IdentityDecision(), IntegrationProposal())
    eid = ctx.input_edge_map['legacy']
    concrete = ctx.edges[eid].model_copy(update={'edge_type': 'has_feature'})
    proposal = IntegrationProposal(
        relation_contracts=[business_contract()],
        edits=[{'operation': 'update_edge', 'edge': concrete.model_dump(mode='json')}],
        changes=[{'before': [{'kind': 'edge', 'id': eid}], 'after': [{'kind': 'edge', 'id': eid}], 'explanation': 'State the evidenced specific feature role.'}],
    )
    assert compiler.compile_proposal(ctx, IdentityDecision(), proposal).state.graph.edges
    # Reading old global knowledge does not require an unrelated migration.
    unrelated = context(incoming, state([event('new_event')]))
    assert compiler.compile_proposal(unrelated, IdentityDecision(), IntegrationProposal())
