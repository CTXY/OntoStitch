"""The reviewer sees actual edits even when the author's explanation omits them."""

from test_graph_integration import (
    Agent,
    business_contract,
    catalog,
    concept,
    contexts,
    edge,
    state,
)

from cortex.capabilities.graph_merge.integration.assessment import assessment_view
from cortex.capabilities.graph_merge.integration.compiler import compile_proposal
from cortex.capabilities.graph_merge.integration.engine import GraphIntegrationEngine
from cortex.capabilities.graph_merge.integration.models import (
    IdentityDecision,
    IntegrationProposal,
)
from cortex.schema import GraphNode, load_root_schema


def test_isolated_addition_without_changes_is_reviewed_before_commit():
    original = GraphNode(
        id="source",
        node_type="event",
        name="source occurrence",
        properties={"definition": "The source occurrence"},
    )
    fresh = original.model_copy(update={"id": "fresh", "name": "A distinct occurrence"})

    class AdditionAgent(Agent):
        def invoke(self, phase, context, payload):
            if phase == "propose":
                self.phases.append(phase)
                return IntegrationProposal(
                    edits=[
                        {
                            "operation": "create_node",
                            "node": fresh.model_dump(mode="json"),
                        }
                    ]
                )
            if phase == "assess":
                assert payload["proposal"]["changes"] == []
                assert payload["actual_changes"]["nodes"]["created"] == ["fresh"]
                assert "fresh" in {n["id"] for n in payload["candidate_nodes"]}
            return super().invoke(phase, context, payload)

    result = GraphIntegrationEngine(AdditionAgent()).merge(
        state(), state([original]), load_root_schema()
    )
    assert "fresh" in {n.id for n in result.state.graph.nodes}


def test_all_edge_writes_visible_beyond_adjacency_limit():
    c = contexts(state(), state([*catalog(), concept("A", "a"), concept("B", "b")]))
    a, b = c.input_node_map["A"], c.input_node_map["B"]
    edits = [
        {
            "operation": "create_edge",
            "edge": edge(f"new{i}", a, b, "has_feature")
            .model_copy(update={"properties": {"description": f"Synthetic scope {i}"}})
            .model_dump(mode="json"),
        }
        for i in range(65)
    ]
    preview = compile_proposal(c, IdentityDecision(), IntegrationProposal(edits=edits, relation_contracts=[business_contract()]))
    payload = assessment_view(c, IdentityDecision(), preview)
    assert len(payload["actual_changes"]["relations"]["created"]) == 65
    assert len(payload["candidate_relations"]) == 65
    assert not payload["more_candidate_relations"]


def test_deletions_show_before_images_and_edge_only_changes_show_endpoints():
    c = contexts(
        state(),
        state(
            [*catalog(), concept("A", "a"), concept("B", "b")],
            [edge("ab", "A", "B", "has_attribute")],
        ),
    )
    a, b, ab = c.input_node_map["A"], c.input_node_map["B"], c.input_edge_map["ab"]
    proposal = IntegrationProposal(
        edits=[
            {"operation": "delete_edge", "edge_id": ab},
            {"operation": "delete_node", "node_id": b},
        ],
        changes=[
            {
                "before": [{"kind": "edge", "id": ab}, {"kind": "node", "id": b}],
                "after": [],
                "explanation": "Synthetic evidence-backed deletion.",
            }
        ],
    )
    preview = compile_proposal(c, IdentityDecision(), proposal)
    c.read_ids.clear()
    payload = assessment_view(c, IdentityDecision(), preview)
    assert payload["deleted_nodes"][0]["id"] == b
    assert payload["deleted_relations"][0]["id"] == ab
    assert a in {n["id"] for n in payload["candidate_nodes"]}


def test_new_relation_contract_is_visible_without_other_changes():
    c = contexts(state(), state([*catalog(), concept("A", "a")]))
    contract = c.schema.edge_type_index()["has_attribute"].model_copy(
        update={"id": "custom_role"}
    )
    proposal = IntegrationProposal(relation_contracts=[contract])
    preview = compile_proposal(c, IdentityDecision(), proposal)
    payload = assessment_view(c, IdentityDecision(), preview)
    assert payload["changed_relation_contracts"][0]["id"] == "custom_role"
