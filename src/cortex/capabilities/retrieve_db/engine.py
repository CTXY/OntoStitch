from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict
from collections.abc import Iterable

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel

from cortex.application.retrieve_db import (
    CandidateEvidence,
    ClarificationAnswer,
    ClarificationSlot,
    NodeCandidate,
    NodeSelection,
    RetrieveDbPending,
    RetrieveDbResult,
    RetrieveDbTraceStep,
    SqlSignals,
)
from cortex.capabilities.retrieve_db.closure import TypedGraphClosure
from cortex.capabilities.retrieve_db.graph_index import GraphIndex
from cortex.capabilities.retrieve_db.selection import CandidateSelector
from cortex.capabilities.retrieve_db.semantic import SemanticEmbeddingRecall
from cortex.capabilities.retrieve_db.signals import (
    GraphPhysicalSchemaRenderer,
    InitialSqlGenerator,
    MentionExtractor,
    SqlSignalExtractor,
)
from cortex.schema import GraphSpace, NodeMergeConflict
from cortex.schema.join_retrieval import join_path_candidates, semantic_tables_from_sql


class RetrieveDbCapability:
    def __init__(
        self,
        *,
        llm: BaseChatModel,
        mention_candidate_limit: int = 5,
        sql_literal_concept_limit: int = 10,
        sql_literal_value_limit: int = 10,
        generation_rule_limit: int = 10,
        edge_rule_per_seed_limit: int = 10,
        edge_rule_total_limit: int = 20,
        binding_concept_per_seed_limit: int = 10,
        binding_concept_total_limit: int = 20,
        embeddings: Embeddings | None = None,
        embedding_candidate_limit: int = 5,
        embedding_minimum_score: float = 0.2,
        closure_max_nodes: int = 50,
        closure_max_edges: int = 100,
        metric_dependency_depth: int = 4,
        graph_index_cache_size: int = 8,
        selection_policy: str = "training_recall",
    ) -> None:
        self.mention_candidate_limit = mention_candidate_limit
        self.sql_literal_concept_limit = sql_literal_concept_limit
        self.sql_literal_value_limit = sql_literal_value_limit
        self.generation_rule_limit = generation_rule_limit
        self.edge_rule_per_seed_limit = edge_rule_per_seed_limit
        self.edge_rule_total_limit = edge_rule_total_limit
        self.binding_concept_per_seed_limit = binding_concept_per_seed_limit
        self.binding_concept_total_limit = binding_concept_total_limit
        self.embedding_candidate_limit = embedding_candidate_limit
        self.embedding_minimum_score = embedding_minimum_score
        self.semantic_embedding = SemanticEmbeddingRecall(embeddings) if embeddings else None
        self.mention_extractor = MentionExtractor(llm)
        self.initial_sql_generator = InitialSqlGenerator(llm)
        self.sql_signal_extractor = SqlSignalExtractor()
        self.selector = CandidateSelector(llm, policy=selection_policy)
        self._selection_policy = selection_policy
        self.schema_renderer = GraphPhysicalSchemaRenderer()
        self.closure = TypedGraphClosure(
            max_nodes=closure_max_nodes,
            max_edges=closure_max_edges,
            metric_depth=metric_dependency_depth,
        )
        self._index_cache = _GraphIndexCache(max_size=graph_index_cache_size)

    async def retrieve(
        self,
        *,
        question: str,
        graph: GraphSpace,
        graph_fingerprint: str,
        dialect: str,
        initial_sql: str | None,
        enable_clarification: bool,
        node_conflicts: tuple[NodeMergeConflict, ...] = (),
    ) -> RetrieveDbResult:
        if node_conflicts and (self._selection_policy != "unattended" or enable_clarification):
            raise ValueError("reference conflicts require unattended, non-interactive retrieval")
        index = (
            GraphIndex.build(graph, node_conflicts=node_conflicts)
            if node_conflicts
            else self._index_cache.get(graph_fingerprint, graph)
        )
        mentions_task = self.mention_extractor.extract(question, index)
        sql_task = (
            _provided(initial_sql)
            if initial_sql is not None
            else self.initial_sql_generator.generate(
                question=question,
                schema_context=self.schema_renderer.render(graph),
                dialect=dialect,
            )
        )
        signal_tasks = [asyncio.create_task(task) for task in (mentions_task, sql_task)]
        try:
            mentions, resolved_sql = await asyncio.gather(*signal_tasks)
        except BaseException:
            for task in signal_tasks:
                task.cancel()
            await asyncio.gather(*signal_tasks, return_exceptions=True)
            raise
        sql_signals = self.sql_signal_extractor.extract(
            resolved_sql,
            dialect=dialect,
            operations=(
                operation
                for configured in index.rule_operations.values()
                for operation in configured
            ),
        )
        sql_column_ids = _resolve_sql_column_ids(index, sql_signals)
        explicit_columns = index.recall_explicit_columns(
            question,
            limit=self.mention_candidate_limit,
        )
        embedding_candidates = (
            await self.semantic_embedding.recall(
                question=question,
                index=index,
                graph_fingerprint=graph_fingerprint,
                limit=self.embedding_candidate_limit,
                minimum_score=self.embedding_minimum_score,
            )
            if self.semantic_embedding is not None
            else ()
        )
        mention_candidates, schema_candidates, value_candidates, early_rules = await asyncio.gather(
            _recall_mentions(index, mentions, self.mention_candidate_limit),
            _resolve_schema(index, sql_signals),
            _resolve_values(
                index,
                sql_signals,
                concept_limit=self.sql_literal_concept_limit,
                value_limit=self.sql_literal_value_limit,
            ),
            _async_value(
                index.recall_generation_rules(
                    question=question,
                    operations=sql_signals.operations,
                    reference_node_ids=(),
                    limit=self.generation_rule_limit,
                    column_node_ids=sql_column_ids,
                )
            ),
        )
        base_candidates = _merge_candidates(
            (
                *mention_candidates,
                *embedding_candidates,
                *explicit_columns,
                *schema_candidates,
                *value_candidates,
                *early_rules,
            )
        )
        binding_concepts = index.recall_concept_superset(
            candidates=base_candidates,
            per_seed_limit=self.binding_concept_per_seed_limit,
            total_limit=self.binding_concept_total_limit,
        )
        semantic_candidates = _merge_candidates((*base_candidates, *binding_concepts))
        edge_rules = index.recall_generation_rule_superset(
            candidates=semantic_candidates,
            per_seed_limit=self.edge_rule_per_seed_limit,
            total_limit=self.edge_rule_total_limit,
        )
        candidates = _merge_candidates((*semantic_candidates, *edge_rules))
        selections = await self.selector.select(
            question=question,
            initial_sql=resolved_sql,
            candidates=candidates,
            concept_value_ids=_concept_value_ids(index, candidates),
            candidate_nodes={item.node_id: index.node_by_id[item.node_id] for item in candidates},
            **({"node_conflicts": index.reference_conflicts} if index.reference_conflicts else {}),
        )
        ambiguous = {item.candidate_id for item in selections if item.status == "ambiguous"}
        if ambiguous and enable_clarification:
            slots = _clarification_slots(mentions, candidates, ambiguous)
            pending = RetrieveDbPending(
                question=question,
                initial_sql=resolved_sql,
                graph_fingerprint=graph_fingerprint,
                mentions=mentions,
                sql_signals=sql_signals,
                candidates=candidates,
                selections=selections,
                clarification_slots=slots,
            )
            return RetrieveDbResult(
                status="needs_clarification",
                question=question,
                initial_sql=resolved_sql,
                graph_fingerprint=graph_fingerprint,
                mentions=mentions,
                sql_signals=sql_signals,
                candidates=candidates,
                selections=selections,
                raw_retrieved_node_ids=tuple(sorted({item.node_id for item in candidates})),
                selected_node_ids=_selected_ids(candidates, selections),
                clarification_slots=slots,
                pending=pending,
                trace=_trace(mentions, candidates, selections, resolved_sql),
            )
        selected = _selected_ids(candidates, selections)
        return self._terminal(
            question=question,
            initial_sql=resolved_sql,
            graph_fingerprint=graph_fingerprint,
            mentions=mentions,
            sql_signals=sql_signals,
            candidates=candidates,
            selections=selections,
            selected=selected,
            index=index,
        )

    async def resume(
        self,
        *,
        graph: GraphSpace,
        pending: RetrieveDbPending,
        answers: tuple[ClarificationAnswer, ...],
    ) -> RetrieveDbResult:
        answers_by_slot = {item.slot_id: item for item in answers}
        candidate_by_node = {item.node_id: item for item in pending.candidates}
        replacements = {
            candidate_by_node[answers_by_slot[slot.slot_id].selected_node_id].candidate_id
            for slot in pending.clarification_slots
        }
        selections = tuple(
            item.model_copy(update={"status": "selected", "reason": "selected by clarification"})
            if item.candidate_id in replacements
            else item.model_copy(update={"status": "rejected"})
            if item.status == "ambiguous"
            else item
            for item in pending.selections
        )
        index = self._index_cache.get(pending.graph_fingerprint, graph)
        selected = _selected_ids(pending.candidates, selections)
        return self._terminal(
            question=pending.question,
            initial_sql=pending.initial_sql,
            graph_fingerprint=pending.graph_fingerprint,
            mentions=pending.mentions,
            sql_signals=pending.sql_signals,
            candidates=pending.candidates,
            selections=selections,
            selected=selected,
            index=index,
        )

    def _terminal(
        self,
        *,
        question: str,
        initial_sql: str | None,
        graph_fingerprint: str,
        mentions: tuple,
        sql_signals: SqlSignals | None,
        candidates: tuple[NodeCandidate, ...],
        selections: tuple[NodeSelection, ...],
        selected: tuple[str, ...],
        index: GraphIndex,
    ) -> RetrieveDbResult:
        graph = GraphSpace(
            nodes=tuple(index.node_by_id.values()),
            edges=tuple(index.edge_by_id.values()),
        )
        if self._selection_policy == "unattended":
            # Initial SQL is recall evidence, not authority for terminal dependencies.
            dependencies = self.closure.expand(index, selected, []) if selected else None
            semantic_tables = (
                {node.id for node in dependencies.graph.nodes if node.node_type == "table"}
                if dependencies
                else set()
            )
            join_candidates = [
                {
                    **candidate,
                    "joinability_rule_id": (
                        candidate["joinability_rule_id"]
                        if candidate["joinability_rule_id"] in selected
                        else None
                    ),
                }
                for candidate in join_path_candidates(graph, semantic_tables)
            ]
        else:
            semantic_tables = semantic_tables_from_sql(initial_sql, graph)
            join_candidates = join_path_candidates(graph, semantic_tables)
        if not candidates and not join_candidates and not selected:
            status = "no_match"
        elif not selected and not join_candidates:
            status = "unresolved"
        else:
            status = "completed"
        closure = (
            self.closure.expand(index, selected, join_candidates) if status == "completed" else None
        )
        rule_ids = tuple(
            node_id
            for node_id in selected
            if index.node_by_id[node_id].node_type == "generation_rule"
        )
        relevant_asset_ids = tuple(
            node_id
            for node_id in selected
            if index.node_by_id[node_id].node_type in {"concept", "metric", "generation_rule"}
        )
        return RetrieveDbResult(
            status=status,
            question=question,
            initial_sql=initial_sql,
            graph_fingerprint=graph_fingerprint,
            mentions=mentions,
            sql_signals=sql_signals,
            candidates=candidates,
            selections=selections,
            raw_retrieved_node_ids=tuple(sorted({item.node_id for item in candidates})),
            selected_node_ids=selected,
            relevant_ontology_asset_node_ids=relevant_asset_ids,
            dependency_node_ids=closure.dependency_node_ids if closure else (),
            generation_rule_node_ids=rule_ids,
            seed_node_ids=selected,
            subgraph=closure.graph if closure else None,
            node_conflicts=tuple(
                item
                for item in index.reference_conflicts.values()
                if closure and item.node_id in {n.id for n in closure.graph.nodes}
            ),
            paths=closure.paths if closure else (),
            trace=_trace(mentions, candidates, selections, initial_sql),
        )


async def _provided(value: str) -> str:
    return value


async def _async_value(value):
    return value


async def _recall_mentions(
    index: GraphIndex, mentions: tuple, limit: int
) -> tuple[NodeCandidate, ...]:
    return tuple(
        candidate
        for mention in mentions
        if mention.role != "operator"
        for candidate in index.recall(mention, limit=limit)
    )


async def _resolve_schema(index: GraphIndex, signals: SqlSignals) -> tuple[NodeCandidate, ...]:
    candidates = []
    table_ids: dict[str, tuple[str, ...]] = {}
    for table_name in signals.table_names:
        table_ids[table_name] = index.resolve_table(table_name)
        candidates.extend(
            _direct_candidates(index, table_ids[table_name], "sql_schema", table_name)
        )
    for reference in signals.column_refs:
        owners = table_ids.get(reference.table_name or "", ())
        ids = tuple(
            node_id
            for owner in owners or (None,)
            for node_id in index.resolve_column(reference.column_name, table_id=owner)
        )
        candidates.extend(_direct_candidates(index, ids, "sql_schema", reference.column_name))
    return tuple(candidates)


def _resolve_sql_column_ids(index: GraphIndex, signals: SqlSignals) -> tuple[str, ...]:
    table_ids = {table_name: index.resolve_table(table_name) for table_name in signals.table_names}
    resolved = {
        node_id
        for reference in signals.column_refs
        for owner in table_ids.get(reference.table_name or "", ()) or (None,)
        for node_id in index.resolve_column(reference.column_name, table_id=owner)
    }
    return tuple(sorted(resolved))


async def _resolve_values(
    index: GraphIndex,
    signals: SqlSignals,
    *,
    concept_limit: int,
    value_limit: int,
) -> tuple[NodeCandidate, ...]:
    candidates = []
    for literal in signals.literals:
        value_ids = index.resolve_dimension_value(literal.value)[:value_limit]
        candidates.extend(
            _direct_candidates(index, value_ids, "sql_literal_global_value", literal.value)
        )
        for concept_id, matched_field in index.resolve_concepts(literal.value)[:concept_limit]:
            candidates.extend(
                _direct_candidates(
                    index,
                    (concept_id,),
                    f"sql_literal_global_concept_{matched_field}",
                    literal.value,
                )
            )
    return tuple(candidates)


def _direct_candidates(
    index: GraphIndex, node_ids: Iterable[str], channel: str, detail: str
) -> list[NodeCandidate]:
    result = []
    for node_id in node_ids:
        node = index.node_by_id[node_id]
        result.append(
            NodeCandidate(
                candidate_id=f"candidate:{channel}:{node_id}",
                node_id=node.id,
                node_type=node.node_type,
                name=node.name,
                channels=(channel,),
                score=1.0,
                evidence=(CandidateEvidence(channel=channel, detail=detail),),
                neighbor_summary=index.neighbor_summary(node.id),
            )
        )
    return result


def _merge_candidates(candidates: tuple[NodeCandidate, ...]) -> tuple[NodeCandidate, ...]:
    grouped: dict[str, list[NodeCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.node_id].append(candidate)
    result = []
    for node_id, values in sorted(grouped.items()):
        first = values[0]
        result.append(
            first.model_copy(
                update={
                    "candidate_id": f"candidate:node:{node_id}",
                    "mention_ids": tuple(sorted({x for item in values for x in item.mention_ids})),
                    "channels": tuple(sorted({x for item in values for x in item.channels})),
                    "score": max(item.score for item in values),
                    "evidence": tuple(dict.fromkeys(x for item in values for x in item.evidence)),
                }
            )
        )
    return tuple(result)


def _selected_ids(
    candidates: tuple[NodeCandidate, ...], selections: tuple[NodeSelection, ...]
) -> tuple[str, ...]:
    selected_candidates = {item.candidate_id for item in selections if item.status == "selected"}
    return tuple(
        sorted(item.node_id for item in candidates if item.candidate_id in selected_candidates)
    )


def _concept_value_ids(
    index: GraphIndex, candidates: tuple[NodeCandidate, ...]
) -> dict[str, frozenset[str]]:
    return {
        candidate.node_id: frozenset(
            edge.to_node_id
            for edge in index.outgoing_edges.get(candidate.node_id, ())
            if edge.edge_type == "maps_to_value"
            and index.node_by_id[edge.to_node_id].node_type == "dimension_value"
        )
        for candidate in candidates
        if candidate.node_type == "concept"
    }


def _clarification_slots(mentions, candidates, ambiguous) -> tuple[ClarificationSlot, ...]:
    result = []
    by_id = {item.mention_id: item for item in mentions}
    for mention_id in sorted(
        {mid for item in candidates if item.candidate_id in ambiguous for mid in item.mention_ids}
    ):
        options = tuple(item for item in candidates if mention_id in item.mention_ids)
        result.append(
            ClarificationSlot(
                slot_id=f"clarify:{mention_id}",
                mention_id=mention_id,
                question=f"请选择“{by_id[mention_id].text}”对应的业务含义",
                candidates=options,
            )
        )
    covered = {candidate.candidate_id for slot in result for candidate in slot.candidates}
    for candidate in candidates:
        if candidate.candidate_id not in ambiguous or candidate.candidate_id in covered:
            continue
        result.append(
            ClarificationSlot(
                slot_id=f"clarify:{candidate.candidate_id}",
                mention_id=f"candidate:{candidate.node_id}",
                question=f"是否选择候选资产“{candidate.name}”？",
                candidates=(candidate,),
            )
        )
    return tuple(result)


def _trace(mentions, candidates, selections, initial_sql) -> tuple[RetrieveDbTraceStep, ...]:
    return (
        RetrieveDbTraceStep(
            action="parallel_signal_generation",
            observation=f"mentions={len(mentions)}, initial_sql={bool(initial_sql)}",
        ),
        RetrieveDbTraceStep(action="candidate_recall", observation=f"candidates={len(candidates)}"),
        RetrieveDbTraceStep(
            action="llm_selection",
            observation=f"selected={sum(x.status == 'selected' for x in selections)}",
        ),
    )


__all__ = ["RetrieveDbCapability"]


class _GraphIndexCache:
    def __init__(self, *, max_size: int) -> None:
        if max_size < 1:
            raise ValueError("graph index cache size must be positive")
        self._max_size = max_size
        self._values: OrderedDict[str, GraphIndex] = OrderedDict()

    def get(self, fingerprint: str, graph: GraphSpace) -> GraphIndex:
        cached = self._values.get(fingerprint)
        if cached is not None:
            self._values.move_to_end(fingerprint)
            return cached
        index = GraphIndex.build(graph)
        self._values[fingerprint] = index
        self._values.move_to_end(fingerprint)
        while len(self._values) > self._max_size:
            self._values.popitem(last=False)
        return index
