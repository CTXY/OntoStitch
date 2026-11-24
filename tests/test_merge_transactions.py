"""Reviewed candidates update one graph, preserving conflicts and resumable progress."""

from contextlib import nullcontext

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from relation_fixtures import metric_context_contract

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.identity.legacy import IdentityGrouper
from cortex.capabilities.graph_merge.llm.agents import RoleResult
from cortex.capabilities.graph_merge.llm.tools import build_read_tools
from cortex.capabilities.graph_merge.models import (
    DifferenceMember,
    DifferencePoint,
    DifferencePosition,
    EvidenceItem,
    NodeIdentitySelection,
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
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    SourceReference,
    load_root_schema,
)


def metric(node_id, name, expression, refs=None):
    refs = refs or {}
    expression = f"COUNT(*) + {expression}" if expression.isdigit() else expression
    expression = (
        f"SUM({expression})"
        if expression.startswith("${") and expression.endswith("}")
        else expression
    )
    return GraphNode(
        id=node_id,
        node_type="metric",
        name=name,
        properties={
            "expression": {"expr": expression, "refs": refs},
        },
        sources=(SourceReference(source_id=node_id, case_id=node_id),),
    )


def fixture_edges(nodes):
    by_id = {n.id: n for n in nodes}
    return tuple(
        GraphEdge(
            id=f"edge.{n.id}.{index}",
            edge_type="depends_on"
            if by_id.get(ref) and by_id[ref].node_type in {"concept", "metric"}
            else "grounded_on",
            from_node_id=n.id,
            to_node_id=ref,
            properties={
                "description": "This saved operand is used by the calculation."
            },
        )
        for n in nodes
        for index, ref in enumerate(
            n.properties.get("expression", {}).get("refs", {}).values()
        )
    )


def state(*nodes):
    return GraphManager(load_root_schema()).prepare_state(
        GraphState(
            graph=GraphSpace(nodes=nodes, edges=fixture_edges(nodes)), node_conflicts=()
        )
    )[0]


class ByName:
    def resolve(self, seed, entries, queries, policy):
        name = seed.identity_fields["name"]
        return NodeIdentitySelection(
            selected_entry_id=next(
                (
                    entry.entry_id
                    for entry in entries
                    if entry.representations[0].identity_fields["name"] == name
                ),
                None,
            ),
            query_verdicts=tuple(
                {
                    "query_id": query.query_id,
                    "verdict": "same"
                    if query.identity_fields["name"] == name
                    else "different",
                    "reason": "Explicit test identity",
                }
                for query in queries
            ),
            reason="Explicit test identity",
        )


class Evidence:
    def resolve(self, sources):
        return tuple(
            EvidenceItem(
                id=f"e:{source.source_id}",
                source=source,
                case_id=source.case_id,
                content={"question": "Required source behavior", "sql": "SELECT 1"},
            )
            for source in sources
        )


class Consolidator:
    def __init__(self, *, conflict=False, mismatch=False, fail_after=None):
        self.contexts = []
        self.conflict, self.mismatch, self.fail_after = conflict, mismatch, fail_after

    def invoke(self, context, **kwargs):
        if self.fail_after is not None and len(self.contexts) >= self.fail_after:
            raise KeyboardInterrupt("lost after committed group")
        self.contexts.append(context)
        members = context.group.members
        points = ()
        if self.conflict and len(members) > 1:
            points = (
                DifferencePoint(
                    point_id="calculation",
                    aspect="metric_expression",
                    resolution="conflict",
                    reason="Competing calculations under the same request",
                    positions=tuple(
                        DifferencePosition(
                            statement=member.content.properties["expression"]["expr"],
                            members=(
                                DifferenceMember(
                                    representation_ref=member.ref,
                                    source_case_ids=tuple(
                                        s.case_id for s in member.sources if s.case_id
                                    ),
                                ),
                            ),
                        )
                        for member in members
                    ),
                ),
            )
        proposal = ConsolidationProposal(
            analysis=ConsolidationAnalysis(),
            candidates=()
            if self.mismatch
            else tuple(
                ConsolidationCandidate(
                    content=member.content, supporting_refs=(member.ref,)
                )
                for member in members
            )
            if points
            else (
                ConsolidationCandidate(
                    content=members[0].content,
                    supporting_refs=tuple(m.ref for m in members),
                ),
            ),
            difference_points=points,
            unresolved_refs=tuple(m.ref for m in members) if self.mismatch else (),
            identity_mismatch="Evidence establishes different business quantities"
            if self.mismatch
            else None,
        )
        return RoleResult(proposal, 1)


class Assessor:
    def __init__(self):
        self.contexts = []

    def invoke(self, context, *, proposal, **kwargs):
        from cortex.capabilities.graph_merge.reconciliation.validation import (
            source_checklist,
        )

        self.contexts.append(context)
        parsed = ConsolidationProposal.model_validate(proposal)
        return RoleResult(
            ConsolidationReview(
                generalization={
                    "verdict": "acceptable",
                    "reason": "Checked explicit test behavior",
                },
                coverage_by_source=tuple(
                    SourceCoverage(
                        supporting_ref=ref,
                        evidence_id=evidence_id,
                        verdict="covered",
                        reason="Source retained",
                    )
                    for ref, evidence_id in source_checklist(
                        parsed, context.evidence_by_ref()
                    )
                ),
            ),
            1,
        )


def operator(consolidator, assessor, saver=None):
    return GraphMergeOperator(
        identity_grouper=IdentityGrouper(ByName()),
        node_reconciler=NodeGroupReconciler(
            group_consolidator=consolidator,
            consolidation_checker=assessor,
            evidence_resolver=Evidence(),
        ),
        checkpoint_factory=lambda: nullcontext(saver),
    )


def test_existing_dependency_is_updated_before_its_consumer_and_preview_is_visible():
    upstream = metric("z-upstream", "X", "1")
    consumer = metric("a-consumer", "Y", "${x} + 1", {"x": upstream.id})
    existing = state(upstream, consumer)
    incoming = state(metric("new-upstream", "X", "2"))
    proposer, checker = Consolidator(conflict=True), Assessor()
    result = operator(proposer, checker).merge(existing, incoming, load_root_schema())
    assert [ctx.group.node_type for ctx in proposer.contexts] == ["metric", "metric"]
    consumer_context = proposer.contexts[1]
    assert consumer_context.group.existing_node_id == consumer.id
    assert consumer_context.existing_state.conflict_nodes[0].id == upstream.id
    assert checker.contexts[0].candidate_state is not None
    assert not checker.contexts[0].existing_state.conflict_nodes
    conflict = next(c for c in result.state.conflict_nodes if c.id == upstream.id)
    assert conflict.consolidation_status == "semantic_conflict"
    assert len(conflict.alternatives) == 2
    assert (
        next(n for n in result.state.graph.nodes if n.id == upstream.id).merge_status
        == "conflict"
    )
    assert not existing.conflict_nodes
    reader = next(
        tool
        for tool in build_read_tools(checker.contexts[0])
        if tool.name == "read_nodes"
    )
    preview = reader.invoke({"ids": [upstream.id], "view": "candidate"})
    assert preview["items"][0]["incoming_relations"][0]["from_node_id"] == consumer.id


def test_assessed_identity_correction_keeps_separate_original_nodes():
    incoming = state(metric("one", "same name", "1"), metric("two", "same name", "2"))
    proposer, checker = Consolidator(mismatch=True), Assessor()
    result = operator(proposer, checker).merge(state(), incoming, load_root_schema())
    assert len(proposer.contexts) == len(checker.contexts) == 1
    assert len(result.state.graph.nodes) == 2
    assert not result.state.conflict_nodes
    assert {n.properties["expression"]["expr"] for n in result.state.graph.nodes} == {
        "COUNT(*) + 1",
        "COUNT(*) + 2",
    }
    assert len({m.canonical_id for m in result.node_mappings}) == 2


def test_parent_checkpoint_retains_previously_committed_groups():
    incoming = state(
        metric("a1", "A", "1"),
        metric("a2", "A", "2"),
        metric("b1", "B", "3"),
        metric("b2", "B", "4"),
    )
    saver, assessor = InMemorySaver(), Assessor()
    first = Consolidator(fail_after=1)
    with pytest.raises(KeyboardInterrupt, match="committed group"):
        operator(first, assessor, saver).merge(
            state(), incoming, load_root_schema(), run_id="group-progress"
        )
    parent = saver.get_tuple(
        {"configurable": {"thread_id": "group-progress", "checkpoint_ns": ""}}
    )
    assert len(parent.checkpoint["channel_values"]["assembly"]["completed"]) == 1
    resumed = Consolidator()
    result = operator(resumed, Assessor(), saver).merge(
        state(), incoming, load_root_schema(), run_id="group-progress"
    )
    assert len(resumed.contexts) == 1
    assert resumed.contexts[0].group.members[0].content.name == "B"
    assert len(result.state.graph.nodes) == 2


def test_physical_owner_identity_is_remapped_before_matching_columns():
    def catalog(suffix):
        table = GraphNode(
            id=f"table-{suffix}",
            node_type="table",
            name="T",
            properties={"database": "db", "table_name": "T"},
        )
        column = GraphNode(
            id=f"column-{suffix}",
            node_type="column",
            name="id",
            properties={
                "table_ref": table.id,
                "column_name": "id",
                "data_type": "INTEGER",
            },
        )
        return state(table, column)

    original, incoming = catalog("existing"), catalog("incoming")
    result = operator(Consolidator(), Assessor()).merge(
        original, incoming, load_root_schema()
    )
    assert len(result.state.graph.nodes) == 2
    assert {m.input_id: m.canonical_id for m in result.node_mappings} == {
        "table-incoming": "table-existing",
        "column-incoming": "column-existing",
    }


def test_missing_relation_feedback_revises_candidate_before_assessment():
    upstream = metric("x", "X", "1")
    consumer = metric("y", "Y", "${x}", {"x": "x"})
    existing = state(upstream, consumer)
    incoming = state(metric("new-x", "X", "2"))

    class Revising(Consolidator):
        def __init__(self):
            super().__init__()
            self.feedback = []

        def invoke(self, context, **kwargs):
            self.feedback.append(kwargs.get("feedback"))
            result = super().invoke(context, **kwargs)
            if len(self.contexts) == 1:
                candidate = result.output.candidates[0]
                content = candidate.content.model_copy(
                    update={
                        "properties": {
                            "expression": {"expr": "SUM(${y})", "refs": {"y": "y"}},
                        }
                    }
                )
                return RoleResult(
                    result.output.model_copy(
                        update={
                            "candidates": (
                                candidate.model_copy(update={"content": content}),
                            )
                        }
                    ),
                    1,
                )
            return result

    proposer, checker = Revising(), Assessor()
    result = operator(proposer, checker).merge(existing, incoming, load_root_schema())
    assert "MISSING_FORMULA_RELATION" in proposer.feedback[1]
    assert len(checker.contexts) == 2
    assert (
        next(n for n in result.state.graph.nodes if n.id == "x").properties[
            "expression"
        ]["expr"]
        == "COUNT(*) + 1"
    )
    assert existing == state(upstream, consumer)


def test_existing_conflict_can_be_reviewed_into_new_normal_definition():
    first, second = metric("one", "X", "1"), metric("two", "X", "2")
    conflicted = (
        operator(Consolidator(conflict=True), Assessor())
        .merge(
            state(),
            state(first, second),
            load_root_schema(),
        )
        .state
    )
    node_id = conflicted.conflict_nodes[0].id

    class Fuse(Consolidator):
        def invoke(self, context, **kwargs):
            result = super().invoke(context, **kwargs)
            candidate = result.output.candidates[0]
            content = candidate.content.model_copy(
                update={
                    "properties": {
                        "expression": {"expr": "COUNT(*) + 3", "refs": {}},
                    }
                }
            )
            return RoleResult(
                result.output.model_copy(
                    update={
                        "candidates": (
                            candidate.model_copy(update={"content": content}),
                        )
                    }
                ),
                1,
            )

    result = operator(Fuse(), Assessor()).merge(
        conflicted,
        state(metric("new", "X", "3")),
        load_root_schema(),
    )
    assert not result.state.conflict_nodes
    node = next(n for n in result.state.graph.nodes if n.id == node_id)
    assert node.merge_status == "normal"
    assert node.properties["expression"]["expr"] == "COUNT(*) + 3"
    assert {s.source_id for s in node.sources} == {"one", "two", "new"}
    assert conflicted.conflict_nodes


def test_explicit_dependency_orders_and_reassesses_existing_rule():
    from cortex.schema import GraphEdge

    upstream = metric("z-upstream", "X", "1")
    rule = GraphNode(
        id="a-rule",
        node_type="generation_rule",
        name="Use X",
        properties={
            "condition": "The request needs X",
            "action": "Use X as requested",
            "reason": "Preserve the quantity",
            "triggers": {"question": {"phrases": ["X"]}},
        },
        sources=(SourceReference(source_id="rule", case_id="rule"),),
    )
    existing, _ = GraphManager(load_root_schema()).prepare_state(
        GraphState(
            graph=GraphSpace(
                nodes=(rule, upstream),
                edges=(
                    GraphEdge(
                        id="explicit-dependency",
                        edge_type="applies_to",
                        from_node_id=rule.id,
                        to_node_id=upstream.id,
                        properties={
                            "description": "This rule applies to the upstream quantity."
                        },
                    ),
                ),
            ),
            node_conflicts=(),
        )
    )
    proposer = Consolidator()
    operator(proposer, Assessor()).merge(
        existing, state(metric("new-x", "X", "2")), load_root_schema()
    )
    assert [context.group.node_type for context in proposer.contexts] == [
        "metric",
        "generation_rule",
    ]


@pytest.mark.parametrize("approve", [False, True])
def test_explicit_branch_scope_expansion_requires_assessment_approval(approve):
    from cortex.schema import GraphEdge

    table = GraphNode(
        id="table", node_type="table", name="T", properties={"table_name": "T"}
    )
    columns = tuple(
        GraphNode(
            id=key,
            node_type="column",
            name=key,
            properties={"table_ref": "table", "column_name": key},
        )
        for key in ("a", "b")
    )
    conflicted = (
        operator(Consolidator(conflict=True), Assessor())
        .merge(
            state(table, *columns),
            GraphState(
                graph=GraphSpace(
                    nodes=(
                        metric("one", "X", "${v}", {"v": "a"}),
                        metric("two", "X", "${v}", {"v": "b"}),
                    ),
                    edges=fixture_edges(
                        (
                            metric("one", "X", "${v}", {"v": "a"}),
                            metric("two", "X", "${v}", {"v": "b"}),
                        )
                    ),
                ),
                node_conflicts=(),
            ),
            load_root_schema(),
        )
        .state
    )
    owner = conflicted.conflict_nodes[0]
    selected = next(
        a
        for a in owner.alternatives
        if GraphManager.resolve_alternative(
            next(n for n in conflicted.graph.nodes if n.id == owner.id), a
        ).properties["expression"]["refs"]
        == {"v": "a"}
    )
    conflicted, _ = GraphManager(load_root_schema()).prepare_state(
        conflicted.model_copy(
            update={
                "graph": conflicted.graph.model_copy(
                    update={
                        "relation_contracts": (metric_context_contract(),),
                        "edges": (
                            *conflicted.graph.edges,
                            GraphEdge(
                                id="branch-fact",
                                edge_type="custom_measures",
                                from_node_id=owner.id,
                                to_node_id="a",
                                properties={
                                    "alternative_ids": [selected.alternative_id],
                                    "description": "This branch measures the specified field.",
                                },
                            ),
                        )
                    }
                )
            }
        )
    )

    class Fuse(Consolidator):
        def invoke(self, context, **kwargs):
            result = super().invoke(context, **kwargs)
            candidate = result.output.candidates[0]
            matching = next(
                m
                for m in context.group.members
                if m.content.properties["expression"]["refs"] == {"v": "a"}
            )
            return RoleResult(
                result.output.model_copy(
                    update={
                        "candidates": (
                            candidate.model_copy(update={"content": matching.content}),
                        )
                    }
                ),
                1,
            )

    class ScopeAssessor(Assessor):
        def invoke(self, context, **kwargs):
            result = super().invoke(context, **kwargs)
            return RoleResult(
                result.output.model_copy(
                    update={
                        "approved_relation_scope_changes": context.relation_scope_changes()
                        if approve
                        else ()
                    }
                ),
                1,
            )

    result = operator(Fuse(), ScopeAssessor()).merge(
        conflicted,
        GraphState(
            graph=GraphSpace(
                nodes=(metric("new", "X", "${v}", {"v": "a"}),),
                edges=fixture_edges((metric("new", "X", "${v}", {"v": "a"}),)),
            ),
            node_conflicts=(),
        ),
        load_root_schema(),
    )
    assert bool(result.state.conflict_nodes) is not approve
    relation = next(e for e in result.state.graph.edges if e.id == "branch-fact")
    assert bool(relation.properties.get("alternative_ids")) is not approve
