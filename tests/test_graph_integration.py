"""Contracts for graph rewrites, source correspondence and independent review."""

from contextlib import nullcontext

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.publication import (
    commit_result,
    compile_proposal,
    identity_map,
    mapped_base,
)
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.integration.engine import (
    GraphIntegrationEngine,
    IntegrationIncomplete,
)
from cortex.capabilities.graph_merge.graph.edit_contract import (
    IdentityDecision,
    IntegrationProposal,
    IntegrationReview,
)
from cortex.capabilities.graph_merge.identity.tools import build_tools
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    SourceReference,
    load_root_schema,
)


def concept(key, target, *, name=None):
    return GraphNode(
        id=key,
        node_type="concept",
        name=name or key,
        properties={"targets": [[target]]},
        sources=(SourceReference(source_id=key),),
    )


def edge(key, source, target, kind="grounded_on"):
    return GraphEdge(
        id=key,
        edge_type=kind,
        from_node_id=source,
        to_node_id=target,
        properties={"description": "The evidenced business relation."},
    )


def business_contract():
    return load_root_schema().edge_type_index()["has_attribute"].model_copy(update={
        "id": "has_feature", "name": "has feature", "description": "The object has the specified business feature.",
    })


def state(nodes=(), edges=()):
    return GraphManager(load_root_schema()).prepare_state(
        GraphState(graph=GraphSpace(nodes=tuple(nodes), edges=tuple(edges), relation_contracts=(business_contract(),) if any(e.edge_type == "has_feature" for e in edges) else ()))
    )[0]


def catalog():
    return [
        GraphNode(
            id="table",
            node_type="table",
            name="objects",
            properties={"table_name": "objects"},
        ),
        *[
            GraphNode(
                id=k,
                node_type="column",
                name=k,
                properties={"table_ref": "table", "column_name": k},
            )
            for k in ("a", "b", "c")
        ],
    ]


def contexts(old, new):
    context = IntegrationContext(old, new, load_root_schema())
    context.payload("identity")
    return context


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
    from cortex.capabilities.graph_merge.models import EvidenceItem

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


def test_physical_objects_cannot_be_retyped_to_bypass_read_only_protection():
    c = contexts(state(), state([*catalog(), concept("A", "a")]))
    c.read_ids.update(c.nodes)
    physical_id = c.input_node_map["table"]
    physical_edge = next(
        e
        for e in c.edges.values()
        if c.nodes[e.from_node_id].node_type == "column"
        and c.nodes[e.to_node_id].node_type == "table"
    )
    for kind, key, edit in (
        (
            "node",
            physical_id,
            {
                "operation": "update_node",
                "node": {
                    "id": physical_id,
                    "node_type": "event",
                    "name": "occurrence",
                    "properties": {"definition": "An occurrence"},
                },
            },
        ),
        (
            "edge",
            physical_edge.id,
            {
                "operation": "update_edge",
                "edge": edge(
                    physical_edge.id, c.input_node_map["A"], c.input_node_map["a"]
                ).model_dump(mode="json"),
            },
        ),
    ):
        proposal = IntegrationProposal(
            edits=[edit],
            changes=[
                {
                    "before": [{"kind": kind, "id": key}],
                    "after": [{"kind": kind, "id": key}],
                    "explanation": "Attempted physical object replacement.",
                }
            ],
        )
        with pytest.raises(ValueError, match="existing physical catalog"):
            compile_proposal(c, IdentityDecision(), proposal)


def test_identity_is_explicit_and_binding_contests_are_not_automatic_merges():
    old = state([*catalog(), concept("first", "a")], [edge("g1", "first", "a")])
    new = state([*catalog(), concept("second", "a")], [edge("g2", "second", "a")])
    c = contexts(old, new)
    second = c.input_node_map["second"]
    with pytest.raises(ValueError, match="binding contest"):
        identity_map(c, IdentityDecision())
    d = IdentityDecision(
        groups=[
            {
                "members": ["first", second],
                "meaning": "Same object",
                "reason": "Source evidence",
            }
        ]
    )
    preview = compile_proposal(c, d, IntegrationProposal())
    result = commit_result(c, IntegrationProposal(), preview, run_id="same")
    assert [n.id for n in result.state.graph.nodes if n.node_type == "concept"] == [
        "first"
    ]
    assert (
        len([e for e in result.state.graph.edges if e.edge_type == "grounded_on"]) == 1
    )
    assert {x.input_id: x.canonical_id for x in result.node_mappings}[
        "second"
    ] == "first"
    assert len(old.graph.nodes) == 5  # Original input remains immutable.


def test_edge_to_path_has_joint_correspondence_not_fake_alias_or_source_spread():
    nodes = [*catalog(), concept("A", "a"), concept("B", "b"), concept("C", "c")]
    edges = [
        edge("ga", "A", "a"),
        edge("gb", "B", "b"),
        edge("gc", "C", "c"),
        edge("ca", "C", "A", "is_a"),
        edge("cb", "C", "B", "is_a"),
        edge("ba", "B", "A", "is_a"),
    ]
    old = state()
    c = contexts(old, state(nodes, edges))
    ca, cb, ba = [c.input_edge_map[key] for key in ("ca", "cb", "ba")]
    proposal = IntegrationProposal(
        edits=[{"operation": "delete_edge", "edge_id": ca}],
        changes=[
            {
                "before": [{"kind": "edge", "id": ca}],
                "after": [{"kind": "edge", "id": cb}, {"kind": "edge", "id": ba}],
                "explanation": "Same-scope class inclusion follows along the evidenced path.",
            }
        ],
    )
    preview = compile_proposal(c, IdentityDecision(), proposal)
    result = commit_result(c, proposal, preview, run_id="path")
    assert "ca" not in {m.input_id for m in result.edge_mappings}
    assert len(result.rewrites[0].after) == 2
    assert result.rewrites[0].before[0].id == "ca"
    assert not next(e for e in result.state.graph.edges if e.id == "cb").sources


def test_deletion_requires_explicit_consumer_repair_and_does_not_cascade():
    c = contexts(
        state(), state([*catalog(), concept("A", "a")], [edge("ga", "A", "a")])
    )
    a = c.input_node_map["A"]
    p = IntegrationProposal(
        edits=[{"operation": "delete_node", "node_id": a}],
        changes=[
            {
                "before": [{"kind": "node", "id": a}],
                "after": [],
                "explanation": "Hypothetical evidenced correction.",
            }
        ],
    )
    with pytest.raises(Exception, match="unknown|missing|endpoint|does not exist"):
        compile_proposal(c, IdentityDecision(), p)
    assert a in c.nodes


def test_same_triple_different_scope_descriptions_are_not_mechanical_duplicates():
    c = contexts(
        state(),
        state(
            [*catalog(), concept("A", "a"), concept("B", "b")],
            [
                edge("ga", "A", "a"),
                edge("gb", "B", "b"),
                edge("r1", "A", "B", "has_feature"),
                edge("r2", "A", "B", "has_feature").model_copy(
                    update={
                        "properties": {
                            "description": "Only in the earlier reporting scope."
                        }
                    }
                ),
            ],
        ),
    )
    preview = compile_proposal(c, IdentityDecision(), IntegrationProposal())
    assert (
        len([e for e in preview.state.graph.edges if e.edge_type == "has_feature"])
        == 2
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


class Agent:
    def __init__(self, accept=True):
        self.phases, self.accept = [], accept

    def invoke(self, phase, context, payload):
        self.phases.append(phase)
        if phase == "identity":
            return IdentityDecision()
        if phase == "propose":
            return IntegrationProposal()
        assert set(payload["proposal"]) == {"changes"}
        assert "candidate_nodes" in payload and "candidate_relations" in payload
        assert all(
            n["node_type"] not in {"table", "column", "dimension_value"}
            for n in payload["candidate_nodes"]
        )
        return IntegrationReview(
            verdict="accept" if self.accept else "revise",
            decision_check="Source identity checked.",
            quality_check="Candidate node and relation contracts checked.",
            preservation_check="All meanings preserved.",
            issues=[] if self.accept else ["Deliberate rejection"],
        )


def test_new_candidate_identity_is_retrieved_before_commit_without_source_mutation():
    old = state(
        [
            GraphNode(
                id=f"event{i}",
                node_type="event",
                name=f"occurrence {i}",
                properties={"definition": f"Occurrence {i}"},
            )
            for i in range(10)
        ]
    )
    incoming = state(
        [
            GraphNode(
                id="seed",
                node_type="event",
                name="new occurrence",
                properties={"definition": "An occurrence"},
            )
        ]
    )

    class SplitAgent(Agent):
        def invoke(self, phase, context, payload):
            if phase != "propose":
                return super().invoke(phase, context, payload)
            self.phases.append(phase)
            if self.phases.count("propose") == 1:
                unseen = next(
                    n for n in old.graph.nodes if n.id not in context.read_ids
                )
                self.existing_match = unseen.id
                return IntegrationProposal(
                    edits=[
                        {
                            "operation": "create_node",
                            "node": unseen.model_copy(
                                update={"id": "proposed_event"}
                            ).model_dump(mode="json"),
                        }
                    ]
                )
            assert self.existing_match in {n["id"] for n in payload["nodes"]}
            assert "proposed_event" in " ".join(payload["feedback"])
            assert "proposed_event" not in context.nodes
            # The validated candidate now retains the first proposal. Remove the
            # redundant new occurrence explicitly while reusing the source identity.
            assert payload["proposal_mode"] == "delta"
            return IntegrationProposal(
                edits=[{"operation": "delete_node", "node_id": "proposed_event"}]
            )

    agent = SplitAgent()
    result = GraphIntegrationEngine(agent, top_k=1).merge(
        old, incoming, load_root_schema()
    )
    assert agent.phases == ["identity", "propose", "propose", "assess"]
    assert "proposed_event" not in {n.id for n in result.state.graph.nodes}


def test_candidate_view_retrieval_accepts_new_node_and_returns_only_source_matches():
    c = contexts(state(), state([*catalog(), concept("A", "a")]))
    new = concept("new", c.input_node_map["a"], name="A")
    c.candidate = GraphState(graph=GraphSpace(nodes=(*c.nodes.values(), new)))
    result = build_tools(c)[0].invoke(
        {"mode": "candidates", "view": "candidate", "ids": ["new"]}
    )
    assert result["items"][0]["id"] == c.input_node_map["A"]
    assert "new" not in c.nodes and "new" not in c.read_ids


def test_physical_reads_do_not_expand_the_assessment_candidate_to_the_catalog():
    class PhysicalReader(Agent):
        def invoke(self, phase, context, payload):
            if phase == "propose":
                physical = [
                    key
                    for key, node in context.nodes.items()
                    if node.node_type in {"table", "column"}
                ]
                build_tools(context)[0].invoke({"mode": "nodes", "ids": physical})
            if phase == "assess":
                assert len(payload["candidate_nodes"]) == 1
                assert all(
                    e["edge_type"] == "grounded_on"
                    for e in payload["candidate_relations"]
                )
            return super().invoke(phase, context, payload)

    incoming = state([*catalog(), concept("A", "a")], [edge("ga", "A", "a")])
    GraphIntegrationEngine(PhysicalReader()).merge(
        state(), incoming, load_root_schema()
    )


def test_separate_identity_and_assessment_checkpoint_and_no_partial_publish():
    incoming = state([*catalog(), concept("A", "a")], [edge("ga", "A", "a")])
    agent, saver = Agent(), InMemorySaver()
    engine = GraphIntegrationEngine(
        agent, checkpoint_factory=lambda: nullcontext(saver)
    )
    result = engine.merge(state(), incoming, load_root_schema(), run_id="one")
    assert agent.phases == ["identity", "propose", "assess"]
    assert engine.merge(state(), incoming, load_root_schema(), run_id="one") == result
    assert len(agent.phases) == 3
    before = state()
    with pytest.raises(IntegrationIncomplete):
        GraphIntegrationEngine(Agent(False), max_rounds=2).merge(
            before, incoming, load_root_schema()
        )
    assert not before.graph.nodes


def test_safe_remap_does_not_replace_sql_or_description_literals():
    c = contexts(
        state(), state([*catalog(), concept("A", "a")], [edge("ga", "A", "a")])
    )
    nodes, _, _, _ = mapped_base(c, IdentityDecision())
    assert nodes[c.input_node_map["A"]].name == "A"
    assert nodes[c.input_node_map["A"]].properties["targets"] == [
        [c.input_node_map["a"]]
    ]


def rule(key, action):
    return GraphNode(
        id=key,
        node_type="generation_rule",
        name=key,
        properties={
            "triggers": {"question": {"phrases": ["test behavior"]}},
            "condition": "When the evidenced operation is requested",
            "action": action,
            "reason": "Supported by fixture source",
        },
        sources=(SourceReference(source_id="case"),),
    )


def test_rule_split_is_one_to_many_correspondence_without_single_node_alias():
    c = contexts(
        state(),
        state([rule("compound", "Select the group, then restrict the detail rows.")]),
    )
    original = c.input_node_map["compound"]
    first, second = (
        rule("first", "Select the group."),
        rule("second", "Restrict the detail rows."),
    )
    p = IntegrationProposal(
        edits=[
            {"operation": "delete_node", "node_id": original},
            {"operation": "create_node", "node": first.model_dump(mode="json")},
            {"operation": "create_node", "node": second.model_dump(mode="json")},
        ],
        changes=[
            {
                "before": [{"kind": "node", "id": original}],
                "after": [
                    {"kind": "node", "id": "first"},
                    {"kind": "node", "id": "second"},
                ],
                "explanation": "The fixture's two independent obligations are separately preserved.",
            }
        ],
    )
    preview = compile_proposal(c, IdentityDecision(), p)
    result = commit_result(c, p, preview, run_id="split")
    assert not result.node_mappings
    assert len(result.rewrites[0].after) == 2
    assert {s.source_id for n in result.state.graph.nodes for s in n.sources} == {
        "case"
    }


def test_business_cycle_does_not_require_topological_node_scheduling():
    incoming = state(
        [*catalog(), concept("A", "a"), concept("B", "b")],
        [
            edge("ga", "A", "a"),
            edge("gb", "B", "b"),
            edge("ab", "A", "B", "has_feature"),
            edge("ba", "B", "A", "has_feature"),
        ],
    )
    # Structural permissibility only; the fixture reviewer is not a business-truth proof.
    result = GraphIntegrationEngine(Agent()).merge(
        state(), incoming, load_root_schema()
    )
    assert (
        len([e for e in result.state.graph.edges if e.edge_type == "has_feature"])
        == 2
    )


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


def test_accept_review_cannot_contradict_itself_with_open_issues():
    with pytest.raises(ValueError, match="unresolved issues"):
        IntegrationReview(
            verdict="accept",
            decision_check="Pass",
            quality_check="Pass",
            preservation_check="Pass",
            issues=["Lost information"],
        )


def test_edge_only_update_is_visible_to_identity_and_assessment():
    existing = state(
        [*catalog(), concept("A", "a"), concept("B", "b")],
        [edge("ga", "A", "a"), edge("gb", "B", "b")],
    )
    fragment = GraphState(
        graph=GraphSpace(edges=(edge("ab", "A", "B", "has_attribute"),))
    )
    c = contexts(existing, fragment)
    assert c.seeds == {"A", "B"}
    assert any(
        e["edge_type"] == "has_attribute" for e in c.payload("identity")["relations"]
    )
    result = GraphIntegrationEngine(Agent()).merge(
        existing, fragment, load_root_schema()
    )
    assert any(e.id == "ab" for e in result.state.graph.edges)


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


def test_real_agent_wrapper_exposes_tools_and_separates_phase_histories():
    from langchain_core.messages import ToolMessage
    from test_agent_runtime import ScriptModel, call, message

    from cortex.capabilities.graph_merge.llm.integration import IntegrationAgent

    incoming = state([*catalog(), concept("A", "a")], [edge("ga", "A", "a")])
    model = ScriptModel(
        [
            message(call("read_guidance", {"topic": "concept"}, "guide")),
            message(
                call("IdentityDecision", {"groups": [], "distinctions": []}, "identity")
            ),
            message(
                call("IntegrationProposal", {"edits": [], "changes": []}, "proposal")
            ),
            message(
                call(
                    "IntegrationReview",
                    {
                        "verdict": "accept",
                        "decision_check": "Identity checked",
                        "quality_check": "Candidate contracts checked",
                        "preservation_check": "Unchanged fixture meanings preserved",
                        "issues": [],
                    },
                    "review",
                )
            ),
        ]
    )
    result = GraphIntegrationEngine(IntegrationAgent(model)).merge(
        state(), incoming, load_root_schema()
    )
    assert any(n.id == "A" for n in result.state.graph.nodes)
    assert len(model._calls) == 4
    assert any(isinstance(m, ToolMessage) for m in model._calls[1])
    assert not any(isinstance(m, ToolMessage) for m in model._calls[2])
    assert not any(isinstance(m, ToolMessage) for m in model._calls[3])


def test_provider_balance_failure_is_not_retried_as_semantic_feedback():
    class BalanceFailure(RuntimeError):
        status_code = 402

    class UnavailableAgent:
        calls = 0

        def invoke(self, phase, context, payload):
            self.calls += 1
            raise BalanceFailure('Insufficient Balance')

    agent = UnavailableAgent()
    with pytest.raises(BalanceFailure):
        GraphIntegrationEngine(agent).merge(state(), state([*catalog(), concept('A', 'a')]), load_root_schema())
    assert agent.calls == 1
