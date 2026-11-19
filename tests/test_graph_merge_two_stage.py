"""Small submissions preserve previous work; graph repair rechecks changed meaning."""

import json
from contextlib import nullcontext

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from test_graph_integration import catalog, concept, state

from cortex.capabilities.graph_merge.integration.compiler import mapped_base
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.models import (
    IdentityDecision,
    IntegrationProposal,
    IntegrationReview,
    StageCompletion,
)
from cortex.capabilities.graph_merge.integration.submission import ProposalBuffer
from cortex.capabilities.graph_merge.integration.two_stage import (
    TwoStageGraphIntegrationEngine,
)
from cortex.schema import load_root_schema


def test_failed_object_repair_keeps_unrelated_edits_and_requires_fresh_revision():
    ctx = IntegrationContext(
        state(),
        state([*catalog(), concept("A", "a"), concept("B", "b")]),
        load_root_schema(),
    )
    ctx.read_ids.update(ctx.nodes)
    buf = ProposalBuffer(ctx, IdentityDecision())
    a, b = [
        ctx.nodes[ctx.input_node_map[x]].model_dump(mode="json") for x in ["A", "B"]
    ]
    a["description"] = "Supported clarified meaning."
    b["properties"]["targets"] = [[]]
    changes = [
        {
            "before": [{"kind": "node", "id": n["id"]}],
            "after": [{"kind": "node", "id": n["id"]}],
            "explanation": "Clarify supported source meaning.",
        }
        for n in [a, b]
    ]
    r = buf.update(
        edits=[{"operation": "update_node", "node": n} for n in [a, b]], changes=changes
    )
    assert r["status"] == "needs_repair" and not buf.valid
    assert buf.context.candidate is None
    b["properties"]["targets"] = [
        [ctx.physical_map.get(ctx.input_node_map["b"], ctx.input_node_map["b"])]
    ]
    r = buf.update(
        edits=[{"operation": "update_node", "node": b}], expected_revision=r["revision"]
    )
    assert r["status"] == "validated" and buf.valid
    assert (
        next(e for e in buf.accepted.edits if e.node["id"] == a["id"]).node[
            "description"
        ]
        == a["description"]
    )
    assert len(buf.accepted.changes) == 2
    assert buf.update(expected_revision=0)["status"] == "stale_revision"


def test_nested_rule_errors_are_reported_before_graph_compilation():
    ctx = IntegrationContext(state(), state(catalog()), load_root_schema())
    buf = ProposalBuffer(ctx, IdentityDecision())
    node = {
        "id": "new_rule",
        "node_type": "generation_rule",
        "name": "Decision",
        "properties": {
            "condition": "Supported condition",
            "action": "Choose an evidenced implementation",
            "reason": "Original evidence",
            "operation_intents": ["column_binding_choice"],
            "triggers": {"question": {"phrases": ["request"], "sql": {"columns": []}}},
        },
    }
    r = buf.update(edits=[{"operation": "create_node", "node": node}])
    assert r["status"] == "needs_repair"
    assert "triggers.question" in r["errors"][0]["message"]
    assert buf.preview is None


def test_rewrite_source_guard_preserves_exact_references_without_auto_propagation():
    from cortex.capabilities.graph_merge.integration.compiler import Preview
    from cortex.capabilities.graph_merge.integration.provenance import (
        validate_rewrite_sources,
    )
    from cortex.schema import GraphSpace, GraphState, SourceReference

    source = SourceReference(source_id="record", locator="QA")
    original = concept("A", "a").model_copy(update={"sources": (source,)})
    ctx = IntegrationContext(state(), state([*catalog(), original]), load_root_schema())
    nodes, edges, nm, em = mapped_base(ctx, IdentityDecision())
    key = nm.get(ctx.input_node_map["A"], ctx.input_node_map["A"])
    proposal = IntegrationProposal.model_validate(
        {
            "changes": [
                {
                    "before": [{"kind": "node", "id": ctx.input_node_map["A"]}],
                    "after": [{"kind": "node", "id": key}],
                    "explanation": "Clarify the same source-backed meaning.",
                }
            ]
        }
    )
    nodes[key] = nodes[key].model_copy(
        update={"sources": (source.model_copy(update={"locator": "qa"}),)}
    )

    def preview():
        return Preview(
            GraphState(
                graph=GraphSpace(
                    nodes=tuple(nodes.values()), edges=tuple(edges.values())
                )
            ),
            nm,
            em,
            [],
        )

    with pytest.raises(ValueError, match="loses exact SourceReferences"):
        validate_rewrite_sources(ctx, proposal, preview())
    with pytest.raises(ValueError, match="Direct node mapping"):
        validate_rewrite_sources(ctx, IntegrationProposal(), preview())
    nodes[key] = nodes[key].model_copy(
        update={"sources": (*nodes[key].sources, source)}
    )
    validate_rewrite_sources(ctx, proposal, preview())


@pytest.mark.parametrize("change_content", [False, True])
@pytest.mark.parametrize("reject_initial", [False, True])
def test_two_stage_flow_and_targeted_reassessment(change_content, reject_initial):
    incoming = state([*catalog(), concept("A", "a"), concept("B", "a")])
    phases = []

    class Agent:
        def invoke(self, phase, ctx, payload):
            phases.append(phase)
            if phase == "identity":
                return IdentityDecision(
                    groups=[
                        {
                            "members": [
                                ctx.input_node_map["A"],
                                ctx.input_node_map["B"],
                            ],
                            "meaning": "Same object",
                            "reason": "Same evidenced referent",
                        }
                    ]
                )
            if phase == "node_assess":
                assert payload["reviewed_node_ids"]
                if reject_initial and phases.count("node_assess") == 2:
                    assert payload["previous_review_feedback"]["issues"] == [
                        "Clarify merged description."
                    ]
                if reject_initial and phases.count("node_assess") == 1:
                    return IntegrationReview(
                        verdict="revise",
                        decision_check="Same referent.",
                        quality_check="Clarification is needed.",
                        preservation_check="Sources retained.",
                        issues=["Clarify merged description."],
                        issue_refs=[
                            {"kind": "node", "id": payload["reviewed_node_ids"][0]}
                        ],
                    )
                return IntegrationReview(
                    verdict="accept",
                    decision_check="Same referent.",
                    quality_check="Supported node content.",
                    preservation_check="Complete source coverage.",
                )
            buf = ctx.proposal_buffer
            if phase == "consolidate":
                assert (
                    "incoming_seeds" not in payload and "candidate_ids" not in payload
                )
                assert {n["id"] for n in payload["nodes"]} == set(
                    payload["matched_groups"][0]["members"]
                )
            if phase == "consolidate" or change_content:
                n = next(
                    n for n in ctx.candidate.graph.nodes if n.node_type == "concept"
                ).model_dump(mode="json")
                n["description"] = (
                    "Consolidated meaning"
                    if phase == "consolidate"
                    else "Supported corrected meaning"
                )
                members = [ctx.input_node_map["A"], ctx.input_node_map["B"]]
                r = buf.update(
                    edits=[{"operation": "update_node", "node": n}],
                    changes=[
                        {
                            "before": [{"kind": "node", "id": k} for k in members],
                            "after": [{"kind": "node", "id": n["id"]}],
                            "explanation": "Preserve the complete shared source meaning.",
                        }
                    ],
                )
            else:
                r = buf.update()
            assert r["status"] == "validated", r
            return StageCompletion(
                verdict="complete", explanation="Checked actual result."
            )

    engine = TwoStageGraphIntegrationEngine(
        Agent(), checkpoint_factory=lambda: nullcontext(InMemorySaver())
    )
    result = engine.merge(
        state(), incoming, load_root_schema(), run_id="test-two-stage"
    )
    assert phases[:3] == ["identity", "consolidate", "node_assess"]
    assert phases.count("node_assess") == (2 if change_content else 1) + int(
        reject_initial
    )
    assert phases.count("consolidate") == 1 + int(reject_initial)
    if reject_initial:
        assert phases[:5] == [
            "identity",
            "consolidate",
            "node_assess",
            "consolidate",
            "node_assess",
        ]
    assert len([n for n in result.state.graph.nodes if n.node_type == "concept"]) == 1
    assert len([m for m in result.node_mappings if m.input_id in {"A", "B"}]) == 2


def test_call_trace_survives_agent_error(tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace
    from cortex.infrastructure.artifacts.graph_merge_trace import (
        FileSystemGraphMergeTraceSink,
    )

    path = tmp_path / "calls.jsonl"
    trace = EpisodeTrace(FileSystemGraphMergeTraceSink(path), "reconcile")
    trace.on_chat_model_start({}, [[HumanMessage(content="input")]], run_id="one")
    trace.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="reply",
                            usage_metadata={
                                "input_tokens": 3,
                                "output_tokens": 2,
                                "total_tokens": 5,
                            },
                        )
                    )
                ]
            ]
        ),
        run_id="one",
    )
    trace.on_chat_model_start({}, [[HumanMessage(content="repair")]], run_id="two")
    trace.on_llm_error(RuntimeError("upstream failure"), run_id="two")
    events = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["event_type"] for x in events] == [
        "model_start",
        "model_end",
        "model_start",
        "model_error",
    ]
    assert events[1]["message"]["usage_metadata"]["total_tokens"] == 5
    assert trace.calls == 2


def test_submit_tool_schema_requires_operation_and_retains_pending_repair():
    from cortex.capabilities.graph_merge.integration.tools import build_tools

    ctx = IntegrationContext(
        state(), state([*catalog(), concept("A", "a")]), load_root_schema()
    )
    ctx.read_ids.update(ctx.nodes)
    ctx.proposal_buffer = ProposalBuffer(ctx, IdentityDecision())
    tool = next(t for t in build_tools(ctx) if t.name == "submit_edits")
    schema = tool.args_schema.model_json_schema()
    assert "operation" in schema["$defs"]["NodeWrite"]["required"]
    assert "node_id" not in schema["$defs"]["NodeWrite"]["properties"]
    a = ctx.nodes[ctx.input_node_map["A"]].model_dump(mode="json")
    a["description"] = "Supported clarification"
    r = tool.invoke({"edits": [{"operation": "update_node", "node": a}]})
    assert r["status"] == "needs_repair"  # Missing correspondence, not lost edits.
    r = tool.invoke(
        {
            "changes": [
                {
                    "before": [{"kind": "node", "id": a["id"]}],
                    "after": [{"kind": "node", "id": a["id"]}],
                    "explanation": "Clarify unchanged business identity.",
                }
            ]
        }
    )
    assert r["status"] == "validated" and len(ctx.proposal_buffer.accepted.edits) == 1


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "base_url", ["https://api.deepseek.com/v1", "https://compatible.example/v1"]
)
def test_thinking_switch_uses_provider_supported_parameter(
    monkeypatch, enabled, base_url
):
    from cortex.config.models import LLMConfig
    from cortex.infrastructure.llm import providers

    monkeypatch.setattr(providers, "ChatOpenAI", lambda **kw: kw)
    monkeypatch.setattr(providers, "DeepSeekChatOpenAI", lambda **kw: kw)
    model = providers.create_chat_model(
        LLMConfig(
            model="configured-model",
            base_url=base_url,
            api_key="test-only",
            enable_thinking=enabled,
        )
    )
    assert model["extra_body"] == (
        {"thinking": {"type": "enabled" if enabled else "disabled"}}
        if "api.deepseek.com" in base_url
        else {"enable_thinking": enabled}
    )


def test_read_budget_blocks_execution_not_just_advertisement():
    from test_agent_runtime import ScriptModel, call, message

    from cortex.capabilities.graph_merge.llm.integration import IntegrationAgent

    ctx = IntegrationContext(
        state(), state([*catalog(), concept("A", "a")]), load_root_schema()
    )
    ctx.proposal_buffer = ProposalBuffer(ctx, IdentityDecision())
    model = ScriptModel(
        [
            *[
                message(call("read_guidance", {"topic": "concept"}, f"r{i}"))
                for i in range(5)
            ],
            message(
                call(
                    "StageCompletion",
                    {
                        "verdict": "failed",
                        "explanation": "Specific missing evidence remains.",
                    },
                    "finish",
                )
            ),
        ]
    )
    result = IntegrationAgent(model).invoke("consolidate", ctx, {"nodes": []})
    assert result.verdict == "failed"
    assert len([x for x in ctx.tool_log if x["tool"] == "read_guidance"]) == 4
    assert "Read budget exhausted" in str(model._calls[-1][-1].content)


def test_pending_invalid_contract_can_be_retracted_without_losing_edits():
    ctx = IntegrationContext(
        state(), state([*catalog(), concept("A", "a")]), load_root_schema()
    )
    ctx.read_ids.update(ctx.nodes)
    # Root contracts are immutable, including has_attribute.
    buf = ProposalBuffer(ctx, IdentityDecision())
    a = ctx.nodes[ctx.input_node_map["A"]].model_dump(mode="json")
    a["description"] = "Clarification"
    change = {
        "before": [{"kind": "node", "id": a["id"]}],
        "after": [{"kind": "node", "id": a["id"]}],
        "explanation": "Preserve source meaning.",
    }
    contract = (
        load_root_schema()
        .edge_type_index()["has_attribute"]
        .model_copy(update={"description": "Changed meaning"})
        .model_dump(mode="json")
    )
    r = buf.update(
        edits=[{"operation": "update_node", "node": a}],
        changes=[change],
        relation_contracts=[contract],
    )
    assert r["status"] == "needs_repair"
    r = buf.update(remove_contracts=["has_attribute"])
    assert r["status"] == "validated" and len(buf.accepted.edits) == 1
    assert (
        load_root_schema().edge_type_index()["has_attribute"].description
        != "Changed meaning"
    )


def test_bad_correspondence_can_be_removed_and_replaced():
    ctx = IntegrationContext(
        state(), state([*catalog(), concept("A", "a")]), load_root_schema()
    )
    ctx.read_ids.update(ctx.nodes)
    buf = ProposalBuffer(ctx, IdentityDecision())
    bad = {
        "before": [{"kind": "node", "id": "wrong_source"}],
        "after": [],
        "explanation": "Invalid input reference.",
    }
    assert buf.update(changes=[bad])["status"] == "needs_repair"
    r = buf.update(remove_changes=[{"kind": "node", "id": "wrong_source"}])
    assert r["status"] == "validated" and not buf.accepted.changes


def test_selection_signal_uses_sql_dialect_and_preserves_implicit_source_choice():
    from cortex.capabilities.graph_merge.integration.compiler import compile_proposal
    from cortex.capabilities.graph_merge.integration.signals import inspection_signals
    from cortex.capabilities.graph_merge.models import EvidenceItem
    from cortex.schema import SourceReference

    incoming = state([*catalog(), concept("A", "a"), concept("B", "a")])
    evidence = (
        EvidenceItem(
            id="first",
            case_id="first",
            source=SourceReference(source_id="A"),
            content={"sql": "SELECT `a` FROM `objects` ORDER BY `b`"},
        ),
        EvidenceItem(
            id="second",
            case_id="second",
            source=SourceReference(source_id="B"),
            content={"sql": "SELECT a, b FROM objects"},
        ),
    )
    ctx = IntegrationContext(state(), incoming, load_root_schema(), evidence=evidence)
    ctx.sql_dialect = "sqlite"
    # The second representation is complementary; source SELECT choices differ.
    ctx.raw_nodes[ctx.input_node_map["B"]] = ctx.raw_nodes[
        ctx.input_node_map["B"]
    ].model_copy(
        update={
            "properties": {
                "targets": [[ctx.input_node_map["a"]], [ctx.input_node_map["b"]]]
            }
        }
    )
    decision = IdentityDecision(
        groups=[
            {
                "members": [ctx.input_node_map["A"], ctx.input_node_map["B"]],
                "meaning": "Same",
                "reason": "same role",
            }
        ]
    )
    preview = compile_proposal(ctx, decision, IntegrationProposal(), strict=False)
    signals = inspection_signals(ctx, preview)
    choices = next(s for s in signals if s["kind"] == "source_selection_variants")
    assert len(choices["variants"]) == 2
    assert len(choices["variants"][0]["selected_physical_inputs"]) == 1
    assert len(choices["variants"][1]["selected_physical_inputs"]) == 2

    # Lineage lookup has identical selection semantics but is performed only
    # once for each current Concept, independent of evidence/source count.
    from collections import Counter
    from cortex.capabilities.graph_merge.integration.lineage import LineageIndex

    lineage = LineageIndex(ctx, preview.node_map, preview.edge_map)
    lookups = Counter()
    original_lookup = lineage.origin_refs

    def counted_lookup(kind, key):
        lookups[kind, key] += 1
        return original_lookup(kind, key)

    lineage.origin_refs = counted_lookup
    assert inspection_signals(ctx, preview, origins=lineage) == signals
    assert lookups
    assert max(lookups.values()) == 1
    assert len(lookups) == sum(n.node_type == "concept" for n in preview.state.graph.nodes)


def test_supported_reason_cannot_also_declare_unsupported_claims():
    from cortex.capabilities.graph_merge.integration.models import RationaleCheck

    with pytest.raises(ValueError, match="unsupported"):
        RationaleCheck(
            node_id="R",
            supported=True,
            unsupported_claims=["No replay record supplied"],
            explanation="Gold supports action only.",
        )


def test_conflict_tool_factors_actual_actions_and_preserves_sources():
    from cortex.capabilities.graph_merge.integration.tools import build_tools
    from cortex.schema import GraphNode, SourceReference

    def rule(key, action):
        return GraphNode(
            id=key,
            node_type="generation_rule",
            name="Choose representation",
            sources=(SourceReference(source_id=key),),
            properties={
                "operation_intents": ["column_binding_choice"],
                "condition": "Requested value has two supported representations.",
                "action": action,
                "reason": "Source chooses this representation.",
                "triggers": {
                    "question": {"phrases": ["value"]},
                    "sql": {"columns": [], "operations": []},
                },
            },
        )

    ctx = IntegrationContext(
        state(),
        state(
            [
                *catalog(),
                rule("L", "Choose left input"),
                rule("R", "Choose right input"),
            ]
        ),
        load_root_schema(),
    )
    ctx.read_ids.update(ctx.nodes)
    ctx.proposal_buffer = ProposalBuffer(ctx, IdentityDecision())
    ns, es, _nm, _em = mapped_base(ctx, IdentityDecision())
    from cortex.schema import GraphSpace, GraphState

    ctx.candidate = GraphState(
        graph=GraphSpace(nodes=tuple(ns.values()), edges=tuple(es.values()))
    )
    left, right = [ctx.nodes[ctx.input_node_map[k]] for k in ["L", "R"]]
    alts = [
        {
            "alternative_id": k,
            "content": {
                f: getattr(n, f)
                for f in ["name", "description", "aliases", "tags", "properties"]
            },
            "sources": [s.model_dump(mode="json") for s in n.sources],
        }
        for k, n in [("left", left), ("right", right)]
    ]
    tool = next(t for t in build_tools(ctx) if t.name == "write_conflict")
    r = tool.invoke(
        {
            "node_id": left.id,
            "alternatives": alts,
            "conflict_points": [
                {
                    "point_id": "choice",
                    "aspect": "representation choice",
                    "description": "Overlapping requests require different inputs.",
                    "choices": [
                        {"statement": "Use left", "alternative_ids": ["left"]},
                        {"statement": "Use right", "alternative_ids": ["right"]},
                    ],
                }
            ],
            "edits": [{"operation": "delete_node", "node_id": right.id}],
            "changes": [
                {
                    "before": [
                        {"kind": "node", "id": left.id},
                        {"kind": "node", "id": right.id},
                    ],
                    "after": [{"kind": "node", "id": left.id}],
                    "explanation": "Preserve both complete competing source choices.",
                }
            ],
        }
    )
    assert r["status"] == "validated", r
    node = next(n for n in ctx.candidate.graph.nodes if n.id == left.id)
    assert {a.overrides["properties.action"] for a in node.alternatives} == {
        "Choose left input",
        "Choose right input",
    }
    assert len(node.sources) == 2


def test_reasoning_survives_response_and_following_tool_turn():
    from langchain_core.messages import HumanMessage, ToolMessage

    from cortex.infrastructure.llm.deepseek import DeepSeekChatOpenAI

    model = DeepSeekChatOpenAI(
        model="configured-model",
        api_key="test-only",
        base_url="https://api.deepseek.com",
    )
    response = {
        "model": "configured-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "External model reasoning",
                    "tool_calls": [
                        {
                            "id": "call1",
                            "type": "function",
                            "function": {"name": "inspect_graph", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    ai = model._create_chat_result(response).generations[0].message
    payload = model._get_request_payload(
        [
            HumanMessage(content="review"),
            ai,
            ToolMessage(content="facts", tool_call_id="call1"),
        ]
    )
    assert payload["messages"][1]["reasoning_content"] == "External model reasoning"
    assert "reasoning_content" not in payload["messages"][0]


@pytest.mark.parametrize("enabled", [False, True])
def test_deepseek_thinking_protocol_survives_proxy_host(monkeypatch, enabled):
    from cortex.config.models import LLMConfig
    from cortex.infrastructure.llm import providers

    monkeypatch.setattr(providers, "ChatOpenAI", lambda **kw: kw)
    monkeypatch.setattr(providers, "DeepSeekChatOpenAI", lambda **kw: kw)
    model = providers.create_chat_model(LLMConfig(
        model="deepseek-v4-pro", base_url="https://proxy.example/v1", api_key="test-only",
        enable_thinking=enabled, reasoning_effort="high",
    ))
    assert model["extra_body"] == {"thinking": {"type": "enabled" if enabled else "disabled"}}
    assert model["reasoning_effort"] == ("high" if enabled else None)
