"""Controlled cost reductions must retain graph content, provenance and review scope."""

import json

import pytest
from test_graph_integration import Agent, catalog, concept, contexts, edge, state
from test_graph_integration_local_context import event, evidence

from cortex.capabilities.graph_merge.integration.assessment import assessment_view
from cortex.capabilities.graph_merge.graph.publication import compile_proposal
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.integration.engine import GraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.edit_contract import (
    IdentityDecision,
    IntegrationProposal,
    IntegrationReview,
)
from cortex.capabilities.graph_merge.llm.packing import (
    bounded_items,
    expand_payload,
    pack_payload,
)
from cortex.capabilities.graph_merge.integration.revision import compose_revision
from cortex.capabilities.graph_merge.identity.tools import build_tools
from cortex.schema import load_root_schema


def update(node, description):
    return {
        "operation": "update_node",
        "node": node.model_copy(update={"description": description}).model_dump(
            mode="json"
        ),
    }


def disposition(key, *, after=None):
    return {
        "before": [{"kind": "node", "id": key}],
        "after": [{"kind": "node", "id": key if after is None else after}],
        "explanation": "Preserve the evidenced referent with a clearer description.",
    }


def test_shared_payload_is_lossless_with_changed_and_deleted_objects():
    ctx = contexts(
        state(),
        state(
            [*catalog(), concept("A", "a"), concept("B", "b")], [edge("g", "A", "a")]
        ),
    )
    decision = IdentityDecision()
    preview = compile_proposal(ctx, decision, IntegrationProposal())
    payload = {**ctx.payload("assess"), **assessment_view(ctx, decision, preview)}
    packed = pack_payload(payload)
    assert all(set(n) == {"input_ref"} for n in packed["candidate_nodes"])
    assert expand_payload(packed) == payload
    assert len(json.dumps(packed)) < len(json.dumps(payload))
    a, b = ctx.input_node_map["A"], ctx.input_node_map["B"]
    change = disposition(b)
    change["after"] = []
    proposal = IntegrationProposal(
        edits=[
            update(ctx.nodes[a], "new description"),
            {"operation": "delete_node", "node_id": b},
        ],
        changes=[disposition(a), change],
    )
    preview = compile_proposal(ctx, decision, proposal)
    payload = {**ctx.payload("assess"), **assessment_view(ctx, decision, preview)}
    packed = pack_payload(payload)
    assert {"input_ref": a, "overrides": {"description": "new description"}} in packed[
        "candidate_nodes"
    ]
    assert packed["deleted_nodes"] == [{"input_ref": b}]
    assert expand_payload(packed) == payload


def test_preloaded_evidence_is_local_and_large_items_remain_readable():
    incoming = state([event("local")])
    huge = evidence("local").model_copy(update={"content": {"sql": "x" * 13000}})
    ctx = IntegrationContext(
        state([event("remote")]),
        incoming,
        load_root_schema(),
        evidence=[huge, evidence("local")],
        top_k=1,
    )
    payload = ctx.payload("propose")
    assert payload["evidence"] == [ctx.evidence_rows()[1]]
    full = {tool.name: tool for tool in build_tools(ctx)}['read_evidence'].invoke({"ids": ["local"]})
    assert len(full["items"]) == 2
    assert bounded_items([{"x": "x" * 100}, {"x": "small"}], budget=30) == [
        {"x": "small"}
    ]


def test_direct_binding_facts_are_preloaded_without_entire_catalog():
    ctx = contexts(state(), state([*catalog(), concept("A", "a")]))
    payload = ctx.payload("propose")
    assert {n["id"] for n in payload["physical_facts"]} == {ctx.input_node_map["a"]}
    assert build_tools(ctx)[0].invoke(
        {"mode": "nodes", "ids": [ctx.input_node_map["b"]]}
    )["items"]


def test_batched_candidates_equal_independent_queries():
    ctx = contexts(state(), state([*catalog(), concept("A", "a"), concept("B", "b")]))
    ids = [ctx.input_node_map[x] for x in ("A", "B")]
    inspect = build_tools(ctx, visible_source_ids=ctx.read_ids)[0]
    singles = {key: inspect.invoke({"mode": "candidates", "ids": [key]}) for key in ids}
    assert inspect.invoke({"mode": "candidates", "ids": ids}) == {"pages": singles}


def test_size_pagination_preserves_every_edge_and_batches_small_results():
    nodes = [
        *catalog(),
        concept("root", "a"),
        *[concept(f"c{i}", "b") for i in range(30)],
    ]
    edges = [edge(f"e{i}", "root", f"c{i}", "has_attribute") for i in range(30)]
    ctx = contexts(state(), state(nodes, edges))
    inspect = build_tools(ctx)[0]
    assert (
        len(
            inspect.invoke({"mode": "relations", "ids": [ctx.input_node_map["root"]]})[
                "items"
            ]
        )
        == 30
    )
    edges = [
        e.model_copy(update={"properties": {"description": "Evidence " * 600}})
        for e in edges
    ]
    ctx = contexts(state(), state(nodes, edges))
    inspect = build_tools(ctx)[0]
    offset, found = 0, []
    while offset is not None:
        page = inspect.invoke(
            {"mode": "relations", "ids": [ctx.input_node_map["root"]], "offset": offset}
        )
        found.extend(e["id"] for e in page["items"])
        offset = page["next_offset"]
    assert len(found) == len(set(found)) == 30


def test_revision_preserves_other_edits_sources_and_is_identical_to_full_proposal():
    ctx = contexts(state(), state([*catalog(), concept("A", "a"), concept("B", "b")]))
    a, b = [ctx.input_node_map[x] for x in ("A", "B")]
    first = IntegrationProposal(
        edits=[
            update(ctx.nodes[a], "A version one"),
            update(ctx.nodes[b], "B retained"),
        ],
        changes=[disposition(a), disposition(b)],
    )
    preview = compile_proposal(ctx, IdentityDecision(), first)
    current = next(n for n in preview.state.graph.nodes if n.id == a)
    write = update(current, "A version two")
    del write["node"]["sources"]
    patch = IntegrationProposal(edits=[write], changes=[disposition(a)])
    composed = compose_revision(ctx, IdentityDecision(), first, preview, patch)
    full = IntegrationProposal(
        edits=[
            update(ctx.nodes[a], "A version two"),
            update(ctx.nodes[b], "B retained"),
        ],
        changes=[disposition(b), disposition(a)],
    )
    result = compile_proposal(ctx, IdentityDecision(), composed)
    expected = compile_proposal(ctx, IdentityDecision(), full)
    assert {n.id: n for n in result.state.graph.nodes} == {
        n.id: n for n in expected.state.graph.nodes
    }
    assert result.node_map == expected.node_map and result.edge_map == expected.edge_map
    assert composed.changes == full.changes
    assert len(patch.model_dump_json()) < len(full.model_dump_json())


def test_revision_created_node_can_update_then_delete_without_replay():
    ctx = contexts(state(), state([event("original")]))
    fresh = event("new").model_copy(update={"sources": ()})
    first = IntegrationProposal(
        edits=[{"operation": "create_node", "node": fresh.model_dump(mode="json")}]
    )
    preview = compile_proposal(ctx, IdentityDecision(), first)
    patch = IntegrationProposal(edits=[update(fresh, "refined")])
    second = compose_revision(ctx, IdentityDecision(), first, preview, patch)
    assert second.edits[0].operation == "create_node"
    preview = compile_proposal(ctx, IdentityDecision(), second)
    deleted = compose_revision(
        ctx,
        IdentityDecision(),
        second,
        preview,
        IntegrationProposal(edits=[{"operation": "delete_node", "node_id": "new"}]),
    )
    assert not deleted.edits
    compile_proposal(ctx, IdentityDecision(), deleted)
    with pytest.raises(ValueError, match="fresh ID"):
        compose_revision(
            ctx,
            IdentityDecision(),
            first,
            compile_proposal(ctx, IdentityDecision(), first),
            first,
        )


def test_revision_rejects_partial_correspondence_group_replacement():
    ctx = contexts(state(), state([event("A"), event("B")]))
    a, b = [ctx.input_node_map[x] for x in ("A", "B")]
    first = IntegrationProposal(
        changes=[
            {
                "before": [{"kind": "node", "id": a}, {"kind": "node", "id": b}],
                "after": [{"kind": "node", "id": a}, {"kind": "node", "id": b}],
                "explanation": "Joint correspondence",
            }
        ]
    )
    preview = compile_proposal(ctx, IdentityDecision(), first)
    with pytest.raises(ValueError, match="all before"):
        compose_revision(
            ctx,
            IdentityDecision(),
            first,
            preview,
            IntegrationProposal(changes=[disposition(a)]),
        )


def test_engine_revisions_show_valid_candidate_and_failed_patch_does_not_advance_it():
    class RevisionAgent(Agent):
        def invoke(self, phase, context, payload):
            self.phases.append(phase)
            if phase == "identity":
                return IdentityDecision()
            key = context.input_node_map["original"]
            if phase == "propose":
                count = self.phases.count("propose")
                current = next(n for n in context.candidate.graph.nodes if n.id == key)
                if count == 1:
                    assert payload["proposal_mode"] == "base"
                    return IntegrationProposal(
                        edits=[update(current, "valid first")],
                        changes=[disposition(key)],
                    )
                assert payload["proposal_mode"] == "delta"
                assert current.description == "valid first"
                assert "previous_proposal" not in payload
                if count == 2:
                    wrong = update(current, "must not persist")
                    wrong["node"]["properties"] = {"invalid_field": True}
                    return IntegrationProposal(
                        edits=[wrong], changes=[disposition(key)]
                    )
                assert payload["failed_patch"]["edits"]
                return IntegrationProposal(
                    edits=[update(current, "valid final")], changes=[disposition(key)]
                )
            count = self.phases.count("assess")
            if count == 2:
                assert payload["previous_review"]["verdict"] == "revise"
                assert payload["changes_since_review"]["nodes"]["updated"] == [key]
            return IntegrationReview(
                verdict="revise" if count == 1 else "accept",
                decision_check="checked",
                quality_check="checked",
                preservation_check="checked",
                issues=["Refine description"] if count == 1 else [],
            )

    agent = RevisionAgent()
    result = GraphIntegrationEngine(agent, max_rounds=3).merge(
        state(), state([event("original")]), load_root_schema()
    )
    assert (
        next(n for n in result.state.graph.nodes if n.id == "original").description
        == "valid final"
    )
    assert agent.phases == [
        "identity",
        "propose",
        "assess",
        "propose",
        "propose",
        "assess",
    ]


def test_identity_revisit_discards_previous_candidate_and_review_focus():
    class RevisitAgent(Agent):
        def invoke(self, phase, context, payload):
            self.phases.append(phase)
            if phase == "identity":
                return IdentityDecision()
            key = context.input_node_map["original"]
            if phase == "propose":
                assert payload["proposal_mode"] == "base"
                assert "prior_changes" not in payload
                node = next(n for n in context.candidate.graph.nodes if n.id == key)
                assert node.description is None
                return IntegrationProposal(
                    edits=[update(node, f"round {self.phases.count('propose')}")],
                    changes=[disposition(key)],
                )
            assert "previous_review" not in payload
            revisit = self.phases.count("assess") == 1
            return IntegrationReview(
                verdict="revisit_identity" if revisit else "accept",
                decision_check="Reconsider identity" if revisit else "checked",
                quality_check="checked",
                preservation_check="checked",
                issues=["Reconsider source identities"] if revisit else [],
            )

    agent = RevisitAgent()
    result = GraphIntegrationEngine(agent).merge(
        state(), state([event("original")]), load_root_schema()
    )
    assert (
        next(n for n in result.state.graph.nodes if n.id == "original").description
        == "round 2"
    )
    assert agent.phases.count("identity") == 2


def test_revision_cannot_silently_remove_a_rewrite_destination():
    ctx = contexts(state(), state([event("old")]))
    old = ctx.input_node_map["old"]
    fresh = event("fresh").model_copy(update={"sources": ctx.nodes[old].sources})
    initial = IntegrationProposal(
        edits=[
            {"operation": "delete_node", "node_id": old},
            {"operation": "create_node", "node": fresh.model_dump(mode="json")},
        ],
        changes=[disposition(old, after="fresh")],
    )
    preview = compile_proposal(ctx, IdentityDecision(), initial)
    patch = IntegrationProposal(
        edits=[{"operation": "delete_node", "node_id": "fresh"}]
    )
    cumulative = compose_revision(ctx, IdentityDecision(), initial, preview, patch)
    with pytest.raises(ValueError, match="Unknown after handle"):
        compile_proposal(ctx, IdentityDecision(), cumulative)
    deletion = disposition(old)
    deletion["after"] = []
    repaired = compose_revision(
        ctx,
        IdentityDecision(),
        initial,
        preview,
        patch.model_copy(
            update={"changes": (type(initial.changes[0]).model_validate(deletion),)}
        ),
    )
    assert not compile_proposal(ctx, IdentityDecision(), repaired).state.graph.nodes


def test_revision_cannot_retype_previously_created_node_in_place():
    ctx = contexts(state(), state([*catalog(), concept("A", "a")]))
    first = IntegrationProposal(
        edits=[
            {
                "operation": "create_node",
                "node": event("new")
                .model_copy(update={"sources": ()})
                .model_dump(mode="json"),
            }
        ]
    )
    preview = compile_proposal(ctx, IdentityDecision(), first)
    wrong = concept("new", ctx.input_node_map["a"]).model_copy(update={"sources": ()})
    with pytest.raises(ValueError, match="node type cannot change"):
        compose_revision(
            ctx,
            IdentityDecision(),
            first,
            preview,
            IntegrationProposal(
                edits=[
                    {"operation": "update_node", "node": wrong.model_dump(mode="json")}
                ]
            ),
        )


def test_llm_wrapper_shares_input_and_keeps_structured_submission_recovery():
    from langchain_core.messages import AIMessage
    from test_agent_runtime import ScriptModel, call, message

    from cortex.capabilities.graph_merge.llm.integration import IntegrationAgent

    ctx = contexts(state(), state([event("original")]))
    preview = compile_proposal(ctx, IdentityDecision(), IntegrationProposal())
    payload = {
        **ctx.payload("assess"),
        **assessment_view(ctx, IdentityDecision(), preview),
    }
    model = ScriptModel(
        [
            AIMessage(content="Draft review"),
            message(
                call(
                    "IntegrationReview",
                    {
                        "verdict": "accept",
                        "decision_check": "checked",
                        "quality_check": "checked",
                        "preservation_check": "checked",
                        "issues": [],
                    },
                )
            ),
        ]
    )
    review = IntegrationAgent(model).invoke("assess", ctx, payload)
    assert review.verdict == "accept"
    actual = json.loads(model._calls[0][1].content)
    assert expand_payload(actual) == payload
    assert actual["candidate_nodes"] == [{"input_ref": ctx.input_node_map["original"]}]
    assert "## Submission" in model._calls[0][0].content
    assert "**IntegrationReview**" in model._calls[0][0].content
    assert "{output_name}" not in model._calls[0][0].content
    assert model._calls[1][-1].content.startswith("## Structured submission required")


def test_artifact_audit_replays_incremental_traces_and_shared_payloads(tmp_path):
    from scripts.audit_graph_integration_review import audit

    class RecordedAgent(Agent):
        def __init__(self):
            super().__init__()
            self.traces = []

        def invoke(self, phase, context, payload):
            self.phases.append(phase)
            if phase == "identity":
                reply = IdentityDecision()
            elif phase == "propose":
                key = context.input_node_map["original"]
                node = next(n for n in context.candidate.graph.nodes if n.id == key)
                reply = IntegrationProposal(
                    edits=[update(node, f"round {self.phases.count('propose')}")],
                    changes=[disposition(key)],
                )
            else:
                first = self.phases.count("assess") == 1
                reply = IntegrationReview(
                    verdict="revise" if first else "accept",
                    decision_check="checked",
                    quality_check="checked",
                    preservation_check="checked",
                    issues=["Clarify"] if first else [],
                )
            self.traces.append(
                {
                    "component": "integration_" + phase,
                    "payload": pack_payload(payload),
                    "parsed_response": reply.model_dump(mode="json"),
                }
            )
            return reply

    old, incoming, schema = state(), state([event("original")]), load_root_schema()
    agent = RecordedAgent()
    result = GraphIntegrationEngine(agent).merge(old, incoming, schema)
    (tmp_path / "workflow.json").write_text(
        json.dumps(
            {
                "request": {
                    "existing": old.model_dump(mode="json"),
                    "incoming": incoming.model_dump(mode="json"),
                    "schema": schema.model_dump(mode="json"),
                }
            }
        )
    )
    (tmp_path / "merge_report.json").write_text(result.model_dump_json())
    (tmp_path / "graph_state.json").write_text(result.state.model_dump_json())
    (tmp_path / "agent_traces.jsonl").write_text(
        "\n".join(json.dumps(t) for t in agent.traces)
    )
    audit(tmp_path)
    report = json.loads((tmp_path / "integrity_audit.json").read_text())
    assert report["status"] == "passed"
    assert len(report["assessments"]) == 2


def test_corrected_identity_error_is_not_replayed_as_proposal_feedback():
    class CorrectingAgent(Agent):
        def invoke(self, phase, context, payload):
            if phase == "identity":
                self.phases.append(phase)
                a, b = [context.input_node_map[k] for k in ("A", "B")]
                if self.phases.count("identity") == 1:
                    return IdentityDecision(
                        groups=[
                            {
                                "members": [a, b],
                                "meaning": "Same occurrence",
                                "reason": "Fixture identity",
                            }
                        ],
                        distinctions=[
                            {
                                "members": [a, b],
                                "reason": "Invalid contradictory decision",
                            }
                        ],
                    )
                assert "both same and different" in payload["feedback"][0]
                return IdentityDecision()
            if phase == "propose":
                assert payload["feedback"] == []
            return super().invoke(phase, context, payload)

    agent = CorrectingAgent()
    GraphIntegrationEngine(agent).merge(
        state(), state([event("A"), event("B")]), load_root_schema()
    )
    assert agent.phases == ["identity", "identity", "propose", "assess"]
