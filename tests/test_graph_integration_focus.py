"""Focused repair must reduce context without losing dependencies or review scope."""

from test_graph_integration import catalog, concept, contexts, edge, state

from cortex.capabilities.graph_merge.integration.assessment import assessment_view
from cortex.capabilities.graph_merge.integration.compiler import compile_proposal
from cortex.capabilities.graph_merge.integration.models import (
    IdentityDecision,
    IntegrationProposal,
)
from cortex.capabilities.graph_merge.integration.scope import (
    content_consumers,
    focused_revision,
)
from cortex.capabilities.graph_merge.integration.tools import build_tools


def fixture():
    ctx = contexts(
        state(),
        state(
            [*catalog(), concept("A", "a"), concept("B", "b"), concept("C", "c")],
            [edge("attribute", "A", "B", "has_feature"), edge("ground", "C", "c")],
        ),
    )
    preview = compile_proposal(ctx, IdentityDecision(), IntegrationProposal())
    ctx.candidate = preview.state
    return ctx, preview


def test_edge_focus_is_full_objects_with_endpoints_and_catalog_not_unrelated_nodes():
    ctx, preview = fixture()
    eid = next(
        e.id for e in preview.state.graph.edges if e.edge_type == "has_feature"
    )
    packet = focused_revision(
        ctx, preview, IntegrationProposal(), [{"kind": "edge", "id": eid}]
    )
    assert {n["name"] for n in packet["candidate_nodes"]} == {"A", "B"}
    assert [e["id"] for e in packet["candidate_relations"]] == [eid]
    assert {n["name"] for n in packet["physical_facts"]} == {"a", "b", "objects"}
    assert packet["active_contracts"][0]["used_by"] == [eid]
    assert packet["consumers"] == []
    # Assessment remains broad even though the revision packet is narrow.
    full = assessment_view(ctx, IdentityDecision(), preview)
    assert len(full["candidate_nodes"]) == 3
    assert len(full["candidate_relations"]) > 1


def test_missing_or_deleted_review_reference_falls_back_to_full_context():
    ctx, preview = fixture()
    assert focused_revision(ctx, preview, IntegrationProposal(), []) is None
    assert (
        focused_revision(
            ctx, preview, IntegrationProposal(), [{"kind": "edge", "id": "missing"}]
        )
        is None
    )


def test_consumers_excludes_adjacency_but_keeps_content_and_group_refs():
    ctx, _preview = fixture()
    inspect = build_tools(ctx)[0]
    aid = ctx.input_node_map["A"]
    assert (
        inspect.invoke({"mode": "consumers", "view": "candidate", "ids": [aid]})[
            "items"
        ]
        == []
    )
    physical = ctx.input_node_map["a"]
    assert inspect.invoke(
        {"mode": "consumers", "view": "candidate", "ids": [physical]}
    )["items"] == [{"target": physical, "consumer": aid}]
    n = concept("reader", "x").model_copy(
        update={
            "properties": {
                "expression": {"refs": {"operand": "dep"}},
                "relation_metadata": [
                    {
                        "id": "group",
                        "edges": ["edge1", "edge2"],
                        "description": "group semantics",
                    }
                ],
            }
        }
    )
    assert content_consumers([n], ["dep", "edge1"]) == [
        {"target": "dep", "consumer": "reader"},
        {
            "consumer": "reader",
            "relation_metadata": n.properties["relation_metadata"][0],
        },
    ]


def test_contracts_tool_respects_input_and_candidate_views():
    ctx, preview = fixture()
    from cortex.schema.relation_contracts import effective_schema

    contract = next(
        c
        for c in effective_schema(ctx.schema, preview.state.graph).edge_types
        if c.id == "has_attribute"
    )
    local = contract.model_copy(update={"id": "local_attribute"})
    ctx.candidate = preview.state.model_copy(
        update={
            "graph": preview.state.graph.model_copy(
                update={"relation_contracts": (local,)}
            )
        }
    )
    inspect = build_tools(ctx)[0]
    assert (
        inspect.invoke(
            {"mode": "contracts", "ids": ["local_attribute"], "view": "input"}
        )["items"]
        == []
    )
    assert (
        inspect.invoke(
            {"mode": "contracts", "ids": ["local_attribute"], "view": "candidate"}
        )["items"][0]["id"]
        == "local_attribute"
    )


def test_rationale_coverage_is_required_without_claiming_semantic_truth():
    import pytest
    from pydantic import ValidationError

    from cortex.capabilities.graph_merge.integration.models import review_output_model

    model = review_output_model(["rule-a", "rule-b"])
    fields = {
        "verdict": "accept",
        "decision_check": "valid",
        "quality_check": "valid",
        "preservation_check": "valid",
    }
    with pytest.raises(ValidationError, match="exactly one"):
        model(**fields)
    checks = [
        {
            "node_id": key,
            "supported": True,
            "explanation": "Compared claim with supplied evidence.",
        }
        for key in ["rule-a", "rule-b"]
    ]
    assert model(**fields, rationale_checks=checks).verdict == "accept"
    checks[1]["supported"] = False
    with pytest.raises(ValidationError, match="unresolved"):
        model(**fields, rationale_checks=checks)
    fields["verdict"] = "revise"
    assert model(**fields, rationale_checks=checks).verdict == "revise"


def test_submission_retry_distinguishes_invalid_json_without_repairing_it():
    from langchain_core.messages import AIMessage

    from cortex.capabilities.graph_merge.llm.submission import submission_error

    message = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "IntegrationProposal",
                "args": '{"edits": []}}',
                "id": "bad",
                "error": "invalid",
            }
        ],
    )
    error = submission_error(message)
    assert "Extra data" in error and "column" in error
    assert message.invalid_tool_calls[0]["args"] == '{"edits": []}}'
    assert submission_error(AIMessage(content="plain answer")) == ""
