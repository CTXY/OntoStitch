"""Independent embedding ranks with exact evidence retained before budgets."""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict

from langchain_core.embeddings import Embeddings

from cortex.application.retrieval import RetrievedRepresentation
from cortex.capabilities.retrieve_db.retrieval.index import RepresentationIndex
from cortex.infrastructure.llm.embedding_cache import (
    DocumentVectorCache,
    EmbeddingSpaceIdentity,
)


class TypedRecall:
    def __init__(
        self,
        embeddings: Embeddings | None,
        limits: dict[str, int],
        *,
        document_cache: DocumentVectorCache | None = None,
        embedding_identity: EmbeddingSpaceIdentity | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.limits = limits
        self._cache: OrderedDict[str, list[list[float]]] = OrderedDict()
        self._document_cache = document_cache
        self._embedding_identity = embedding_identity
        if (document_cache is None) != (embedding_identity is None):
            raise ValueError(
                "document cache and embedding identity must be supplied together"
            )

    async def recall(
        self, index: RepresentationIndex, question: str, initial_sql: str | None
    ):
        lexical = {
            item.document.document_id: item
            for item in index.recall(
                question=question, initial_sql=initial_sql, limit=len(index.documents)
            )
        }
        documents = index.documents
        vectors = None
        query = None
        if self.embeddings is not None:
            texts = [
                # Alternative search text is built from the fully restored candidate;
                # keep default evidence compact while embedding common + overrides.
                d.searchable_text
                if d.representation_kind == "conflict_alternative"
                else json.dumps(
                    d.structured_signals, sort_keys=True, ensure_ascii=False
                )
                for d in documents
            ]
            key = hashlib.sha256(json.dumps(texts).encode()).hexdigest()
            if self._document_cache is not None:
                vectors = await self._document_cache.get_or_create(
                    namespace="retrieve_db.typed_recall",
                    serializer_version="structured-signals-full-alternative-v2",
                    identity=self._embedding_identity,
                    texts=texts,
                    loader=lambda: self.embeddings.aembed_documents(texts),
                )
            else:
                vectors = self._cache.get(key)
            if vectors is None:
                vectors = await self.embeddings.aembed_documents(texts)
                if len(vectors) != len(documents):
                    raise ValueError(
                        "embedding provider returned the wrong document count"
                    )
                self._cache[key] = vectors
                while len(self._cache) > 4:
                    self._cache.popitem(last=False)
            query = await self.embeddings.aembed_query(
                question + ("\nAttempted SQL: " + initial_sql if initial_sql else "")
            )
        selected = {}
        groups: dict[str, list[RetrievedRepresentation]] = {}
        for i, document in enumerate(documents):
            previous = lexical.get(document.document_id)
            protected = previous and set(previous.channels) & {
                "question_exact",
                "initial_sql_exact",
            }
            if protected:
                selected[document.document_id] = previous
                continue
            if document.node_type == "table":
                continue
            if vectors is not None and query is not None:
                vector = vectors[i]
                if len(vector) != len(query):
                    raise ValueError("embedding dimensions do not match")
                denominator = math.sqrt(
                    sum(x * x for x in vector) * sum(x * x for x in query)
                )
                score = (
                    sum(a * b for a, b in zip(vector, query, strict=True)) / denominator
                    if denominator
                    else 0
                )
                item = RetrievedRepresentation(
                    document=document,
                    score=max(0, min(1, score)),
                    channels=("semantic_embedding",),
                )
            elif previous is not None:
                item = previous
            else:
                continue
            groups.setdefault(document.node_type, []).append(item)
        for kind, items in groups.items():
            # One node's targets/plans must not exhaust the whole type budget.
            owners: set[str] = {
                i.document.canonical_node_id
                for i in selected.values()
                if i.document.node_type == kind
            }
            added = 0
            for item in sorted(items, key=lambda x: (-x.score, x.document.document_id)):
                owner = item.document.canonical_node_id
                if owner in owners:
                    continue
                if added >= self.limits.get(kind, 0):
                    break
                owners.add(owner)
                added += 1
                selected[item.document.document_id] = item
        # Schema selector needs each value's column as colocated context.
        by_node = {
            d.canonical_node_id: d
            for d in documents
            if d.representation_kind == "canonical_node"
        }
        for item in tuple(selected.values()):
            if item.document.node_type == "dimension_value":
                node = index.node_by_id[item.document.canonical_node_id]
                parent = by_node.get(str(node.properties.get("column_ref") or ""))
                if parent is not None and parent.document_id not in selected:
                    selected[parent.document_id] = RetrievedRepresentation(
                        document=parent, score=item.score, channels=("value_owner",)
                    )
        # Several exact alternatives can map to the same canonical node. Present
        # that node once, with the full alternatives below and all recall channels.
        canonical = {}
        for item in selected.values():
            owner = item.document.canonical_node_id
            previous = canonical.get(owner)
            if previous is None:
                canonical[owner] = item
            else:
                best = item if item.score > previous.score else previous
                canonical[owner] = best.model_copy(
                    update={
                        "channels": tuple(
                            sorted(set(previous.channels) | set(item.channels))
                        ),
                    }
                )
        selected = {item.document.document_id: item for item in canonical.values()}
        for key, item in tuple(selected.items()):
            if item.document.node_type in {"concept", "metric", "generation_rule"}:
                node = index.node_by_id[item.document.canonical_node_id]
                alternatives = [
                    d.structured_signals
                    for d in index.documents_by_node[node.id]
                    if d.representation_kind == "conflict_alternative"
                ]
                conflict = index.conflicts_by_node.get(node.id)
                document = item.document.model_copy(
                    update={
                        "structured_signals": {
                            "content": {
                                "name": node.name,
                                "description": node.description,
                                "aliases": node.aliases,
                                "properties": node.properties,
                            },
                            "alternatives": alternatives,
                            "sources": [
                                source.model_dump(mode="json")
                                for source in node.sources
                            ],
                            **(
                                {
                                    "consolidation_status": conflict.consolidation_status,
                                    "preservation_reasons": list(
                                        conflict.preservation_reasons
                                    ),
                                    "conflict_points": [
                                        point.model_dump(mode="json")
                                        for point in conflict.conflict_points
                                    ],
                                }
                                if conflict is not None
                                else {}
                            ),
                        }
                    }
                )
                selected[key] = item.model_copy(update={"document": document})
        return tuple(selected[k] for k in sorted(selected))
