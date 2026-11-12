"""Integration tests use actual LangChain agents and file-backed SQLite checkpoints."""

from __future__ import annotations

import json

import pytest
from conftest import make_rule
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from test_agent_runtime import ScriptModel, call, message

from cortex.capabilities.graph_merge.identity import IdentityGrouper, node_content
from cortex.capabilities.graph_merge.llm import (
    LLMConsolidationChecker,
    LLMGroupConsolidator,
)
from cortex.capabilities.graph_merge.models import (
    EvidenceItem,
    FinalNodeGroup,
    IdentityGroupingResult,
    NodeRepresentation,
    RepresentationRef,
)
from cortex.capabilities.graph_merge.operator import GraphMergeOperator
from cortex.capabilities.graph_merge.reconciliation.models import (
    ConsolidationAnalysis,
    ConsolidationCandidate,
    ConsolidationProposal,
    ConsolidationReview,
    SourceCoverage,
)
from cortex.capabilities.graph_merge.reconciliation.service import NodeGroupReconciler
from cortex.capabilities.graph_merge.workflow.context import GroupContext
from cortex.capabilities.graph_merge.workflow.group import build_group_workflow
from cortex.schema import (
    GraphNode,
    GraphSpace,
    GraphState,
    SourceReference,
    load_root_schema,
)


def inputs():
    nodes, members, evidence = [], [], []
    for key, action in (
        ("a", "Count requested records."),
        ("b", "Count the required rows."),
    ):
        asset = make_rule(key, action)
        node = GraphNode(
            id=asset.id,
            node_type="generation_rule",
            name=asset.name,
            properties={
                **asset.properties.model_dump(mode="json"),
                "operation_intents": ["count"],
            },
            sources=(SourceReference(source_id=key, case_id=key),),
        )
        ref = RepresentationRef(origin="incoming_node", ref_id=node.id)
        nodes.append(node)
        members.append(
            NodeRepresentation(
                ref=ref,
                node_type=node.node_type,
                content=node_content(node),
                sources=node.sources,
            )
        )
        evidence.append(
            EvidenceItem(
                id=f"source:{key}",
                source=node.sources[0],
                case_id=key,
                content={
                    "question": "How many records?",
                    "sql": "SELECT COUNT(*) FROM orders",
                },
            )
        )
    group = FinalNodeGroup(
        group_id="same-count",
        node_type="generation_rule",
        existing_node_id=None,
        members=tuple(members),
    )
    context = GroupContext(
        group=group,
        schema_definition=load_root_schema(),
        evidence=tuple(evidence),
        nodes=tuple(nodes),
    )
    proposal = ConsolidationProposal(
        analysis=ConsolidationAnalysis(shared=("Count the requested records",)),
        candidates=(
            ConsolidationCandidate(
                content=members[0].content,
                supporting_refs=tuple(m.ref for m in members),
            ),
        ),
    )
    review = ConsolidationReview(
        generalization={"verdict": "acceptable", "reason": "Same counting behavior"},
        coverage_by_source=tuple(
            SourceCoverage(
                supporting_ref=m.ref,
                evidence_id=e.id,
                verdict="covered",
                reason="Count and dependencies retained",
            )
            for m, e in zip(members, evidence, strict=True)
        ),
    )
    return context, proposal, review


def response(value):
    return message(call(type(value).__name__, value.model_dump(mode="json")))


def build(model, saver=None, max_calls=18):
    return build_group_workflow(
        LLMGroupConsolidator(model),
        LLMConsolidationChecker(model),
        checkpointer=saver,
        max_calls=max_calls,
    )


def test_agents_read_tools_and_return_source_assessment():
    context, proposal, review = inputs()
    model = ScriptModel(
        [
            message(call("read_evidence", {"ids": ["a"]})),
            response(proposal),
            message(call("read_nodes", {"ids": [context.nodes[0].id]})),
            response(review),
        ]
    )
    output = build(model).invoke({"context": context.model_dump(mode="json")})
    assert output["accepted"] and output["model_calls"] == 4
    assert "conflict" not in output["result"]
    assert not output["result"]["node"].get("alternatives")
    assert len(output["result"]["node"]["sources"]) == 2
    assert len(model._calls) == 4
    assessor_context = json.loads(
        model._calls[2][1]
        .content.split("<merge_context>\n")[1]
        .split("\n</merge_context>")[0]
    )
    assert "revision" not in assessor_context
    assert len(assessor_context["source_checklist"]) == 2


def test_semantic_feedback_revises_analysis_and_candidate():
    context, proposal, review = inputs()
    bad_review = review.model_copy(
        update={
            "generalization": review.generalization.model_copy(
                update={"verdict": "revise", "reason": "Clarify the counting scope"}
            )
        }
    )
    revised = proposal.model_copy(
        update={
            "analysis": ConsolidationAnalysis(
                shared=("Count records in the requested scope",)
            )
        }
    )
    model = ScriptModel(
        [response(proposal), response(bad_review), response(revised), response(review)]
    )
    output = build(model).invoke({"context": context.model_dump(mode="json")})
    assert output["accepted"] and output["round"] == 2
    assert "Clarify the counting scope" in model._calls[2][1].content
    assert "Clarify the counting scope" not in model._calls[3][1].content
    assert output["proposal"]["analysis"] == revised.analysis.model_dump(mode="json")


def test_malformed_assessment_retries_only_assessment():
    context, proposal, review = inputs()
    invalid = review.model_copy(
        update={"coverage_by_source": review.coverage_by_source[:1]}
    )
    model = ScriptModel([response(proposal), response(invalid), response(review)])
    output = build(model).invoke({"context": context.model_dump(mode="json")})
    assert output["accepted"] and output["round"] == 1
    assert len(model._calls) == 3
    assert "Coverage must include" in model._calls[2][1].content


def test_tool_loop_hits_shared_model_budget_and_preserves_originals():
    context, _proposal, _review = inputs()
    model = ScriptModel(
        [message(call("read_evidence", {"ids": ["a"]})) for _ in range(2)]
    )
    output = build(model, max_calls=2).invoke(
        {"context": context.model_dump(mode="json")}
    )
    assert not output["accepted"] and output["model_calls"] == 2
    assert len(model._calls) == 2
    assert output["result"]["node"]["consolidation_status"] == "unresolved"
    assert len(output["result"]["node"]["alternatives"]) == 2


def test_missing_source_cannot_vacuously_pass_coverage():
    context, proposal, _review = inputs()
    context = context.model_copy(update={"evidence": ()})
    model = ScriptModel([response(proposal) for _ in range(3)])
    output = build(model).invoke({"context": context.model_dump(mode="json")})
    assert not output["accepted"]
    assert not model._calls
    assert output["stop_reason"] == "evidence_unavailable"
    assert output["result"]["node"]["consolidation_status"] == "unresolved"


def test_group_receives_only_its_sources_from_full_run_snapshot():
    context, proposal, review = inputs()
    unrelated = tuple(
        EvidenceItem(
            id=f"unrelated:{i}",
            source=SourceReference(source_id=f"unrelated:{i}", case_id=str(i)),
            case_id=str(i),
            content={"question": "Unrelated", "sql": "SELECT 1"},
        )
        for i in range(600)
    )
    model = ScriptModel([response(proposal), response(review)])
    reconciler = NodeGroupReconciler(
        group_consolidator=LLMGroupConsolidator(model),
        consolidation_checker=LLMConsolidationChecker(model),
    )
    result = reconciler.reconcile_group(
        context.group,
        schema=context.schema_definition,
        available_nodes=context.nodes,
        evidence_items=(*context.evidence, *unrelated),
    )
    assert result.conflict is None
    assert len(model._calls) == 2
    payload = json.loads(
        model._calls[0][1]
        .content.split("<merge_context>\n")[1]
        .split("\n</merge_context>")[0]
    )
    assert len(payload["evidence_directory"]) == 2
    assert len(payload["evidence"]) == 2


@pytest.mark.parametrize(
    ("node_type", "properties", "error"),
    [
        (
            "metric",
            {
                "expression": {"expr": "SUM(${x})", "refs": {"x": "missing"}},
            },
            "nested references",
        ),
        (
            "metric",
            {"expression": {"expr": "SUM(${x})", "refs": {}}},
            "placeholders",
        ),
        (
            "metric",
            {
                "expression": {"expr": "COUNT(*)", "refs": {}},
                "sql_generation_plans": [{"id": "p"}],
            },
            "plans require",
        ),
        (
            "concept",
            {"targets": [["missing"]]},
            "nested references",
        ),
    ],
)
def test_invalid_semantic_structures_are_rejected(node_type, properties, error):
    from cortex.capabilities.graph_merge.reconciliation.validation import (
        validate_proposal,
    )

    context, proposal, _review = inputs()
    group = context.group.model_copy(update={"node_type": node_type})
    candidate = proposal.candidates[0]
    proposal = proposal.model_copy(
        update={
            "candidates": (
                candidate.model_copy(
                    update={
                        "content": candidate.content.model_copy(
                            update={"properties": properties}
                        )
                    }
                ),
            )
        }
    )
    with pytest.raises(ValueError, match=error):
        validate_proposal(
            proposal,
            group,
            context.schema_definition,
            context.nodes,
            context.evidence_by_ref(),
        )


def test_memory_checkpoint_replays_result_without_new_calls():
    context, proposal, review = inputs()
    saver = InMemorySaver()
    model = ScriptModel([response(proposal), response(review)])
    graph = build(model, saver)
    config = {"configurable": {"thread_id": "one-group"}}
    first = graph.invoke({"context": context.model_dump(mode="json")}, config=config)
    assert graph.invoke(None, config=config)["result"] == first["result"]
    assert "conflict" not in graph.get_state(config).values["result"]
    assert len(model._calls) == 2


@pytest.fixture
def checkpoint_path(tmp_path):
    return str(tmp_path / "merge-checkpoints.sqlite")


def test_sqlite_resumes_after_proposal_without_recalling_consolidator(checkpoint_path):
    context, proposal, review = inputs()

    def power_loss(_messages):
        raise KeyboardInterrupt("simulated process loss")

    first_model = ScriptModel([response(proposal), power_loss])
    config = {"configurable": {"thread_id": "resume-group"}}
    with (
        SqliteSaver.from_conn_string(checkpoint_path) as saver,
        pytest.raises(KeyboardInterrupt),
    ):
        build(first_model, saver).invoke(
            {"context": context.model_dump(mode="json")},
            config=config,
            durability="sync",
        )
    second_model = ScriptModel([response(review)])
    with SqliteSaver.from_conn_string(checkpoint_path) as saver:
        output = build(second_model, saver).invoke(
            None, config=config, durability="sync"
        )
    assert output["accepted"] and len(second_model._calls) == 1
    assert "source_checklist" in second_model._calls[0][1].content


class OneGroup(IdentityGrouper):
    def __init__(self):
        super().__init__(None)
        self.calls = 0

    def group(self, existing, incoming, *args, **kwargs):
        self.calls += 1
        context, _, _ = inputs()
        return IdentityGroupingResult(groups=(context.group,))


def test_sqlite_merge_resume_preserves_grouping_and_validates_input(checkpoint_path):
    context, proposal, review = inputs()

    def power_loss(_messages):
        raise KeyboardInterrupt("simulated process loss")

    grouper = OneGroup()
    model = ScriptModel([response(proposal), power_loss])

    def factory():
        return SqliteSaver.from_conn_string(checkpoint_path)

    def operator(model):
        return GraphMergeOperator(
            identity_grouper=grouper,
            checkpoint_factory=factory,
            node_reconciler=NodeGroupReconciler(
                group_consolidator=LLMGroupConsolidator(model),
                consolidation_checker=LLMConsolidationChecker(model),
                evidence_resolver=Evidence(context.evidence),
            ),
        )

    old = GraphState(graph=GraphSpace(), node_conflicts=())
    incoming = GraphState(graph=GraphSpace(nodes=context.nodes), node_conflicts=())
    with pytest.raises(KeyboardInterrupt):
        operator(model).merge(old, incoming, load_root_schema(), run_id="resume-merge")
    second = ScriptModel([response(review)])
    result = operator(second).merge(
        old, incoming, load_root_schema(), run_id="resume-merge"
    )
    assert len(result.state.graph.nodes) == 1 and len(second._calls) == 1
    assert result.run_id == "resume-merge" and grouper.calls == 1
    assert (
        operator(ScriptModel([])).merge(
            old, incoming, load_root_schema(), run_id="resume-merge"
        )
        == result
    )
    changed = incoming.model_copy(
        update={
            "graph": incoming.graph.model_copy(
                update={"nodes": incoming.graph.nodes[:1]}
            )
        }
    )
    with pytest.raises(ValueError, match="Cannot resume merge"):
        operator(ScriptModel([])).merge(
            old, changed, load_root_schema(), run_id="resume-merge"
        )


class Evidence:
    def __init__(self, evidence):
        self.evidence = evidence

    def resolve(self, sources):
        return tuple(e for e in self.evidence if e.source in sources)


def test_paper_mixed_group_keeps_conditional_definition_and_complete_competitor():
    from cortex.capabilities.graph_merge.models import (
        DifferenceMember,
        DifferencePoint,
        DifferencePosition,
    )

    nodes, members, evidence = [], [], []
    for key, expression, product in (
        ("N1", "returned_units / sold_units", "functional sofa"),
        ("N2", "returned_units / sold_units", "other products"),
        ("N3", "orders_with_returns / all_orders", "functional sofa"),
    ):
        source = SourceReference(source_id=key, case_id=key)
        condition = (
            "Apply kit_coef >= 0.8."
            if product == "functional sofa"
            else "No kit filter."
        )
        node = GraphNode(
            id=key,
            node_type="metric",
            name="Return rate",
            description=condition,
            properties={
                "expression": {"expr": expression, "refs": {}},
                "pattern": expression,
                "sql_generation_plans": [],
            },
            sources=(source,),
        )
        nodes.append(node)
        members.append(
            NodeRepresentation(
                ref=RepresentationRef(origin="incoming_node", ref_id=key),
                node_type="metric",
                content=node_content(node),
                sources=(source,),
            )
        )
        evidence.append(
            EvidenceItem(
                id=f"e:{key}",
                source=source,
                case_id=key,
                content={
                    "question": f"Return rate for {product}",
                    "sql": f"SELECT {expression} FROM sales",
                },
            )
        )
    group = FinalNodeGroup(
        group_id="return-rate",
        node_type="metric",
        existing_node_id=None,
        members=tuple(members),
    )
    common = members[0].content.model_copy(
        update={
            "description": "For product p, use returned_units / sold_units; require kit_coef >= 0.8 for functional sofas, otherwise no kit filter."
        }
    )
    point = DifferencePoint(
        point_id="counting-base",
        aspect="metric_expression",
        resolution="conflict",
        reason="The same product/filter does not select a counting base.",
        positions=tuple(
            DifferencePosition(
                statement=statement,
                members=tuple(
                    DifferenceMember(
                        representation_ref=members[i].ref,
                        source_case_ids=(f"N{i + 1}",),
                    )
                    for i in indices
                ),
            )
            for statement, indices in (("unit ratio", (0, 1)), ("order ratio", (2,)))
        ),
    )
    proposal = ConsolidationProposal(
        analysis=ConsolidationAnalysis(shared=("A return rate",)),
        candidates=(
            ConsolidationCandidate(
                content=common, supporting_refs=tuple(m.ref for m in members[:2])
            ),
            ConsolidationCandidate(
                content=members[2].content, supporting_refs=(members[2].ref,)
            ),
        ),
        difference_points=(point,),
    )
    review = ConsolidationReview(
        generalization={
            "verdict": "acceptable",
            "reason": "Product explains filtering, not counting base",
        },
        coverage_by_source=tuple(
            SourceCoverage(
                supporting_ref=m.ref,
                evidence_id=e.id,
                verdict="covered",
                reason="Complete source behavior retained",
            )
            for m, e in zip(members, evidence, strict=True)
        ),
    )
    context = GroupContext(
        group=group,
        schema_definition=load_root_schema(),
        evidence=tuple(evidence),
        nodes=tuple(nodes),
    )
    output = build(ScriptModel([response(proposal), response(review)])).invoke(
        {"context": context.model_dump(mode="json")}
    )
    assert output["accepted"]
    conflict = output["result"]["node"]
    assert conflict["consolidation_status"] == "semantic_conflict"
    assert len(conflict["alternatives"]) == 2
    assert len(conflict["conflict_points"][0]["choices"]) == 2
    assert {
        a["overrides"]["properties.expression"]["expr"]
        for a in conflict["alternatives"]
    } == {"returned_units / sold_units", "orders_with_returns / all_orders"}
    assert any(
        "kit_coef >= 0.8" in a["overrides"]["description"]
        for a in conflict["alternatives"]
    )


def test_native_langsmith_keeps_usage_and_hides_payloads(monkeypatch):
    import requests
    from langsmith import Client

    from cortex.infrastructure.workflow import tracing

    class RecordingSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.payloads = []

        def request(self, method, url, **kwargs):
            raw = kwargs.get("data") or kwargs.get("json") or {}
            if isinstance(raw, (str, bytes)):
                raw = json.loads(raw)
            self.payloads.append(raw)
            result = requests.Response()
            result.status_code = 200
            result._content = b"{}"
            return result

    context, proposal, review = inputs()
    private = "PRIVATE_SQL_SENTINEL"
    context = context.model_copy(
        update={
            "evidence": tuple(
                e.model_copy(update={"content": {**e.content, "sql": private}})
                for e in context.evidence
            )
        }
    )
    session = RecordingSession()
    clients = []

    def client_factory(**options):
        client = Client(
            api_url="http://langsmith.test",
            api_key="local-test",
            session=session,
            auto_batch_tracing=False,
            **options,
        )
        clients.append(client)
        return client

    tracing._summary_client.cache_clear()
    monkeypatch.setattr(tracing, "Client", client_factory)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    messages = [response(proposal), response(review)]
    for item in messages:
        item.usage_metadata = {
            "input_tokens": 20,
            "output_tokens": 10,
            "total_tokens": 30,
        }
    with tracing.merge_tracing():
        result = build(ScriptModel(messages)).invoke(
            {"context": context.model_dump(mode="json")}
        )

    def failure(_messages):
        raise ValueError(private)

    with tracing.merge_tracing():
        failed = build(ScriptModel([failure]), max_calls=1).invoke(
            {"context": context.model_dump(mode="json")}
        )
    assert not failed["accepted"]
    client = clients[0]
    client.flush()
    assert result["accepted"]
    assert session.payloads
    serialized = json.dumps(session.payloads, default=str)
    assert private not in serialized
    assert "input_tokens" in serialized
    assert "20" in serialized and "10" in serialized
    client.close()
    tracing._summary_client.cache_clear()


def test_equal_definitions_skip_agents_but_distinct_relations_do_not():
    context, proposal, review = inputs()
    members = (
        context.group.members[0],
        context.group.members[1].model_copy(
            update={"content": context.group.members[0].content}
        ),
    )
    group = context.group.model_copy(update={"members": members})
    model = ScriptModel([])
    service = NodeGroupReconciler(
        group_consolidator=LLMGroupConsolidator(model),
        consolidation_checker=LLMConsolidationChecker(model),
    )
    result = service.reconcile_group(
        group, schema=load_root_schema(), evidence_items=context.evidence
    )
    assert not model._calls and len(result.node.sources) == 2
    model._script.extend(
        [
            response(
                proposal.model_copy(
                    update={
                        "candidates": (
                            proposal.candidates[0].model_copy(
                                update={"content": members[0].content}
                            ),
                        )
                    }
                )
            ),
            response(review),
        ]
    )
    result = service.reconcile_group(
        group,
        schema=load_root_schema(),
        evidence_items=context.evidence,
        available_nodes=context.nodes,
        unchanged_relations=False,
    )
    assert len(model._calls) == 2 and result.conflict is None


def test_untouched_existing_node_is_not_reinterpreted_for_unrelated_increment():
    context, _, _ = inputs()
    old = context.nodes[0]
    incoming = context.nodes[1]
    model = ScriptModel([])
    service = NodeGroupReconciler(
        group_consolidator=LLMGroupConsolidator(model),
        consolidation_checker=LLMConsolidationChecker(model),
    )
    operator = GraphMergeOperator(
        identity_grouper=IdentityGrouper(None), node_reconciler=service
    )
    result = operator.merge(
        GraphState(graph=GraphSpace(nodes=(old,)), node_conflicts=()),
        GraphState(graph=GraphSpace(nodes=(incoming,)), node_conflicts=()),
        load_root_schema(),
    )
    assert next(n for n in result.state.graph.nodes if n.id == old.id) == old
    assert not model._calls


@pytest.mark.parametrize("function", ["RANK", "DENSE_RANK"])
def test_rank_explanation_is_assessed_by_agent_not_schema(function):
    from cortex.capabilities.graph_merge.reconciliation.validation import (
        validate_proposal,
    )

    context, proposal, _ = inputs()
    content = proposal.candidates[0].content.model_copy(
        update={
            "properties": {
                **proposal.candidates[0].content.properties,
                "action": f"{function} does not skip rank numbers.",
            }
        }
    )
    proposal = proposal.model_copy(
        update={
            "candidates": (
                proposal.candidates[0].model_copy(update={"content": content}),
            )
        }
    )
    validate_proposal(
        proposal,
        context.group,
        context.schema_definition,
        context.nodes,
        context.evidence_by_ref(),
    )


def test_structured_agents_allow_automatic_tools_for_reasoning_providers():
    context, proposal, review = inputs()

    class AutomaticOnly(ScriptModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            assert tool_choice == "auto"
            return super().bind_tools(tools, tool_choice=tool_choice, **kwargs)

    output = build(AutomaticOnly([response(proposal), response(review)])).invoke(
        {"context": context.model_dump(mode="json")}
    )
    assert output["accepted"]


def test_plain_text_does_not_end_role_before_structured_submission():
    from langchain_core.messages import AIMessage

    context, proposal, review = inputs()
    model = ScriptModel(
        [
            AIMessage(content="I have reviewed the definitions."),
            response(proposal),
            response(review),
        ]
    )
    output = build(model).invoke({"context": context.model_dump(mode="json")})
    assert output["accepted"] and output["model_calls"] == 3
    assert "structured output tool" in model._calls[1][-1].content


def test_invalid_json_tool_call_receives_feedback_without_lenient_acceptance():
    from langchain_core.messages import AIMessage

    context, proposal, review = inputs()
    invalid = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "id": "bad-json",
                "name": "ConsolidationProposal",
                "args": "invalid JSON arguments",
                "error": "Invalid JSON escape in patient apostrophe",
                "type": "invalid_tool_call",
            }
        ],
    )
    model = ScriptModel([invalid, response(proposal), response(review)])
    output = build(model).invoke({"context": context.model_dump(mode="json")})
    assert output["accepted"] and output["model_calls"] == 3
    assert "Invalid JSON escape" in model._calls[1][-1].content


def test_plain_text_retry_is_bounded_and_preserves_original_definitions():
    from langchain_core.messages import AIMessage

    context, _, _ = inputs()
    model = ScriptModel([AIMessage(content="Still reasoning") for _ in range(4)])
    output = build(model, max_calls=4).invoke(
        {"context": context.model_dump(mode="json")}
    )
    assert not output["accepted"]
    assert len(model._calls) == 4
    assert output["result"]["node"]["merge_status"] == "conflict"
    assert output["result"]["node"]["consolidation_status"] == "unresolved"


def test_conflict_checkpoint_stores_candidates_once_on_its_node():
    context, _, _ = inputs()
    context = context.model_copy(update={"evidence": ()})
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "one-inline-conflict"}}
    graph = build(ScriptModel([]), saver)
    output = graph.invoke({"context": context.model_dump(mode="json")}, config=config)
    result = graph.get_state(config).values["result"]
    assert "conflict" not in result
    assert result["node"]["alternatives"] == output["result"]["node"]["alternatives"]
    assert result["node"]["merge_status"] == "conflict"
    assert result["node"]["consolidation_status"] == "unresolved"
    assert (
        graph.get_state(config).values["context"]["workflow_version"]
        == "merge-group-inline-node-v2"
    )


def test_reconciler_refuses_old_group_checkpoint_layout():
    import hashlib

    from cortex.capabilities.graph_merge.reconciliation.service import (
        NodeGroupReconciler,
    )

    context, proposal, review = inputs()
    saver = InMemorySaver()
    model = ScriptModel([response(proposal), response(review)])
    reconciler = NodeGroupReconciler(
        group_consolidator=LLMGroupConsolidator(model),
        consolidation_checker=LLMConsolidationChecker(model),
    )
    kwargs = {
        "schema": context.schema_definition,
        "available_nodes": context.nodes,
        "evidence_items": context.evidence,
        "checkpointer": saver,
        "run_id": "old-layout",
    }
    reconciler.reconcile_group(context.group, **kwargs)
    suffix = hashlib.sha256(context.group.group_id.encode()).hexdigest()[:24]
    config = {
        "configurable": {"thread_id": f"old-layout:group:{suffix}", "checkpoint_ns": ""}
    }
    graph = build(model, saver)
    previous = dict(graph.get_state(config).values["context"])
    previous["workflow_version"] = "merge-group-sidecar-v1"
    graph.update_state(config, {"context": previous})
    with pytest.raises(ValueError, match="Cannot resume group checkpoint"):
        reconciler.reconcile_group(context.group, **kwargs)
    assert len(model._calls) == 2
