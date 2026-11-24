"""Deterministic graph assembly around structured relevance judgments."""

from __future__ import annotations

import json

from cortex.application.retrieval import (
    ColumnSemanticContext,
    RelevanceDecision,
    RetrievedRepresentation,
)
from cortex.application.retrieve_db import (
    CandidateEvidence,
    NodeCandidate,
    NodeSelection,
    RetrieveDbResult,
    RetrieveDbTraceStep,
)
from cortex.capabilities.retrieve_db.graph.context import (
    build_retrieved_graph_state,
    select_context,
    without_untrusted_dimension_values,
)
from cortex.capabilities.retrieve_db.retrieval.index import RepresentationIndex
from cortex.schema import GraphState


class RetrievalNodes:
    def __init__(self, *, recall, selector, initial_sql_generator, schema_renderer,
                 semantic_node_limit=30, physical_node_limit=50):
        if any(type(n) is not int or n < 1 for n in (semantic_node_limit, physical_node_limit)):
            raise ValueError('Final retrieval node limits must be positive integers')
        self.typed_recall = recall
        self.selector = selector
        self.initial_sql_generator = initial_sql_generator
        self.schema_renderer = schema_renderer
        self.semantic_node_limit = semantic_node_limit
        self.physical_node_limit = physical_node_limit

    async def signals(self, state):
        request = state["request"]
        sql = request["initial_sql"]
        if sql is None and self.initial_sql_generator is not None:
            graph = GraphState.model_validate(request["graph_state"])
            sql = await self.initial_sql_generator.generate(
                question=request["question"],
                schema_context=self.schema_renderer.render(graph.graph),
                dialect=request["dialect"],
            )
        return {"initial_sql": read_only_sql(sql, request["dialect"])}

    async def recall(self, state):
        request = state["request"]
        source = without_untrusted_dimension_values(
            GraphState.model_validate(request["graph_state"])
        )
        index = RepresentationIndex.build(
            source,
            column_context=tuple(
                ColumnSemanticContext.model_validate(item)
                for item in request["column_context"]
            ),
        )
        recalled = await self.typed_recall.recall(
            index, request["question"], state["initial_sql"]
        )
        return {"representations": [item.model_dump(mode="json") for item in recalled]}

    async def select(self, state):
        items = tuple(
            RetrievedRepresentation.model_validate(item)
            for item in state["representations"]
        )
        decisions = await self.selector.select(
            question=state["request"]["question"],
            initial_sql=state["initial_sql"],
            representations=items,
        )
        expected = {item.document.document_id for item in items}
        if (
            len(decisions) != len(expected)
            or {item.document_id for item in decisions} != expected
        ):
            raise ValueError(
                "relevance decisions must cover every retrieved representation exactly once"
            )
        return {"relevance": [item.model_dump(mode="json") for item in decisions]}

    def assemble(self, state):
        request = state["request"]
        source = GraphState.model_validate(request["graph_state"])
        items = tuple(
            RetrievedRepresentation.model_validate(item)
            for item in state["representations"]
        )
        relevance = tuple(
            RelevanceDecision.model_validate(item) for item in state["relevance"]
        )
        decisions = {item.document_id: item for item in relevance}
        selected = {
            item.document.canonical_node_id
            for item in items
            if decisions[item.document.document_id].status in {"relevant", "uncertain"}
            or protected(item)
        }
        semantic = {
            item.document.canonical_node_id
            for item in items
            if decisions[item.document.document_id].status == "relevant"
            and item.document.node_type in {"concept", "event", "metric", "generation_rule"}
        }
        grounding = {
            item.document.canonical_node_id
            for item in items
            if decisions[item.document.document_id].status == "relevant"
            and item.document.node_type == "dimension_value"
        }
        ordered = sorted(items, key=lambda item: (
            -int('initial_sql_exact' in item.channels),
            -int('question_exact' in item.channels),
            -item.score, item.document.canonical_node_id, item.document.document_id))
        budget = select_context(source, selected,
                ranked_seed_ids=[item.document.canonical_node_id for item in ordered],
                # Do not undo the model's relevance decision through a hub.
                # Mandatory formula/binding references are still completed.
                excluded_node_ids={item.document.canonical_node_id for item in items} - selected,
                semantic_node_limit=self.semantic_node_limit,
                physical_node_limit=self.physical_node_limit)
        selected = budget.seed_ids
        semantic.intersection_update(selected)
        grounding.intersection_update(selected)
        snapshot = (
            build_retrieved_graph_state(source, budget.node_ids)
            if selected
            else None
        )
        nodes = {node.id: node for node in source.graph.nodes}
        candidates = tuple(
            NodeCandidate(
                candidate_id=item.document.document_id,
                node_id=item.document.canonical_node_id,
                node_type=item.document.node_type,
                name=nodes[item.document.canonical_node_id].name,
                channels=item.channels,
                score=item.score,
                evidence=tuple(
                    CandidateEvidence(
                        channel=channel,
                        detail=decisions[item.document.document_id].reason,
                    )
                    for channel in item.channels
                ),
            )
            for item in items
        )
        selections = tuple(
            NodeSelection(
                candidate_id=item.document.document_id,
                status="selected"
                if item.document.canonical_node_id in selected
                else "rejected",
                role="rule"
                if item.document.node_type == "generation_rule"
                else "value"
                if item.document.node_type == "dimension_value"
                else "schema"
                if item.document.node_type in {"table", "column"}
                else "direct",
                reason=decisions[item.document.document_id].reason + (
                    '; excluded by final subgraph node budget (complete dependencies retained for admitted units)'
                    if item.document.canonical_node_id not in selected and item.document.canonical_node_id in budget.skipped else ''),
            )
            for item in items
        )
        retained = {node.id for node in snapshot.graph.nodes} if snapshot else set()
        result = RetrieveDbResult(
            status="completed" if snapshot else "no_match",
            question=request["question"],
            initial_sql=state["initial_sql"],
            graph_fingerprint=request["graph_fingerprint"],
            run_id=request["run_id"],
            candidates=candidates,
            selections=selections,
            raw_retrieved_node_ids=tuple(sorted({item.node_id for item in candidates})),
            selected_node_ids=tuple(sorted(selected)),
            relevant_ontology_asset_node_ids=tuple(
                sorted(
                    ref
                    for ref in selected
                    if nodes[ref].node_type in {"concept", "event", "metric", "generation_rule"}
                )
            ),
            dependency_node_ids=tuple(sorted(retained - selected)),
            seed_node_ids=tuple(sorted(selected)),
            generation_rule_node_ids=tuple(
                sorted(
                    ref for ref in retained if nodes[ref].node_type == "generation_rule"
                )
            ),
            retrieved_graph_state=snapshot,
            representations=items,
            relevance=relevance,
            relevant_semantic_node_ids=tuple(sorted(semantic)),
            relevant_grounding_node_ids=tuple(sorted(grounding)),
            trace=(
                RetrieveDbTraceStep(
                    action="typed_recall", observation=f"{len(items)} representations"
                ),
                RetrieveDbTraceStep(
                    action="dependency_closure", observation=f"{len(retained)} nodes"
                ),
                RetrieveDbTraceStep(action='final_node_budget', observation=json.dumps({
                    'semantic_nodes': budget.semantic_nodes, 'semantic_limit': self.semantic_node_limit,
                    'physical_nodes': budget.physical_nodes, 'physical_limit': self.physical_node_limit,
                    'skipped_complete_units': budget.skipped,
                    'rank_order': 'initial_sql_exact, question_exact, recall_score, stable_node_id'}, sort_keys=True)),
            ),
        )
        return {"result": result.model_dump(mode="json")}


def protected(item):
    return bool(
        set(item.channels) & {"question_exact", "initial_sql_exact", "value_owner"}
    )


def read_only_sql(sql, dialect):
    if not sql or not sql.strip():
        return None
    import sqlglot
    from sqlglot import expressions

    try:
        trees = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    if len(trees) != 1 or not isinstance(trees[0], expressions.Query):
        return None
    if any(
        isinstance(
            node,
            (
                expressions.Insert,
                expressions.Update,
                expressions.Delete,
                expressions.Create,
                expressions.Drop,
                expressions.Command,
            ),
        )
        for node in trees[0].walk()
    ):
        return None
    return sql.strip()
