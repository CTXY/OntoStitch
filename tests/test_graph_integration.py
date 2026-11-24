import pytest
from graph_merge_fixtures import catalog, concept, edge, state, contexts, aligned_preview
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.identity.tools import build_tools
from cortex.capabilities.graph_merge.graph.edit_contract import IntegrationProposal
from cortex.schema import GraphNode, GraphState, GraphSpace, SourceReference, load_root_schema
"""Contracts for graph rewrites, source correspondence and independent review."""


def test_edit_envelope_rejects_ambiguous_flat_or_mixed_operations():
    from pydantic import ValidationError

    for edit in (
        {"action": "update_node", "node_id": "x", "name": "object"},
        {"operation": "update_node", "node_id": "x"},
        {"operation": "delete_node", "node_id": "x", "edge_id": "y"},
    ):
        with pytest.raises(ValidationError):
            IntegrationProposal(edits=[edit])
    proposal = IntegrationProposal(
        edits=[{"operation": "update_node", "node": {"id": "x"}}]
    )
    assert proposal.edits[0].node == {"id": "x"}


def test_evidence_bodies_are_shared_without_losing_provenance_or_variants():
    from cortex.capabilities.graph_merge.evidence import EvidenceItem

    refs = [SourceReference(source_id="case", locator=x) for x in ("qa", "QA")]
    evidence = [
        EvidenceItem(id="case", case_id="case", source=ref, content={"sql": "A"})
        for ref in refs
    ]
    evidence.append(
        EvidenceItem(id="case", case_id="case", source=refs[0], content={"sql": "B"})
    )
    c = IntegrationContext(state(), state(), load_root_schema(), evidence=evidence)
    result = {tool.name: tool for tool in build_tools(c)}['read_evidence'].invoke({"ids": ["case"]})
    assert result["total"] == 2
    assert len(result["items"][0]["source_refs"]) == 2
    assert {x["content"]["sql"] for x in result["items"]} == {"A", "B"}
    assert not result["missing_ids"]


def test_candidate_retrieval_reuses_visible_bodies_and_directory_is_semantic():
    c = contexts(state(), state([*catalog(), concept("A", "a"), concept("B", "b")]))
    inspect = build_tools(c, visible_source_ids=c.read_ids)[0]
    result = inspect.invoke({"mode": "candidates", "ids": [c.input_node_map["A"]]})
    assert result["items"][0]["already_in_context"] is True
    assert "node" not in result["items"][0]
    inspect = build_tools(c)[0]  # A fresh conversation has no supplied source bodies.
    result = inspect.invoke({"mode": "candidates", "ids": [c.input_node_map["A"]]})
    assert result["items"][0]["node"]["name"] == "B"
    assert inspect.invoke({"mode": "nodes"})["total"] == 2
    assert (
        inspect.invoke({"mode": "nodes", "ids": [c.input_node_map["a"]]})["items"][0][
            "node_type"
        ]
        == "column"
    )


def test_high_degree_node_relations_are_paginated_without_losing_node_body():
    concepts = [concept(f"c{i}", "a") for i in range(25)]
    c = contexts(
        state(),
        state(
            [*catalog(), *concepts],
            [edge(f"e{i}", f"c{i}", "c0", "is_a") for i in range(1, 25)],
        ),
    )
    inspect = build_tools(c)[0]
    root = c.input_node_map["c0"]
    first = inspect.invoke({"mode": "relations", "ids": [root], "limit": 7})
    assert (
        len(first["items"]) == 7 and first["next_offset"] == 7 and first["total"] == 24
    )
    body = inspect.invoke({"mode": "nodes", "ids": [root]})
    assert body["items"][0]["properties"]["targets"]


def test_candidate_view_retrieval_accepts_new_node_and_returns_only_source_matches():
    c = contexts(state(), state([*catalog(), concept("A", "a")]))
    new = concept("new", c.input_node_map["a"], name="A")
    c.candidate = GraphState(graph=GraphSpace(nodes=(*c.nodes.values(), new)))
    result = build_tools(c)[0].invoke(
        {"mode": "candidates", "view": "candidate", "ids": ["new"]}
    )
    assert result["items"][0]["id"] == c.input_node_map["A"]
    assert "new" not in c.nodes and "new" not in c.read_ids


def test_safe_remap_does_not_replace_sql_or_description_literals():
    c = contexts(
        state(), state([*catalog(), concept("A", "a")], [edge("ga", "A", "a")])
    )
    nodes = {node.id: node for node in aligned_preview(c).state.graph.nodes}
    assert nodes[c.input_node_map["A"]].name == "A"
    assert nodes[c.input_node_map["A"]].properties["targets"] == [
        [c.input_node_map["a"]]
    ]


def test_source_local_legacy_core_name_is_isolated_not_globally_redefined():
    schema = load_root_schema()
    historical = schema.edge_type_index()["has_attribute"].model_copy(
        update={"description": "Historical role wording"}
    )
    incoming = state(
        [*catalog(), concept("A", "a"), concept("B", "b")],
        [edge("ab", "A", "B", "has_attribute")],
    )
    incoming = incoming.model_copy(
        update={
            "graph": incoming.graph.model_copy(
                update={"relation_contracts": (historical,)}
            )
        }
    )
    c = contexts(state(), incoming)
    assert (
        c.schema.edge_type_index()["has_attribute"].description
        != historical.description
    )
    assert c.edges[c.input_edge_map["ab"]].edge_type != "has_attribute"


def test_evaluation_namespace_preserves_targets_metric_refs_and_edge_endpoints():
    from cortex.capabilities.sql_training.merge_evaluation import _collision_safe_state

    population = concept("A", "a").model_copy(update={
        "properties": {"targets": [["a"]], "definition": "Population A retains the recorded a representation."},
    })
    metric = GraphNode(
        id="M", node_type="metric", name="Population count",
        properties={"expression": {"expr": "COUNT(${population})", "refs": {"population": "A"}}},
    )
    graph = state(
        [*catalog(), population, metric],
        [edge("population-binding", "A", "a"), edge("count-input", "M", "A", "depends_on")],
    ).graph
    original = graph.model_dump(mode="json")
    remapped = _collision_safe_state("case", graph, state())
    GraphManager(load_root_schema()).validate_state(remapped)
    assert graph.model_dump(mode="json") == original
    nodes = {node.name: node for node in remapped.graph.nodes}
    owner, counted = nodes["A"], nodes["Population count"]
    assert owner.id != "A" and counted.id != "M"
    assert owner.properties["targets"] == [[nodes["a"].id]]
    assert owner.properties["definition"] == population.properties["definition"]
    assert counted.properties["expression"] == {
        "expr": "COUNT(${population})", "refs": {"population": owner.id},
    }
    assert {(edge.from_node_id, edge.to_node_id) for edge in remapped.graph.edges
            if edge.edge_type == "depends_on"} == {(counted.id, owner.id)}
    assert {(edge.from_node_id, edge.to_node_id) for edge in remapped.graph.edges
            if edge.edge_type == "grounded_on"} == {(owner.id, nodes["a"].id)}
