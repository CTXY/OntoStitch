from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from langchain_core.embeddings import Embeddings

from cortex.capabilities.graph_merge.models import (
    CrossTypeCandidate,
    CrossTypeResolution,
    EvidenceItem,
    FinalNodeGroup,
    GroupIdentityView,
    IdentityGroupingResult,
    IdentityRepresentationView,
    NodeContent,
    NodeIdentitySelection,
    NodeIdentityView,
    NodeRepresentation,
    RepresentationRef,
)
from cortex.schema import (
    GraphNode,
    IdentityMode,
    IdentityPolicy,
    NodeType,
    SourceReference,
)

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_DECORATION_RE = re.compile(r"^[\s_`'\"\[\]（）()]+|[\s_`'\"\[\]（）()]+$")
_SEPARATOR_RE = re.compile(r"[\s\W_]+", re.UNICODE)


class SemanticIdentityResolver(Protocol):
    def resolve(
        self,
        seed: NodeIdentityView,
        collection_candidates: tuple[GroupIdentityView, ...],
        query_candidates: tuple[NodeIdentityView, ...],
        policy: IdentityPolicy,
    ) -> NodeIdentitySelection: ...


class IdentityEvidenceResolver(Protocol):
    def resolve(
        self, sources: tuple[SourceReference, ...]
    ) -> tuple[EvidenceItem, ...]: ...


class CrossTypeAssetResolver(Protocol):
    def resolve(
        self, candidates: tuple[CrossTypeCandidate, ...]
    ) -> CrossTypeResolution: ...


class CandidateTraceSink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...


IdentityImplementationBuilder = Callable[[NodeRepresentation], dict[str, Any]]


class IdentityImplementationRegistry:
    """Provide optional type-specific implementation evidence without defining identity."""

    def __init__(
        self,
        registrations: Mapping[
            str, tuple[frozenset[str], IdentityImplementationBuilder]
        ]
        | None = None,
    ) -> None:
        self.registrations = dict(registrations or {})

    def build(
        self,
        representation: NodeRepresentation,
        effective_fields: frozenset[str],
    ) -> dict[str, Any]:
        registration = self.registrations.get(representation.node_type)
        if registration is None:
            return dict(representation.content.properties)
        required_fields, builder = registration
        if not required_fields <= effective_fields:
            return dict(representation.content.properties)
        return builder(representation)


def build_default_identity_implementation_registry() -> IdentityImplementationRegistry:
    return IdentityImplementationRegistry(
        {
            "concept": (frozenset({"targets"}), _concept_implementation),
            "metric": (frozenset({"expression"}), _metric_implementation),
            "generation_rule": (
                frozenset({"triggers"}),
                _generation_rule_implementation,
            ),
        }
    )


def node_content(node: GraphNode) -> NodeContent:
    return NodeContent(
        name=node.name,
        description=node.description,
        aliases=tuple(node.aliases),
        tags=tuple(node.tags),
        properties=node.properties,
    )


def identity_values(content: NodeContent, policy: IdentityPolicy) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for path in policy.fields:
        if path.startswith("properties."):
            values[path] = content.properties.get(path.removeprefix("properties."))
        else:
            values[path] = getattr(content, path)
    return values


def exact_identity_key(content: NodeContent, policy: IdentityPolicy) -> str:
    return json.dumps(
        identity_values(content, policy),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _representation(node: GraphNode, origin: str) -> NodeRepresentation:
    return NodeRepresentation(
        ref=RepresentationRef(origin=origin, ref_id=node.id),
        node_type=node.node_type,
        content=node_content(node),
        sources=node.sources,
    )


def normalize_identity_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = _DECORATION_RE.sub("", value)
    return _SEPARATOR_RE.sub("", value)


def _names(content: NodeContent) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value
            for value in (
                normalize_identity_text(content.name),
                *(normalize_identity_text(alias) for alias in content.aliases),
            )
            if value
        )
    )


def _ngrams(value: str) -> set[str]:
    return {
        value[index : index + size]
        for size in (2, 3)
        for index in range(len(value) - size + 1)
    }


def _implementation_tokens(content: NodeContent) -> set[str]:
    keys = (
        "targets",
        "expression",
        "filters",
        "references",
        "dependencies",
        "unit",
        "grain",
        "condition",
        "action",
        "operation_intent",
        "triggers",
    )
    tokens = {
        normalize_identity_text(
            json.dumps(content.properties[key], ensure_ascii=False, sort_keys=True)
        )
        for key in keys
        if key in content.properties
        and content.properties[key] not in (None, "", [], {})
    }
    for target in content.properties.get("targets", ()):
        if isinstance(target, dict):
            tokens.add(
                normalize_identity_text(
                    json.dumps(
                        {
                            key: target[key]
                            for key in ("sql_expression", "references")
                            if key in target
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            )
    return tokens


class IdentityGrouper:
    def __init__(
        self,
        resolver: SemanticIdentityResolver | None,
        *,
        evidence_resolver: IdentityEvidenceResolver | None = None,
        trace_sink: CandidateTraceSink | None = None,
        candidate_top_k: int = 5,
        implementation_registry: IdentityImplementationRegistry | None = None,
        cross_type_resolver: CrossTypeAssetResolver | None = None,
        embeddings: Embeddings | None = None,
    ) -> None:
        self.resolver = resolver
        self.evidence_resolver = evidence_resolver
        self.trace_sink = trace_sink
        self.candidate_top_k = candidate_top_k
        self.implementation_registry = (
            implementation_registry or IdentityImplementationRegistry()
        )
        self.cross_type_resolver = cross_type_resolver
        self.embeddings = embeddings

    def group(
        self,
        existing: tuple[GraphNode, ...],
        incoming: tuple[GraphNode, ...],
        node_types: dict[str, NodeType],
        effective_fields_by_type: Mapping[str, frozenset[str]] | None = None,
        *,
        existing_alternatives=None,
    ) -> IdentityGroupingResult:
        existing_alternatives = existing_alternatives or {}
        replacements, replacement_diagnostics = self._cross_type_replacements(
            existing, incoming
        )
        discarded = {loser for loser, _winner in replacements}
        groups = {
            f"existing:{node.id}": FinalNodeGroup(
                group_id=f"existing:{node.id}",
                node_type=node.node_type,
                existing_node_id=node.id,
                members=tuple(existing_alternatives.get(node.id, ()))
                or (_representation(node, "existing_node"),),
            )
            for node in sorted(existing, key=lambda item: (item.node_type, item.id))
            if RepresentationRef(origin="existing_node", ref_id=node.id)
            not in discarded
        }
        queries = {
            node.id: _representation(node, "incoming_node")
            for node in sorted(incoming, key=lambda item: (item.node_type, item.id))
            if RepresentationRef(origin="incoming_node", ref_id=node.id)
            not in discarded
        }
        diagnostics: list[str] = list(replacement_diagnostics)
        while queries:
            seed_id = min(queries, key=lambda item: (queries[item].node_type, item))
            seed = queries[seed_id]
            policy = node_types[seed.node_type].identity_policy
            if policy.mode == IdentityMode.EXACT:
                selected_entry, selected_queries = self._select_exact(
                    seed, groups, queries, policy
                )
            else:
                selected_entry, selected_queries, diagnostic = self._select_semantic(
                    seed,
                    groups,
                    queries,
                    policy,
                    (
                        effective_fields_by_type.get(seed.node_type, frozenset())
                        if effective_fields_by_type
                        else frozenset(node_types[seed.node_type].fields)
                    ),
                )
                if diagnostic:
                    diagnostics.append(diagnostic)
            member_ids = {seed_id, *selected_queries}
            members = tuple(
                queries.pop(item) for item in sorted(member_ids) if item in queries
            )
            if selected_entry is None:
                group_id = f"provisional:{seed_id}"
                groups[group_id] = FinalNodeGroup(
                    group_id=group_id,
                    node_type=seed.node_type,
                    existing_node_id=None,
                    members=members,
                )
            else:
                target = groups[selected_entry]
                combined_by_ref = {
                    member.ref: member for member in (*target.members, *members)
                }
                combined = tuple(
                    combined_by_ref[ref]
                    for ref in sorted(
                        combined_by_ref,
                        key=lambda item: (item.origin, item.ref_id),
                    )
                )
                groups[selected_entry] = target.model_copy(update={"members": combined})
        originals = {node.id: node for node in existing}
        for group_id, group in groups.items():
            if group.existing_node_id in existing_alternatives:
                groups[group_id] = group.model_copy(
                    update={
                        "members": (
                            _representation(
                                originals[group.existing_node_id], "existing_node"
                            ),
                            *(
                                m
                                for m in group.members
                                if m.ref.origin == "incoming_node"
                            ),
                        )
                    }
                )
        return IdentityGroupingResult(
            groups=tuple(sorted(groups.values(), key=lambda item: item.group_id)),
            cross_type_replacements=replacements,
            diagnostic_codes=tuple(diagnostics),
        )

    def _cross_type_replacements(self, existing, incoming):
        if self.cross_type_resolver is None:
            return (), ()
        representations = tuple(
            _representation(node, origin)
            for origin, nodes in (
                ("existing_node", existing),
                ("incoming_node", incoming),
            )
            for node in nodes
            if node.node_type in {"concept", "metric"}
        )
        concepts = [item for item in representations if item.node_type == "concept"]
        metrics = [item for item in representations if item.node_type == "metric"]
        candidates = tuple(
            CrossTypeCandidate(
                pair_id=_cross_type_pair_id(concept.ref, metric.ref),
                concept=concept,
                metric=metric,
            )
            for concept in concepts
            for metric in metrics
            if _candidate_channel(
                concept.content,
                metric.content,
                allowed_channels=_EXACT_NAME_CHANNELS,
            )
        )
        if not candidates:
            return (), ()
        try:
            resolution = self.cross_type_resolver.resolve(candidates)
        except Exception:  # noqa: BLE001 - optional identity decisions fail closed
            return (), ("MODEL_FAILURE",)
        by_id = {item.pair_id: item for item in candidates}
        decisions = {item.pair_id: item for item in resolution.decisions}
        if set(decisions) != set(by_id) or len(decisions) != len(resolution.decisions):
            return (), ("INVALID_SELECTION",)
        proposed = []
        for pair_id, decision in decisions.items():
            candidate = by_id[pair_id]
            refs = {candidate.concept.ref, candidate.metric.ref}
            if decision.verdict == "different":
                if decision.keep_ref is not None:
                    return (), ("INVALID_SELECTION",)
                continue
            if decision.keep_ref not in refs:
                return (), ("INVALID_SELECTION",)
            loser = next(ref for ref in refs if ref != decision.keep_ref)
            proposed.append((loser, decision.keep_ref))
        involved = [ref for pair in proposed for ref in pair]
        if len(involved) != len(set(involved)):
            return (), ("CROSS_TYPE_AMBIGUOUS",)
        return tuple(
            sorted(proposed, key=lambda item: (item[0].origin, item[0].ref_id))
        ), ()

    @staticmethod
    def _select_exact(seed, groups, queries, policy):
        key = exact_identity_key(seed.content, policy)
        entries = [
            group_id
            for group_id, group in groups.items()
            if group.node_type == seed.node_type
            and any(
                exact_identity_key(member.content, policy) == key
                for member in group.members
            )
        ]
        if len(entries) > 1:
            return None, ()
        selected_queries = tuple(
            query_id
            for query_id, query in queries.items()
            if query_id != seed.ref.ref_id
            and query.node_type == seed.node_type
            and exact_identity_key(query.content, policy) == key
        )
        return (entries[0] if entries else None), selected_queries

    def _select_semantic(self, seed, groups, queries, policy, effective_fields):
        if seed.node_type == "generation_rule":
            return self._select_generation_rule_semantic(
                seed, groups, queries, policy, effective_fields
            )
        entry_channels: dict[str, list[tuple[str, FinalNodeGroup]]] = {}
        for group_id, group in groups.items():
            if group.node_type != seed.node_type:
                continue
            channel = _candidate_channel(
                seed.content, *(item.content for item in group.members)
            )
            if channel:
                entry_channels.setdefault(channel, []).append((group_id, group))
        query_channels: dict[str, list[tuple[str, NodeRepresentation]]] = {}
        for query_id, query in queries.items():
            if query_id == seed.ref.ref_id or query.node_type != seed.node_type:
                continue
            channel = _candidate_channel(seed.content, query.content)
            if channel:
                query_channels.setdefault(channel, []).append((query_id, query))
        entries = tuple(
            item[1]
            for item in _diverse_candidates(entry_channels, self.candidate_top_k)
        )
        query_items = _diverse_candidates(query_channels, self.candidate_top_k)
        if self.trace_sink is not None:
            self.trace_sink.record(
                {
                    "component": "candidate_retrieval",
                    "seed_ref": seed.ref.model_dump(mode="json"),
                    "entry_channels": {
                        channel: [item[0] for item in items]
                        for channel, items in sorted(entry_channels.items())
                    },
                    "query_channels": {
                        channel: [item[0] for item in items]
                        for channel, items in sorted(query_channels.items())
                    },
                    "selected_entries": [item.group_id for item in entries],
                    "selected_queries": [item[0] for item in query_items],
                }
            )
        query_views_list = []
        for query_id, query in query_items:
            query_views_list.append(
                NodeIdentityView(
                    query_id=query_id,
                    node_type=query.node_type,
                    representation=self._view(query, policy, effective_fields),
                )
            )
        query_views = tuple(query_views_list)
        if self.resolver is None or (not entries and not query_views):
            return None, (), None
        entry_views_list = []
        for group in entries:
            representations = []
            for member in group.members:
                representations.append(self._view(member, policy, effective_fields))
            entry_views_list.append(
                GroupIdentityView(
                    entry_id=group.group_id,
                    existing_node_id=group.existing_node_id,
                    representations=tuple(representations),
                )
            )
        entry_views = tuple(entry_views_list)
        seed_view = NodeIdentityView(
            query_id=seed.ref.ref_id,
            node_type=seed.node_type,
            representation=self._view(seed, policy, effective_fields),
        )
        return self._resolve_semantic_selection(
            seed, seed_view, entry_views, query_views, policy
        )

    def _select_generation_rule_semantic(
        self, seed, groups, queries, policy, effective_fields
    ):
        seed_text = _condition_action_text(seed.content)
        if not seed_text:
            return None, (), None

        entry_candidates: list[tuple[str, FinalNodeGroup, tuple[str, ...]]] = []
        query_candidates: list[tuple[str, NodeRepresentation, tuple[str, ...]]] = []
        candidate_texts: list[str] = []

        def decision_texts(*contents: NodeContent) -> tuple[str, ...]:
            texts = tuple(
                text
                for content in contents
                for text in (_condition_action_text(content),)
                if text
            )
            candidate_texts.extend(texts)
            return texts

        for group_id, group in groups.items():
            if group.node_type != seed.node_type:
                continue
            texts = decision_texts(*(item.content for item in group.members))
            if texts:
                entry_candidates.append((group_id, group, texts))
        for query_id, query in queries.items():
            if query_id == seed.ref.ref_id or query.node_type != seed.node_type:
                continue
            texts = decision_texts(query.content)
            if texts:
                query_candidates.append((query_id, query, texts))

        embedding_vectors = self._embedding_vectors((seed_text, *candidate_texts))
        entry_scores: list[tuple[str, FinalNodeGroup, dict[str, float]]] = []
        for group_id, group, texts in entry_candidates:
            score = _best_condition_action_score(seed_text, texts, embedding_vectors)
            if score is not None:
                entry_scores.append((group_id, group, score))
        query_scores: list[tuple[str, NodeRepresentation, dict[str, float]]] = []
        for query_id, query, texts in query_candidates:
            score = _best_condition_action_score(seed_text, texts, embedding_vectors)
            if score is not None:
                query_scores.append((query_id, query, score))
        entry_scores.sort(key=lambda item: (-item[2]["score"], item[0]))
        query_scores.sort(key=lambda item: (-item[2]["score"], item[0]))
        entries = tuple(item[1] for item in entry_scores[: self.candidate_top_k])
        query_items = tuple(
            (item[0], item[1]) for item in query_scores[: self.candidate_top_k]
        )
        if self.trace_sink is not None:
            self.trace_sink.record(
                {
                    "component": "candidate_retrieval",
                    "seed_ref": seed.ref.model_dump(mode="json"),
                    "entry_channels": {
                        "condition_action_hybrid": [item[0] for item in entry_scores]
                    },
                    "query_channels": {
                        "condition_action_hybrid": [item[0] for item in query_scores]
                    },
                    "candidate_scores": {
                        item[0]: item[2] for item in (*entry_scores, *query_scores)
                    },
                    "selected_entries": [item.group_id for item in entries],
                    "selected_queries": [item[0] for item in query_items],
                }
            )
        query_views_list = []
        for query_id, query in query_items:
            query_views_list.append(
                NodeIdentityView(
                    query_id=query_id,
                    node_type=query.node_type,
                    representation=self._view(query, policy, effective_fields),
                )
            )
        query_views = tuple(query_views_list)
        if self.resolver is None or (not entries and not query_views):
            return None, (), None
        entry_views_list = []
        for group in entries:
            representations = []
            for member in group.members:
                representations.append(self._view(member, policy, effective_fields))
            entry_views_list.append(
                GroupIdentityView(
                    entry_id=group.group_id,
                    existing_node_id=group.existing_node_id,
                    representations=tuple(representations),
                )
            )
        entry_views = tuple(entry_views_list)
        seed_view = NodeIdentityView(
            query_id=seed.ref.ref_id,
            node_type=seed.node_type,
            representation=self._view(seed, policy, effective_fields),
        )
        return self._resolve_semantic_selection(
            seed, seed_view, entry_views, query_views, policy
        )

    def _embedding_vectors(
        self, texts: tuple[str, ...]
    ) -> dict[str, tuple[float, ...]]:
        unique_texts = tuple(dict.fromkeys(texts))
        if self.embeddings is None or len(unique_texts) < 2:
            return {}
        try:
            vectors = self.embeddings.embed_documents(list(unique_texts))
        except Exception:  # noqa: BLE001 - optional identity decisions fail closed  # noqa: BLE001 - embedding retrieval is an optional route.
            return {}
        if len(vectors) != len(unique_texts):
            return {}
        return {
            text: tuple(float(value) for value in vector)
            for text, vector in zip(unique_texts, vectors, strict=True)
        }

    def _resolve_semantic_selection(
        self, seed, seed_view, entry_views, query_views, policy
    ):
        try:
            selection = self.resolver.resolve(
                seed_view, entry_views, query_views, policy
            )
        except Exception:  # noqa: BLE001 - optional identity decisions fail closed
            return None, (), "MODEL_FAILURE"
        allowed_entries = {item.entry_id for item in entry_views}
        allowed_queries = {item.query_id for item in query_views}
        try:
            selection.validate_candidates(allowed_entries, allowed_queries)
        except ValueError:
            return None, (), "INVALID_SELECTION"
        selected_queries = tuple(
            item.query_id for item in selection.query_verdicts if item.verdict == "same"
        )
        return selection.selected_entry_id, selected_queries, None

    def _view(
        self,
        representation: NodeRepresentation,
        policy: IdentityPolicy,
        effective_fields: frozenset[str],
    ) -> IdentityRepresentationView:
        implementation = self.implementation_registry.build(
            representation, effective_fields
        )
        if self.evidence_resolver is not None and representation.sources:
            try:
                evidence = self.evidence_resolver.resolve(representation.sources)
            except Exception:  # noqa: BLE001 - unavailable evidence never proves identity
                evidence = ()
            remaining = 8_000
            items = []
            for item in evidence:
                if item.source not in representation.sources or remaining <= 0:
                    continue
                serialized = json.dumps(item.content, ensure_ascii=False)
                items.append(
                    {
                        "id": item.id,
                        "content_excerpt": serialized[:remaining],
                        "truncated": len(serialized) > remaining,
                    }
                )
                remaining -= len(serialized)
                if len(items) == 4:
                    break
            implementation["source_evidence"] = items
            implementation["evidence_scope"] = (
                "At most four source excerpts and 8000 content characters; "
                "omitted or truncated evidence is not proof of absence."
            )
        return IdentityRepresentationView(
            ref=representation.ref,
            sources=representation.sources,
            identity_fields=identity_values(representation.content, policy),
            key_implementation=implementation,
        )


def _concept_implementation(representation: NodeRepresentation) -> dict[str, Any]:
    properties = representation.content.properties
    return {key: properties[key] for key in ("targets",) if key in properties}


def _metric_implementation(representation: NodeRepresentation) -> dict[str, Any]:
    properties = representation.content.properties
    implementation = {
        "description": representation.content.description,
        **{
            key: properties[key]
            for key in (
                "expression",
                "pattern",
                "unit",
                "grain",
                "sql_generation_plans",
            )
            if key in properties
        },
    }
    return implementation


def _generation_rule_implementation(
    representation: NodeRepresentation,
) -> dict[str, Any]:
    properties = representation.content.properties
    return {
        key: properties[key]
        for key in (
            "operation_intent",
            "condition",
            "action",
            "reason",
            "triggers",
            "references",
        )
        if key in properties
    }


def _cross_type_pair_id(
    concept_ref: RepresentationRef, metric_ref: RepresentationRef
) -> str:
    return f"{concept_ref.origin}:{concept_ref.ref_id}:{metric_ref.origin}:{metric_ref.ref_id}"


_CHANNELS = ("exact", "cross_exact", "containment", "ngram", "implementation")
_CHANNEL_RANK = {channel: index for index, channel in enumerate(_CHANNELS)}
_EXACT_NAME_CHANNELS = frozenset({"exact", "cross_exact"})


def _candidate_channel(
    seed: NodeContent,
    *candidates: NodeContent,
    allowed_channels: frozenset[str] = frozenset(_CHANNELS),
) -> str | None:
    seed_names = _names(seed)
    seed_primary = normalize_identity_text(seed.name)
    seed_impl = (
        _implementation_tokens(seed) if "implementation" in allowed_channels else set()
    )
    best: str | None = None
    for candidate in candidates:
        names = _names(candidate)
        primary = normalize_identity_text(candidate.name)
        if seed_primary and seed_primary == primary:
            channel = "exact"
        elif set(seed_names) & set(names):
            channel = "cross_exact"
        elif "containment" in allowed_channels and any(
            len(a) >= 2 and len(b) >= 2 and (a in b or b in a)
            for a in seed_names
            for b in names
        ):
            channel = "containment"
        elif "ngram" in allowed_channels and any(
            _ngrams(a) & _ngrams(b) for a in seed_names for b in names
        ):
            channel = "ngram"
        elif seed_impl & _implementation_tokens(candidate):
            channel = "implementation"
        else:
            continue
        if channel not in allowed_channels:
            continue
        if best is None or _CHANNEL_RANK[channel] < _CHANNEL_RANK[best]:
            best = channel
    return best


def _condition_action_text(content: NodeContent) -> str:
    condition = content.properties.get("condition")
    action = content.properties.get("action")
    return "\n".join(
        str(value).strip()
        for value in (condition, action)
        if value not in (None, "", [], {})
    )


def _token_set(value: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(unicodedata.normalize("NFKC", value))
        if token
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _best_condition_action_score(
    seed_text: str,
    candidate_texts: tuple[str, ...],
    embedding_vectors: Mapping[str, tuple[float, ...]],
) -> dict[str, float] | None:
    scores = []
    seed_embedding = embedding_vectors.get(seed_text)
    for candidate_text in candidate_texts:
        jaccard = _jaccard(_token_set(seed_text), _token_set(candidate_text))
        candidate_embedding = embedding_vectors.get(candidate_text)
        embedding = (
            _cosine(seed_embedding, candidate_embedding)
            if seed_embedding is not None and candidate_embedding is not None
            else None
        )
        score = (embedding + jaccard) if embedding is not None else jaccard
        if score > 0:
            scores.append(
                {
                    "score": round(score, 6),
                    "jaccard": round(jaccard, 6),
                    **(
                        {"embedding": round(embedding, 6)}
                        if embedding is not None
                        else {}
                    ),
                }
            )
    if not scores:
        return None
    return max(scores, key=lambda item: item["score"])


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if denominator == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator


def _diverse_candidates(
    channels: dict[str, list[tuple]], top_k: int
) -> tuple[tuple, ...]:
    queues = {
        channel: sorted(items, key=lambda item: item[0])
        for channel, items in channels.items()
    }
    result: list[tuple] = []
    seen: set[str] = set()
    while len(result) < top_k:
        added = False
        for channel in _CHANNELS:
            queue = queues.get(channel, [])
            while queue and queue[0][0] in seen:
                queue.pop(0)
            if queue and len(result) < top_k:
                item = queue.pop(0)
                result.append(item)
                seen.add(item[0])
                added = True
        if not added:
            break
    return tuple(result)
