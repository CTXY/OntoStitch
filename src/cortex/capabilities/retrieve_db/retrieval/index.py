from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import expressions

from cortex.application.ontology_graph import GraphManager
from cortex.application.retrieval import (
    ColumnSemanticContext,
    RetrievalDocument,
    RetrievedRepresentation,
)
from cortex.capabilities.retrieve_db.retrieval.normalization import (
    normalize_text,
    tokenize,
)
from cortex.capabilities.retrieve_db.rule_signals import generation_rule_signals
from cortex.schema import GraphNode, GraphState, alternative_sources
from cortex.schema.bindings import target_views


@dataclass(frozen=True)
class RepresentationIndex:
    documents: tuple[RetrievalDocument, ...]
    node_by_id: dict[str, GraphNode]
    documents_by_node: dict[str, tuple[RetrievalDocument, ...]]
    conflicts_by_node: dict[str, GraphNode]

    @classmethod
    def build(
        cls,
        state: GraphState,
        *,
        column_context: tuple[ColumnSemanticContext, ...] = (),
    ) -> RepresentationIndex:
        documents: list[RetrievalDocument] = []
        node_by_id = {node.id: node for node in state.graph.nodes}
        contexts_by_ref = _index_column_contexts(column_context, node_by_id)
        conflict_by_id = {item.id: item for item in state.conflict_nodes}
        for node in state.graph.nodes:
            documents.append(_canonical_document(node, contexts_by_ref, node_by_id))
            conflict = conflict_by_id.get(node.id)
            if conflict is not None:
                for alternative in conflict.alternatives:
                    documents.append(
                        _content_document(
                            node,
                            GraphManager.resolve_alternative(
                                node, alternative
                            ).model_dump(mode="json"),
                            document_id=f"alternative:{alternative.alternative_id}",
                            kind="conflict_alternative",
                            alternative_id=alternative.alternative_id,
                            contexts=_contexts_for(
                                GraphManager.resolve_alternative(
                                    node, alternative
                                ).model_dump(mode="json"),
                                contexts_by_ref,
                            ),
                        ).model_copy(
                            update={
                                "structured_signals": {
                                    "alternative_id": alternative.alternative_id,
                                    "common_node_id": node.id,
                                    "overrides": alternative.overrides,
                                    "sources": [
                                        source.model_dump(mode="json")
                                        for source in alternative_sources(node, alternative)
                                    ],
                                    **({"common_sources": [source.model_dump(mode="json") for source in node.common_sources]}
                                       if node.common_sources else {}),
                                }
                            }
                        )
                    )
                continue
            if node.node_type == "concept":
                for index, target in enumerate(target_views(node.properties), 1):
                    target_id = str(target.get("id") or f"target-{index}")
                    documents.append(
                        RetrievalDocument(
                            document_id=f"target:{node.id}:{index}:{target_id}",
                            canonical_node_id=node.id,
                            node_type=node.node_type,
                            representation_kind="concept_target",
                            target_id=target_id,
                            searchable_text=_searchable(node.name, target),
                            structured_signals={
                                "target": target,
                                "column_contexts": _contexts_for(
                                    target, contexts_by_ref
                                ),
                            },
                        )
                    )
            elif node.node_type == "metric":
                expression = node.properties.get("expression")
                if isinstance(expression, dict):
                    contexts = _contexts_for(expression, contexts_by_ref)
                    documents.append(
                        RetrievalDocument(
                            document_id=f"expression:{node.id}",
                            canonical_node_id=node.id,
                            node_type=node.node_type,
                            representation_kind="metric_expression",
                            searchable_text=_searchable(
                                node.name, expression, contexts
                            ),
                            structured_signals={
                                "expression": expression,
                                "column_contexts": contexts,
                            },
                        )
                    )
                for index, plan in enumerate(
                    _list(node.properties.get("sql_generation_plans")), 1
                ):
                    plan_id = str(plan.get("id") or f"plan-{index}")
                    contexts = _contexts_for(plan, contexts_by_ref)
                    documents.append(
                        RetrievalDocument(
                            document_id=f"plan:{node.id}:{index}:{plan_id}",
                            canonical_node_id=node.id,
                            node_type=node.node_type,
                            representation_kind="metric_plan",
                            plan_id=plan_id,
                            searchable_text=_searchable(node.name, plan, contexts),
                            structured_signals={
                                "plan": plan,
                                "column_contexts": contexts,
                            },
                        )
                    )
        by_node: dict[str, list[RetrievalDocument]] = defaultdict(list)
        for document in documents:
            by_node[document.canonical_node_id].append(document)
        return cls(
            documents=tuple(documents),
            node_by_id=node_by_id,
            documents_by_node={key: tuple(value) for key, value in by_node.items()},
            conflicts_by_node=conflict_by_id,
        )

    def recall(
        self,
        *,
        question: str,
        initial_sql: str | None,
        limit: int = 80,
    ) -> tuple[RetrievedRepresentation, ...]:
        query = normalize_text(question)
        question_tokens = tokenize(question)
        sql_text = normalize_text(initial_sql or "")
        sql_identifiers = _sql_identifiers(initial_sql)
        try:
            sql_tree = sqlglot.parse_one(initial_sql) if initial_sql else None
        except sqlglot.errors.ParseError:
            sql_tree = None
        sql_tables = (
            {normalize_text(t.name) for t in sql_tree.find_all(expressions.Table)}
            if sql_tree is not None
            else set()
        )
        sql_values = (
            {
                literal.this
                for literal in sql_tree.find_all(expressions.Literal)
                if literal.is_string
            }
            if sql_tree is not None
            else set()
        )
        ranked: list[RetrievedRepresentation] = []
        for document in self.documents:
            text = normalize_text(document.searchable_text)
            tokens = tokenize(text)
            overlap = len(tokens & question_tokens) / max(1, len(question_tokens))
            exact = bool(
                query
                and any(
                    _label_matches_question(label, question_tokens)
                    for label in _labels(document)
                )
            )
            sql_hit = bool(
                sql_text
                and any(label in sql_text for label in _physical_labels(document))
            )
            content = document.structured_signals.get("content", {})
            properties = content.get("properties", {})
            # A value is not an exact SQL hit merely because its owning column is used.
            exact_sql_hit = (
                document.node_type == "column"
                and normalize_text(properties.get("column_name") or content.get("name"))
                in sql_identifiers
            )
            if document.node_type == "table":
                exact_sql_hit = (
                    normalize_text(properties.get("table_name") or content.get("name"))
                    in sql_tables
                )
            if document.node_type == "dimension_value":
                value = str(properties.get("value", content.get("name", "")))
                exact_sql_hit = value in sql_values
            sql_hit = sql_hit or exact_sql_hit
            if not exact and not sql_hit and overlap < 0.08:
                continue
            score = min(
                1.0, (0.65 if exact else 0.0) + (0.2 if sql_hit else 0.0) + overlap
            )
            channels = tuple(
                channel
                for condition, channel in (
                    (exact, "question_exact"),
                    (overlap >= 0.08, "question_tokens"),
                    (sql_hit, "initial_sql"),
                    (exact_sql_hit, "initial_sql_exact"),
                )
                if condition
            )
            ranked.append(
                RetrievedRepresentation(
                    document=document, score=score, channels=channels
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.document.document_id))
        return tuple(ranked[:limit])


def _index_column_contexts(
    contexts: tuple[ColumnSemanticContext, ...], nodes: dict[str, GraphNode]
) -> dict[str, ColumnSemanticContext]:
    """Prefer exact identifiers; repair unambiguous CSV padding against the catalog."""
    physical = {}
    for node in nodes.values():
        table = nodes.get(node.properties.get("table_ref"))
        if node.node_type == "column" and table is not None and table.node_type == "table":
            physical[
                (
                    table.properties["table_name"].casefold(),
                    node.properties["column_name"].casefold(),
                )
            ] = (table, node)
    normalized_physical = defaultdict(list)
    for key in physical:
        normalized_physical[tuple(part.strip() for part in key)].append(key)
    indexed = {(c.table_name.casefold(), c.column_name.casefold()): c for c in contexts}
    candidates = defaultdict(list)
    for key, context in indexed.items():
        if key not in physical:
            candidates[tuple(part.strip() for part in key)].append(context)
    for key, matches in candidates.items():
        owners = normalized_physical.get(key, ())
        if len(matches) == len(owners) == 1 and owners[0] not in indexed:
            table, column = physical[owners[0]]
            context = matches[0]
            indexed.pop((context.table_name.casefold(), context.column_name.casefold()))
            indexed[owners[0]] = context.model_copy(
                update={
                    "table_name": table.properties["table_name"],
                    "column_name": column.properties["column_name"],
                    "column_id": column.id,
                }
            )
    return {f"{table}.{column}": context for (table, column), context in indexed.items()}


def _canonical_document(
    node: GraphNode, contexts_by_ref: dict[str, ColumnSemanticContext], nodes: dict
) -> RetrievalDocument:
    kind = (
        "generation_rule" if node.node_type == "generation_rule" else "canonical_node"
    )
    content = {
        "name": node.name,
        "description": node.description,
        "aliases": node.aliases,
        "properties": node.properties,
    }
    contexts = _contexts_for(content, contexts_by_ref)
    direct_context = _column_context(node, contexts_by_ref, nodes)
    if direct_context is not None and direct_context not in contexts:
        contexts = (*contexts, direct_context)
    return _content_document(
        node,
        content,
        document_id=f"node:{node.id}",
        kind=kind,
        contexts=contexts,
    )


def _column_context(
    node: GraphNode, contexts_by_ref: dict[str, ColumnSemanticContext], nodes: dict
) -> dict[str, Any] | None:
    if node.node_type != "column":
        return None
    table = nodes.get(node.properties.get("table_ref"))
    column_name = str(node.properties.get("column_name") or node.name)
    if table is None or table.node_type != "table" or not column_name:
        return None
    table_name = table.properties["table_name"]
    context = contexts_by_ref.get(
        f"{table_name}.{column_name}".casefold()
    )
    return context.model_dump(mode="json") if context is not None else None


def _content_document(
    node: GraphNode,
    content: dict[str, Any],
    *,
    document_id: str,
    kind: str,
    alternative_id: str | None = None,
    contexts: tuple[dict[str, Any], ...] = (),
) -> RetrievalDocument:
    return RetrievalDocument(
        document_id=document_id,
        canonical_node_id=node.id,
        node_type=node.node_type,
        representation_kind=kind,
        alternative_id=alternative_id,
        searchable_text=_searchable(content, contexts),
        structured_signals={"content": content, "column_contexts": contexts},
    )


def _searchable(*values: Any) -> str:
    return " ".join(
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        if not isinstance(value, str)
        else value
        for value in values
        if value is not None
    )


def _labels(document: RetrievalDocument) -> tuple[str, ...]:
    signals = document.structured_signals
    content = signals.get("content")
    if content is None and "overrides" in signals:
        changes = signals["overrides"]
        content = {
            "name": changes.get("name", ""),
            "aliases": changes.get("aliases", []),
            "properties": {"triggers": changes.get("properties.triggers")},
        }
    target = signals.get("target")
    labels: list[str] = []
    if isinstance(content, dict):
        labels.extend(
            [str(content.get("name") or ""), *map(str, content.get("aliases") or [])]
        )
        properties = content.get("properties") or {}
        triggers = properties.get("triggers") if isinstance(properties, dict) else None
        phrases, _, _ = generation_rule_signals(triggers)
        labels.extend(map(str, phrases))
    if isinstance(target, dict):
        labels.append(str(target.get("expression") or ""))
    return tuple(label for label in labels if _meaningful(label))


def _physical_labels(document: RetrievalDocument) -> tuple[str, ...]:
    values = []
    for key in ("content", "target", "expression", "plan", "overrides"):
        value = document.structured_signals.get(key)
        if value is not None:
            values.extend(tokenize(json.dumps(value, ensure_ascii=False)))
    return tuple(item for item in values if "." in item or "_" in item or len(item) > 3)


def _sql_identifiers(sql: str | None) -> set[str]:
    if not sql:
        return set()
    try:
        tree = sqlglot.parse_one(sql)
    except sqlglot.errors.ParseError:
        return set()
    return {
        normalize_text(column.name)
        for column in tree.find_all(expressions.Column)
        if column.name
    }


def _contexts_for(
    value: Any, contexts_by_ref: dict[str, ColumnSemanticContext]
) -> tuple[dict[str, Any], ...]:
    serialized = json.dumps(value, ensure_ascii=False)
    lowered = serialized.casefold()
    return tuple(
        context.model_dump(mode="json")
        for reference, context in contexts_by_ref.items()
        if reference in lowered or f"column.{reference}" in lowered
    )


def _meaningful(value: str) -> str:
    normalized = normalize_text(value)
    return normalized if len(normalized) >= 2 else ""


def _label_matches_question(label: str, question_tokens: frozenset[str]) -> bool:
    meaningful = _meaningful(label)
    if not meaningful:
        return False
    label_tokens = tokenize(meaningful)
    return bool(label_tokens) and label_tokens <= question_tokens


def _list(value: Any) -> list[dict[str, Any]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


__all__ = ["RepresentationIndex"]
