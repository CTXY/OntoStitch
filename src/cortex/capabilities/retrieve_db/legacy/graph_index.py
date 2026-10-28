from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from cortex.application.ontology_graph import GraphManager
from cortex.application.retrieve_db import (
    CandidateEvidence,
    EntityMention,
    NodeCandidate,
)
from cortex.capabilities.retrieve_db.legacy.normalization import (
    normalize_text,
    tokenize,
)
from cortex.capabilities.retrieve_db.legacy.reference_conflicts import alternative_node
from cortex.capabilities.retrieve_db.rule_signals import generation_rule_signals
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState

_FIELD_WEIGHT = {"name": 0.70, "alias": 0.65, "tag": 0.55, "description": 0.40}
_EXACT_SCORE = {"name": 1.0, "alias": 0.97}
_ORDINARY_TYPES = frozenset({"table", "column", "dimension_value", "metric", "concept"})
_ENGLISH_TOP_N = re.compile(
    r"(?<![a-z0-9_])top\s*(?:n|\d+)(?![a-z0-9_])",
    re.IGNORECASE,
)
_CHINESE_TOP_N = re.compile(
    r"前\s*(?:n|\d+|[一二两三四五六七八九十百]+)", re.IGNORECASE
)
_ENGLISH_TOP_ONE = re.compile(
    r"\b(?:best[- ]selling|highest\s+(?:sales|revenue|volume))\b",
    re.IGNORECASE,
)
_CHINESE_TOP_ONE = re.compile(
    r"(?:卖得|销售|销量|销额|销售额|零售额)(?:最(?:好|高|多)|第一)|最畅销"
)
_TIME_RANGE_SUFFIXES = ("年", "年度", "季度", "月", "周", "星期", "天")


@dataclass(frozen=True)
class SearchDocument:
    node_id: str
    normalized_name: str
    normalized_aliases: tuple[str, ...]
    fields: dict[str, frozenset[str]]


@dataclass(frozen=True)
class GraphIndex:
    node_by_id: dict[str, GraphNode]
    edge_by_id: dict[str, GraphEdge]
    outgoing_edges: dict[str, tuple[GraphEdge, ...]]
    incoming_edges: dict[str, tuple[GraphEdge, ...]]
    exact_names_by_type: dict[str, dict[str, tuple[str, ...]]]
    exact_aliases_by_type: dict[str, dict[str, tuple[str, ...]]]
    documents_by_type: dict[str, tuple[SearchDocument, ...]]
    table_names: dict[str, tuple[str, ...]]
    columns_by_table_and_name: dict[tuple[str, str], tuple[str, ...]]
    columns_by_name: dict[str, tuple[str, ...]]
    values_by_column_and_value: dict[tuple[str, str], tuple[str, ...]]
    values_by_value: dict[str, tuple[str, ...]]
    rule_question_phrases: dict[str, tuple[str, ...]]
    rule_columns: dict[str, tuple[str, ...]]
    rule_operations: dict[str, tuple[str, ...]]
    reference_conflicts: dict[str, GraphNode] = dataclass_field(default_factory=dict)

    @classmethod
    def build(cls, graph: GraphSpace, *, node_conflicts=()) -> GraphIndex:
        payload = {"graph": graph}
        if node_conflicts:
            payload["node_conflicts"] = node_conflicts
        graph = GraphState.model_validate(payload).graph
        conflicts = {node.id: node for node in graph.nodes if node.alternatives}
        nodes = {node.id: node for node in graph.nodes}
        edges = {edge.id: edge for edge in graph.edges}
        outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
        incoming: dict[str, list[GraphEdge]] = defaultdict(list)
        names: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        aliases: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        documents: dict[str, list[SearchDocument]] = defaultdict(list)
        table_names: dict[str, list[str]] = defaultdict(list)
        columns_by_name: dict[str, list[str]] = defaultdict(list)
        columns_by_table_name: dict[tuple[str, str], list[str]] = defaultdict(list)
        values_by_column_value: dict[tuple[str, str], list[str]] = defaultdict(list)
        values_by_value: dict[str, list[str]] = defaultdict(list)
        rule_phrases: dict[str, tuple[str, ...]] = {}
        rule_columns: dict[str, tuple[str, ...]] = {}
        rule_operations: dict[str, tuple[str, ...]] = {}

        for edge in graph.edges:
            outgoing[edge.from_node_id].append(edge)
            incoming[edge.to_node_id].append(edge)
        for node in graph.nodes:
            if node.id in conflicts:
                variants = [
                    alternative_node(
                        node,
                        GraphManager.resolve_alternative(node, item).model_dump(
                            mode="json"
                        ),
                    )
                    for item in conflicts[node.id].alternatives
                ]
                node = node.model_copy(
                    update={
                        "aliases": sorted(
                            {
                                label
                                for variant in variants
                                for label in [variant.name, *variant.aliases]
                            }
                        ),
                        "description": "\n".join(
                            variant.description or "" for variant in variants
                        ),
                    }
                )
            name = normalize_text(node.name)
            alias_values = tuple(
                filter(None, (normalize_text(value) for value in node.aliases))
            )
            if name:
                names[node.node_type][name].append(node.id)
            for alias in alias_values:
                aliases[node.node_type][alias].append(node.id)
            if node.node_type in _ORDINARY_TYPES:
                documents[node.node_type].append(
                    SearchDocument(
                        node_id=node.id,
                        normalized_name=name,
                        normalized_aliases=alias_values,
                        fields={
                            "name": tokenize(node.name),
                            "alias": frozenset().union(
                                *(tokenize(x) for x in node.aliases)
                            ),
                            "tag": frozenset().union(*(tokenize(x) for x in node.tags)),
                            "description": tokenize(_searchable_description(node)),
                        },
                    )
                )
            if node.node_type == "table":
                for value in {
                    name,
                    normalize_text(node.properties.get("table_name")),
                } - {""}:
                    table_names[value].append(node.id)
            elif node.node_type == "column":
                column_name = normalize_text(
                    node.properties.get("column_name") or node.name
                )
                columns_by_name[column_name].append(node.id)
                for edge in outgoing.get(node.id, ()):
                    if (
                        edge.edge_type == "belongs_to"
                        or edge.reference_origin is not None
                    ) and nodes[edge.to_node_id].node_type == "table":
                        columns_by_table_name[(edge.to_node_id, column_name)].append(
                            node.id
                        )
            elif node.node_type == "dimension_value":
                value = normalize_text(node.properties.get("value") or node.name)
                values_by_value[value].append(node.id)
                for edge in outgoing.get(node.id, ()):
                    if (
                        edge.edge_type == "value_of"
                        or edge.reference_origin is not None
                    ) and nodes[edge.to_node_id].node_type == "column":
                        values_by_column_value[(edge.to_node_id, value)].append(node.id)
            elif node.node_type == "generation_rule":
                phrases, columns, operations = generation_rule_signals(
                    node.properties.get("triggers")
                )
                rule_phrases[node.id] = tuple(
                    normalize_text(value) for value in phrases
                )
                rule_columns[node.id] = tuple(
                    value for value in columns if value.startswith("column.")
                )
                rule_operations[node.id] = tuple(
                    normalize_text(value) for value in operations
                )
                if node.id in conflicts:
                    signals = [
                        generation_rule_signals(variant.properties.get("triggers"))
                        for variant in variants
                    ]
                    rule_phrases[node.id] = tuple(
                        sorted({normalize_text(p) for s in signals for p in s[0]})
                    )
                    rule_columns[node.id] = tuple(
                        sorted(
                            {
                                p
                                for s in signals
                                for p in s[1]
                                if p.startswith("column.")
                            }
                        )
                    )
                    rule_operations[node.id] = tuple(
                        sorted({normalize_text(p) for s in signals for p in s[2]})
                    )
        return cls(
            reference_conflicts=conflicts,
            node_by_id=nodes,
            edge_by_id=edges,
            outgoing_edges=_freeze_edges(outgoing),
            incoming_edges=_freeze_edges(incoming),
            exact_names_by_type=_freeze_nested(names),
            exact_aliases_by_type=_freeze_nested(aliases),
            documents_by_type={
                k: tuple(sorted(v, key=lambda x: x.node_id))
                for k, v in documents.items()
            },
            table_names=_freeze(table_names),
            columns_by_table_and_name=_freeze(columns_by_table_name),
            columns_by_name=_freeze(columns_by_name),
            values_by_column_and_value=_freeze(values_by_column_value),
            values_by_value=_freeze(values_by_value),
            rule_question_phrases=rule_phrases,
            rule_columns=rule_columns,
            rule_operations=rule_operations,
        )

    @property
    def node_types(self) -> tuple[str, ...]:
        return tuple(sorted({node.node_type for node in self.node_by_id.values()}))

    def recall(
        self, mention: EntityMention, *, limit: int
    ) -> tuple[NodeCandidate, ...]:
        allowed = tuple(
            t for t in mention.expected_node_types if t in self.documents_by_type
        )
        if not allowed:
            allowed = tuple(sorted(_ORDINARY_TYPES & self.documents_by_type.keys()))
        exact_only = mention.role == "other"
        query = normalize_text(mention.normalized_text)
        matches: dict[str, tuple[float, str]] = {}
        for node_type in allowed:
            for field, index in (
                ("name", self.exact_names_by_type.get(node_type, {})),
                ("alias", self.exact_aliases_by_type.get(node_type, {})),
            ):
                for node_id in index.get(query, ()):
                    _keep_best(matches, node_id, _EXACT_SCORE[field], field)
            if exact_only:
                continue
            query_tokens = tokenize(query)
            for document in self.documents_by_type.get(node_type, ()):
                field, score = _document_score(query, query_tokens, document)
                if score >= 0.2:
                    _keep_best(matches, document.node_id, score, field)
        # `limit` is per node type. A large set of similarly named physical Columns must not
        # consume the entire budget before a Concept or Metric can reach semantic selection.
        ranked = []
        for node_type in allowed:
            type_matches = (
                item
                for item in matches.items()
                if self.node_by_id[item[0]].node_type == node_type
            )
            ranked.extend(
                sorted(type_matches, key=lambda item: (-item[1][0], item[0]))[:limit]
            )
        ranked.sort(key=lambda item: (-item[1][0], item[0]))
        return tuple(
            self._candidate(
                node_id,
                score,
                mention_ids=(mention.mention_id,),
                channel=f"mention_{field}",
                detail=f"{field} matched {mention.text!r}",
                rank=rank,
            )
            for rank, (node_id, (score, field)) in enumerate(ranked, 1)
        )

    def resolve_table(self, table_name: str) -> tuple[str, ...]:
        return self.table_names.get(normalize_text(table_name), ())

    def recall_explicit_columns(
        self, question: str, *, limit: int
    ) -> tuple[NodeCandidate, ...]:
        """Recall maximal physical column labels stated verbatim in the Question."""

        normalized = normalize_text(question)
        matched_labels = {
            label
            for label in self.exact_names_by_type.get("column", {})
            if _is_explicit_column_label(label) and label in normalized
        }
        maximal_labels = {
            label
            for label in matched_labels
            if not any(label != other and label in other for other in matched_labels)
        }
        node_ids = sorted(
            {
                node_id
                for label in maximal_labels
                for node_id in self.exact_names_by_type["column"][label]
            }
        )[:limit]
        return tuple(
            NodeCandidate(
                candidate_id=f"candidate:explicit-column:{rank}:{node_id}",
                node_id=node_id,
                node_type="column",
                name=self.node_by_id[node_id].name,
                channels=("explicit_question_column",),
                score=1.0,
                evidence=(
                    CandidateEvidence(
                        channel="explicit_question_column",
                        detail="maximal verbatim physical column label",
                    ),
                ),
                neighbor_summary=self.neighbor_summary(node_id),
            )
            for rank, node_id in enumerate(node_ids, 1)
        )

    def recall_concept_superset(
        self,
        *,
        candidates: tuple[NodeCandidate, ...],
        per_seed_limit: int,
        total_limit: int,
    ) -> tuple[NodeCandidate, ...]:
        """Recall Concepts that own an already recalled physical binding.

        This only expands existing, typed Graph evidence. The returned Concepts remain ordinary
        candidates and semantic selection still decides whether their contracts fit the Question.
        """

        if per_seed_limit < 1 or total_limit < 1:
            raise ValueError("concept superset limits must be positive")
        matches: dict[str, list[tuple[NodeCandidate, GraphEdge]]] = defaultdict(list)
        for candidate in sorted(
            candidates, key=lambda item: (-item.score, item.node_id)
        ):
            if candidate.node_type not in {"column", "dimension_value"}:
                continue
            owners = [
                edge
                for edge in self.incoming_edges.get(candidate.node_id, ())
                if (
                    edge.edge_type == "grounded_on" or edge.reference_origin is not None
                )
                and self.node_by_id[edge.from_node_id].node_type == "concept"
                and (
                    self.node_by_id[edge.from_node_id].merge_status != "conflict"
                    or edge.from_node_id in self.reference_conflicts
                )
            ][:per_seed_limit]
            for edge in owners:
                matches[edge.from_node_id].append((candidate, edge))

        ranked_ids = sorted(
            matches,
            key=lambda node_id: (
                -max(candidate.score for candidate, _edge in matches[node_id]),
                node_id,
            ),
        )[:total_limit]
        return tuple(
            NodeCandidate(
                candidate_id=f"candidate:concept-binding:{rank}:{node_id}",
                node_id=node_id,
                node_type="concept",
                name=self.node_by_id[node_id].name,
                mention_ids=tuple(
                    sorted(
                        {
                            mention_id
                            for candidate, _edge in matches[node_id]
                            for mention_id in candidate.mention_ids
                        }
                    )
                ),
                channels=("binding_concept",),
                score=max(candidate.score for candidate, _edge in matches[node_id]),
                evidence=tuple(
                    CandidateEvidence(
                        channel="binding_concept",
                        detail=f"{edge.edge_type}->{candidate.node_id}",
                    )
                    for candidate, edge in matches[node_id]
                ),
                neighbor_summary=self.neighbor_summary(node_id),
            )
            for rank, node_id in enumerate(ranked_ids, 1)
        )

    def resolve_column(
        self, column_name: str, *, table_id: str | None = None
    ) -> tuple[str, ...]:
        name = normalize_text(column_name)
        if table_id:
            return self.columns_by_table_and_name.get((table_id, name), ())
        return self.columns_by_name.get(name, ())

    def resolve_dimension_value(
        self, value: object, *, column_id: str | None = None
    ) -> tuple[str, ...]:
        normalized = normalize_text(value)
        if column_id:
            return self.values_by_column_and_value.get((column_id, normalized), ())
        return self.values_by_value.get(normalized, ())

    def resolve_concepts(self, value: object) -> tuple[tuple[str, str], ...]:
        """Resolve a literal to exact Concept names and aliases, with name precedence."""
        normalized = normalize_text(value)
        matches = {
            node_id: "name"
            for node_id in self.exact_names_by_type.get("concept", {}).get(
                normalized, ()
            )
        }
        for node_id in self.exact_aliases_by_type.get("concept", {}).get(
            normalized, ()
        ):
            matches.setdefault(node_id, "alias")
        return tuple((node_id, matches[node_id]) for node_id in sorted(matches))

    def recall_generation_rules(
        self,
        *,
        question: str,
        operations: tuple[str, ...],
        reference_node_ids: tuple[str, ...],
        limit: int,
        column_node_ids: tuple[str, ...] = (),
    ) -> tuple[NodeCandidate, ...]:
        normalized_question = normalize_text(question)
        operation_set = {normalize_text(value) for value in operations}
        column_set = set(column_node_ids)
        evidence: dict[str, list[CandidateEvidence]] = defaultdict(list)
        scores: dict[str, float] = defaultdict(float)
        question_has_top_n = _has_top_n_intent(normalized_question)
        for node_id, phrases in self.rule_question_phrases.items():
            matched = [
                phrase for phrase in phrases if phrase and phrase in normalized_question
            ]
            if (
                not matched
                and question_has_top_n
                and any(_has_top_n_intent(phrase) for phrase in phrases)
            ):
                matched = ["top_n"]
            if matched:
                scores[node_id] += 0.4
                evidence[node_id].append(
                    CandidateEvidence(
                        channel="question_rule_trigger", detail=", ".join(matched)
                    )
                )
        for node_id, configured in self.rule_operations.items():
            matched = sorted(set(configured) & operation_set)
            if matched:
                scores[node_id] += 0.25
                evidence[node_id].append(
                    CandidateEvidence(
                        channel="sql_rule_operation", detail=", ".join(matched)
                    )
                )
        for node_id, configured in self.rule_columns.items():
            matched = sorted(set(configured) & column_set)
            if matched:
                scores[node_id] += 0.3
                evidence[node_id].append(
                    CandidateEvidence(
                        channel="sql_rule_column", detail=", ".join(matched)
                    )
                )
        for target in reference_node_ids:
            for edge in self.incoming_edges.get(target, ()):
                source = self.node_by_id.get(edge.from_node_id)
                if (
                    source
                    and (
                        source.merge_status != "conflict"
                        or source.id in self.reference_conflicts
                    )
                    and source.node_type == "generation_rule"
                    and (
                        edge.edge_type in {"applies_to", "grounded_on"}
                        or edge.reference_origin is not None
                    )
                ):
                    scores[source.id] += 0.35
                    evidence[source.id].append(
                        CandidateEvidence(
                            channel="edge_rule_target",
                            detail=f"{edge.edge_type}->{target}",
                        )
                    )
        ranked = sorted(scores, key=lambda node_id: (-scores[node_id], node_id))[:limit]
        return tuple(
            NodeCandidate(
                candidate_id=f"candidate:rule:{rank}:{node_id}",
                node_id=node_id,
                node_type="generation_rule",
                name=self.node_by_id[node_id].name,
                channels=tuple(
                    dict.fromkeys(item.channel for item in evidence[node_id])
                ),
                score=min(1.0, round(scores[node_id], 6)),
                evidence=tuple(evidence[node_id]),
                neighbor_summary=self.neighbor_summary(node_id),
            )
            for rank, node_id in enumerate(ranked, 1)
        )

    def recall_generation_rule_superset(
        self,
        *,
        candidates: tuple[NodeCandidate, ...],
        per_seed_limit: int,
        total_limit: int,
    ) -> tuple[NodeCandidate, ...]:
        """Recall a bounded rule superset from unselected direct candidates."""
        if per_seed_limit < 1 or total_limit < 1:
            raise ValueError("generation rule superset limits must be positive")
        per_seed: list[tuple[str, tuple[str, ...]]] = []
        for candidate in sorted(
            candidates, key=lambda item: (-item.score, item.node_id)
        ):
            if candidate.node_type == "generation_rule":
                continue
            rule_ids = sorted(
                {
                    edge.from_node_id
                    for edge in self.incoming_edges.get(candidate.node_id, ())
                    if (
                        edge.edge_type in {"applies_to", "grounded_on"}
                        or edge.reference_origin is not None
                    )
                    and self.node_by_id[edge.from_node_id].node_type
                    == "generation_rule"
                    and (
                        self.node_by_id[edge.from_node_id].merge_status != "conflict"
                        or edge.from_node_id in self.reference_conflicts
                    )
                }
            )[:per_seed_limit]
            if rule_ids:
                per_seed.append((candidate.node_id, tuple(rule_ids)))

        evidence: dict[str, list[CandidateEvidence]] = defaultdict(list)
        ordered_rule_ids: list[str] = []
        for offset in range(per_seed_limit):
            for seed_id, rule_ids in per_seed:
                if offset >= len(rule_ids):
                    continue
                rule_id = rule_ids[offset]
                evidence[rule_id].append(
                    CandidateEvidence(
                        channel="candidate_rule_reference",
                        detail=seed_id,
                    )
                )
                if rule_id not in ordered_rule_ids:
                    ordered_rule_ids.append(rule_id)
                if len(ordered_rule_ids) >= total_limit:
                    break
            if len(ordered_rule_ids) >= total_limit:
                break

        return tuple(
            NodeCandidate(
                candidate_id=f"candidate:rule-superset:{rank}:{node_id}",
                node_id=node_id,
                node_type="generation_rule",
                name=self.node_by_id[node_id].name,
                channels=("candidate_rule_reference",),
                score=0.35,
                evidence=tuple(dict.fromkeys(evidence[node_id])),
                neighbor_summary=self.neighbor_summary(node_id),
            )
            for rank, node_id in enumerate(ordered_rule_ids, 1)
        )

    def neighbor_summary(self, node_id: str, *, limit: int = 8) -> tuple[str, ...]:
        values = []
        for edge in self.outgoing_edges.get(node_id, ()):
            node = self.node_by_id[edge.to_node_id]
            values.append(f"out:{edge.edge_type}->{node.node_type}:{node.name}")
        for edge in self.incoming_edges.get(node_id, ()):
            node = self.node_by_id[edge.from_node_id]
            values.append(f"in:{edge.edge_type}<-{node.node_type}:{node.name}")
        return tuple(sorted(values)[:limit])

    def _candidate(
        self,
        node_id: str,
        score: float,
        *,
        mention_ids: tuple[str, ...],
        channel: str,
        detail: str,
        rank: int,
    ) -> NodeCandidate:
        node = self.node_by_id[node_id]
        return NodeCandidate(
            candidate_id=f"candidate:{mention_ids[0]}:{rank}:{node_id}",
            node_id=node.id,
            node_type=node.node_type,
            name=node.name,
            mention_ids=mention_ids,
            channels=(channel,),
            score=round(score, 6),
            evidence=(CandidateEvidence(channel=channel, detail=detail),),
            neighbor_summary=self.neighbor_summary(node_id),
        )


def _searchable_description(node: GraphNode) -> str:
    values = [node.description or ""]
    if node.node_type == "metric":
        values.append(str(node.properties.get("pattern") or ""))
        for plan in node.properties.get("sql_generation_plans") or ():
            if isinstance(plan, dict):
                values.append(str(plan.get("application") or ""))
    return " ".join(values)


def _is_explicit_column_label(label: str) -> bool:
    """Keep distinctive labels; a single English business word is not a physical-field mention."""

    latin_tokens = re.findall(r"[a-z0-9_]+", label)
    han_characters = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", label)
    return len(latin_tokens) >= 2 or len(han_characters) >= 2 or "_" in label


def _has_top_n_intent(value: str) -> bool:
    normalized = normalize_text(value)
    if _ENGLISH_TOP_N.search(normalized) or _ENGLISH_TOP_ONE.search(normalized):
        return True
    if _CHINESE_TOP_ONE.search(normalized):
        return True
    for match in _CHINESE_TOP_N.finditer(normalized):
        suffix = normalized[match.end() :].lstrip()
        if suffix.startswith(_TIME_RANGE_SUFFIXES) or (
            suffix.startswith("个") and suffix[1:].startswith(_TIME_RANGE_SUFFIXES)
        ):
            continue
        return True
    return False


def _document_score(
    query: str, query_tokens: frozenset[str], document: SearchDocument
) -> tuple[str, float]:
    if query and query in document.normalized_name:
        return "name", 0.82
    if query and any(query in alias for alias in document.normalized_aliases):
        return "alias", 0.80
    best = ("description", 0.0)
    for field, tokens in document.fields.items():
        if tokens and query_tokens:
            score = (
                len(query_tokens & tokens) / len(query_tokens) * _FIELD_WEIGHT[field]
            )
            if score > best[1]:
                best = (field, score)
    return best


def _keep_best(
    matches: dict[str, tuple[float, str]], node_id: str, score: float, field: str
) -> None:
    if node_id not in matches or score > matches[node_id][0]:
        matches[node_id] = (score, field)


def _freeze(values: dict[Any, list[str]]) -> dict[Any, tuple[str, ...]]:
    return {key: tuple(sorted(set(items))) for key, items in values.items()}


def _freeze_nested(
    values: dict[str, dict[str, list[str]]],
) -> dict[str, dict[str, tuple[str, ...]]]:
    return {node_type: _freeze(index) for node_type, index in values.items()}


def _freeze_edges(
    values: dict[str, list[GraphEdge]],
) -> dict[str, tuple[GraphEdge, ...]]:
    return {
        key: tuple(sorted(items, key=lambda edge: edge.id))
        for key, items in values.items()
    }


__all__ = ["GraphIndex"]
