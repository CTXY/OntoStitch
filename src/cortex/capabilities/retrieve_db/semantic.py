from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass

from langchain_core.embeddings import Embeddings

from cortex.application.retrieve_db import CandidateEvidence, NodeCandidate
from cortex.capabilities.retrieve_db.graph_index import GraphIndex
from cortex.schema import GraphNode

_SEMANTIC_TYPES = ("concept", "metric")


@dataclass(frozen=True)
class _EmbeddedDocument:
    node_id: str
    node_type: str
    vector: tuple[float, ...]


class SemanticEmbeddingRecall:
    """Optional dense recall for semantic assets, with a bounded Graph-version cache."""

    def __init__(self, embeddings: Embeddings, *, cache_size: int = 8) -> None:
        if cache_size < 1:
            raise ValueError("embedding cache size must be positive")
        self._embeddings = embeddings
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[_EmbeddedDocument, ...]] = OrderedDict()

    async def recall(
        self,
        *,
        question: str,
        index: GraphIndex,
        graph_fingerprint: str,
        limit: int,
        minimum_score: float,
    ) -> tuple[NodeCandidate, ...]:
        if limit < 1:
            raise ValueError("embedding recall limit must be positive")
        try:
            documents = await self._documents(index, graph_fingerprint)
            if not documents:
                return ()
            query_vector = tuple(
                float(value) for value in await self._embeddings.aembed_query(question)
            )
        except Exception:  # noqa: BLE001 - dense recall is an optional hybrid channel
            return ()

        ranked: list[tuple[float, _EmbeddedDocument]] = []
        for document in documents:
            score = _cosine(query_vector, document.vector)
            if score >= minimum_score:
                ranked.append((score, document))

        selected: list[tuple[float, _EmbeddedDocument]] = []
        for node_type in _SEMANTIC_TYPES:
            selected.extend(
                sorted(
                    (item for item in ranked if item[1].node_type == node_type),
                    key=lambda item: (-item[0], item[1].node_id),
                )[:limit]
            )
        selected.sort(key=lambda item: (-item[0], item[1].node_id))
        return tuple(
            NodeCandidate(
                candidate_id=f"candidate:embedding:{rank}:{document.node_id}",
                node_id=document.node_id,
                node_type=document.node_type,
                name=index.node_by_id[document.node_id].name,
                channels=("semantic_embedding",),
                score=round(score, 6),
                evidence=(
                    CandidateEvidence(
                        channel="semantic_embedding",
                        detail="semantic similarity to the Question",
                    ),
                ),
                neighbor_summary=index.neighbor_summary(document.node_id),
            )
            for rank, (score, document) in enumerate(selected, 1)
        )

    async def _documents(
        self,
        index: GraphIndex,
        graph_fingerprint: str,
    ) -> tuple[_EmbeddedDocument, ...]:
        cached = self._cache.get(graph_fingerprint)
        if cached is not None:
            self._cache.move_to_end(graph_fingerprint)
            return cached
        nodes = [
            node
            for node in index.node_by_id.values()
            if node.node_type in _SEMANTIC_TYPES and node.merge_status != "conflict"
        ]
        nodes.sort(key=lambda node: node.id)
        texts = [_semantic_text(node) for node in nodes]
        vectors = await self._embeddings.aembed_documents(texts) if texts else []
        documents = tuple(
            _EmbeddedDocument(
                node_id=node.id,
                node_type=node.node_type,
                vector=tuple(float(value) for value in vector),
            )
            for node, vector in zip(nodes, vectors, strict=True)
        )
        self._cache[graph_fingerprint] = documents
        self._cache.move_to_end(graph_fingerprint)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return documents


def _semantic_text(node: GraphNode) -> str:
    values = [node.name, *node.aliases, node.description or ""]
    if node.node_type == "metric":
        values.append(str(node.properties.get("pattern") or ""))
    return "\n".join(value for value in values if value)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if denominator == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator


__all__ = ["SemanticEmbeddingRecall"]
