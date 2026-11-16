"""Evidence selection must not lose conditions or introduce unsupported resolutions."""

import asyncio
import json

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from cortex.application.online_context import ChoicePoint, InteractionOption
from cortex.capabilities.online_context.filter_evidence import build_filter_evidence
from cortex.capabilities.online_context.filtering import ClarificationFilterAgent
from cortex.schema import (
    ConflictAlternative,
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
)


def point(key, refs):
    return ChoicePoint(
        choice_point_id=key,
        decision_question="Which calculation?",
        scope="calculation",
        options=(
            InteractionOption(
                option_id="a",
                statement="COUNT(*)",
                graph_refs=tuple(refs),
                candidate_sources=("memory_graph",) if refs else ("initial_sql",),
                initial_sql_evidence=None if refs else "COUNT(*)",
            ),
            InteractionOption(
                option_id="b",
                statement="COUNT(t.id)",
                initial_sql_evidence="COUNT(t.id)",
                candidate_sources=("initial_sql",),
            ),
        ),
    )


def node(key, kind="concept", **properties):
    return GraphNode(id=key, node_type=kind, name=key, properties=properties)


def conflict():
    return node(
        "rule.count",
        "generation_rule",
        condition="Only active accounts",
        references=["column.t.id"],
        grain="one account",
    ).model_copy(
        update={
            "merge_status": "conflict",
            "consolidation_status": "unresolved",
            "alternatives": (
                ConflictAlternative(
                    alternative_id="alternative.rows",
                    overrides={"properties.action": "COUNT(*)"},
                ),
                ConflictAlternative(
                    alternative_id="alternative.ids",
                    overrides={"properties.action": "COUNT(id)"},
                ),
            ),
        }
    )


def state(*extra, edges=()):
    return GraphState(
        graph=GraphSpace(
            nodes=(
                conflict(),
                node("column.t.id", "column", table_ref="table.t", nullable=False),
                node("table.t", "table", table_name="t", primary_keys=["id"]),
                node("unrelated", description="UNRELATED_SENTINEL"),
                *extra,
            ),
            edges=edges,
        )
    )


def test_alternative_restores_conditions_and_only_required_dependencies():
    graph = state()
    before = graph.model_dump_json()
    payload, missing = build_filter_evidence(graph, (point("x", ["alternative.ids"]),))
    views = {e["graph_ref"]: e for e in payload["evidence"]}
    assert set(views) == {"alternative.ids", "column.t.id", "table.t"}
    assert views["alternative.ids"]["properties"] == {
        "condition": "Only active accounts",
        "references": ["column.t.id"],
        "grain": "one account",
        "action": "COUNT(id)",
    }
    assert "alternative.rows" not in json.dumps(payload)
    assert "UNRELATED_SENTINEL" not in json.dumps(payload)
    assert not missing
    assert graph.model_dump_json() == before


def test_generic_conflict_reference_keeps_all_branches_and_deduplicates():
    payload, missing = build_filter_evidence(
        state(),
        (
            point("x", ["rule.count"]),
            point("y", ["rule.count"]),
        ),
    )
    views = {e["graph_ref"]: e for e in payload["evidence"]}
    assert len(views["rule.count"]["alternatives"]) == 2
    assert len(payload["evidence"]) == 3
    assert not missing


def test_target_preserves_selected_binding_without_siblings():
    owner = node(
        "concept.binding",
        targets=[["column.t.id"], ["unrelated"]],
        target_expressions={"0": "t.id", "1": "other.id"},
        condition="active accounts only",
    )
    payload, missing = build_filter_evidence(
        state(owner), (point("x", ["target:concept.binding:1"]),)
    )
    binding = next(
        e for e in payload["evidence"] if e["graph_ref"].startswith("target:")
    )
    assert binding["properties"]["targets"] == [["column.t.id"]]
    assert binding["properties"]["condition"] == "active accounts only"
    assert "other.id" not in json.dumps(payload)
    assert not missing


def test_dependency_cycles_terminate_without_neighbor_expansion():
    graph = state(
        node("metric.a", references=["metric.b"]),
        node("metric.b", references=["metric.a"]),
        edges=(
            GraphEdge(
                id="edge.unrelated",
                edge_type="applies_to",
                from_node_id="metric.a",
                to_node_id="unrelated",
            ),
        ),
    )
    payload, missing = build_filter_evidence(graph, (point("x", ["metric.a"]),))
    assert {e["graph_ref"] for e in payload["evidence"]} == {"metric.a", "metric.b"}
    assert not missing


def test_known_join_between_required_tables_retains_pairs_and_cardinality():
    graph = state(
        node("table.s", "table", table_name="s"),
        node("column.s.tid", "column", table_ref="table.s"),
        edges=(
            GraphEdge(
                id="join.ts",
                edge_type="joins_with",
                from_node_id="table.t",
                to_node_id="table.s",
                properties={
                    "cardinality": "one_to_many",
                    "column_pairs": [
                        {
                            "from_column_ref": "column.t.id",
                            "to_column_ref": "column.s.tid",
                        }
                    ],
                },
            ),
        ),
    )
    payload, missing = build_filter_evidence(
        graph, (point("x", ["table.t", "table.s"]),)
    )
    views = {e["graph_ref"]: e for e in payload["evidence"]}
    assert views["join.ts"]["properties"]["cardinality"] == "one_to_many"
    assert "column.s.tid" in views
    assert not missing


def test_missing_transitive_dependency_blocks_only_affected_slot():
    graph = state(node("metric.incomplete", references=["column.missing.id"]))
    points = (point("bad", ["metric.incomplete"]), point("good", ["column.t.id"]))
    _, missing = build_filter_evidence(graph, points)
    assert missing == {"bad": ["column.missing.id"]}
    decisions = [
        {
            "choice_point_id": p.choice_point_id,
            "outcome": "question_resolved",
            "question_evidence": "count rows",
            "excluded_options": {"b": "requires rows"},
            "reason": "count rows explicitly",
        }
        for p in points
    ]
    result = asyncio.run(
        ClarificationFilterAgent(
            FakeListChatModel(
                responses=[json.dumps({"decisions": decisions})],
            )
        ).filter(
            question="count rows",
            initial_sql="COUNT(t.id)",
            retrieved_graph_state=graph,
            choice_points=points,
            dialect="sqlite",
        )
    )
    assert [d.outcome for d in result.decisions] == ["keep", "question_resolved"]


def test_no_memory_refs_sends_no_memory():
    payload, missing = build_filter_evidence(state(), (point("x", []),))
    assert payload["evidence"] == []
    assert not missing


def test_conflicting_target_binding_is_incomplete_instead_of_guessed():
    graph = state()
    owner = conflict().model_copy(update={"properties": {"targets": [["column.t.id"]]}})
    graph = graph.model_copy(
        update={
            "graph": graph.graph.model_copy(
                update={
                    "nodes": (owner, *graph.graph.nodes[1:]),
                }
            )
        }
    )
    _, missing = build_filter_evidence(graph, (point("x", ["target:rule.count:1"]),))
    assert missing == {"x": ["target:rule.count:1"]}


def test_round_inputs_keep_required_memory_in_reusable_prefix():
    traces = []
    model = FakeListChatModel(
        responses=[
            json.dumps(
                {
                    "requests": [
                        {
                            "choice_point_id": "x",
                            "tool": "inspect_schema",
                            "purpose": "Check the counted ID",
                            "tables": ["t"],
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "decisions": [
                        {
                            "choice_point_id": "x",
                            "outcome": "keep",
                            "reason": "Insufficient evidence",
                        }
                    ]
                }
            ),
        ]
    )
    asyncio.run(
        ClarificationFilterAgent(
            model,
            trace_sink=lambda stage, payload: traces.append((stage, payload)),
        ).filter(
            question="Task-specific question",
            initial_sql="SELECT COUNT(t.id) FROM t",
            retrieved_graph_state=state(),
            choice_points=(point("x", ["alternative.ids"]),),
            dialect="sqlite",
        )
    )
    prompts = [
        p["prompt"][1]["content"] for stage, p in traces if stage == "filter_llm"
    ]
    assert len(prompts) == 2
    assert all("UNRELATED_SENTINEL" not in p for p in prompts)
    assert (
        prompts[0].split('"observations":')[0] == prompts[1].split('"observations":')[0]
    )
    keys = list(json.loads(prompts[0]))
    assert (
        keys.index("tools")
        < keys.index("retrieved_graph_state")
        < keys.index("question")
    )
    assert (
        keys.index("slots")
        < keys.index("observations")
        < keys.index("remaining_probes")
    )
    assert (
        json.loads(prompts[1])["observations"][0]["error"]
        == "database tools unavailable"
    )
