from __future__ import annotations

import math
import re
from collections import Counter

from nl2sql_baseline.models import QARecord, RetrievedExample


class BM25Retriever:
    """Deterministic BM25 retrieval over question text only."""

    def __init__(self, records: list[QARecord], *, k1: float = 1.5, b: float = 0.75):
        if not records:
            raise ValueError("retrieval records must not be empty")
        self._records = records
        self._k1 = k1
        self._b = b
        self._corpus = [_bm25_terms(record.question) for record in records]
        self._lengths = [len(document) for document in self._corpus]
        self._average_length = sum(self._lengths) / max(1, len(self._lengths))
        self._frequencies = [Counter(document) for document in self._corpus]
        document_frequency: Counter[str] = Counter()
        for document in self._corpus:
            document_frequency.update(set(document))
        count = len(self._corpus)
        self._idf = {
            term: math.log(1 + (count - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def retrieve(self, question: str, *, top_k: int) -> list[RetrievedExample]:
        query = _bm25_terms(question)
        ranked = sorted(
            (
                (self._score(query, index), index)
                for index in range(len(self._records))
            ),
            key=lambda item: (-item[0], self._records[item[1]].source_id),
        )[:top_k]
        return [
            RetrievedExample(record=self._records[index], rank=rank, score=score)
            for rank, (score, index) in enumerate(ranked, 1)
        ]

    def _score(self, query: list[str], index: int) -> float:
        score = 0.0
        frequencies = self._frequencies[index]
        length = self._lengths[index]
        for term in query:
            count = frequencies.get(term, 0)
            if not count:
                continue
            denominator = count + self._k1 * (
                1 - self._b + self._b * length / max(1, self._average_length)
            )
            score += self._idf.get(term, 0.0) * count * (self._k1 + 1) / denominator
        return score


class CharacterTfidfRetriever:
    """Deterministic character n-gram cosine retrieval for short Chinese questions."""

    def __init__(self, records: list[QARecord], *, ngram_sizes: tuple[int, ...] = (1, 2, 3)):
        if not records:
            raise ValueError("retrieval records must not be empty")
        self._records = records
        documents = [_terms(record.question, ngram_sizes) for record in records]
        document_frequency: Counter[str] = Counter()
        for document in documents:
            document_frequency.update(set(document))
        count = len(documents)
        self._idf = {
            term: math.log((count + 1) / (frequency + 1)) + 1
            for term, frequency in document_frequency.items()
        }
        self._vectors = [_vector(document, self._idf) for document in documents]
        self._ngram_sizes = ngram_sizes

    def retrieve(self, question: str, *, top_k: int) -> list[RetrievedExample]:
        query = _vector(_terms(question, self._ngram_sizes), self._idf)
        ranked = sorted(
            enumerate(self._vectors),
            key=lambda item: (-_cosine(query, item[1]), self._records[item[0]].source_id),
        )[:top_k]
        return [
            RetrievedExample(record=self._records[index], rank=rank, score=_cosine(query, vector))
            for rank, (index, vector) in enumerate(ranked, 1)
        ]


class EmbeddingRetriever:
    """Cosine retrieval using an injected embedding provider."""

    def __init__(self, records: list[QARecord], embeddings: object):
        if not records:
            raise ValueError("retrieval records must not be empty")
        self._records = records
        self._embeddings = embeddings
        self._vectors = embeddings.embed_documents([record.question for record in records])

    def retrieve(self, question: str, *, top_k: int) -> list[RetrievedExample]:
        query = self._embeddings.embed_query(question)
        ranked = sorted(
            enumerate(self._vectors),
            key=lambda item: (-_dense_cosine(query, item[1]), self._records[item[0]].source_id),
        )[:top_k]
        return [
            RetrievedExample(
                record=self._records[index],
                rank=rank,
                score=_dense_cosine(query, vector),
            )
            for rank, (index, vector) in enumerate(ranked, 1)
        ]


def _terms(text: str, sizes: tuple[int, ...]) -> Counter[str]:
    normalized = "".join(text.lower().split())
    return Counter(
        normalized[index : index + size]
        for size in sizes
        for index in range(max(0, len(normalized) - size + 1))
    )


def _vector(terms: Counter[str], idf: dict[str, float]) -> dict[str, float]:
    return {
        term: (1 + math.log(frequency)) * idf[term]
        for term, frequency in terms.items()
        if term in idf
    }


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(value * right.get(term, 0) for term, value in left.items()) / (
        left_norm * right_norm
    )


def _dense_cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions do not match")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _bm25_terms(text: str) -> list[str]:
    text = text.lower()
    english = [value for value in re.findall(r"[a-z]+|\d+(?:\.\d+)?%?", text) if len(value) > 1]
    tokens = list(english)
    for size in (2, 3):
        tokens.extend(
            "_".join(english[index : index + size])
            for index in range(len(english) - size + 1)
        )
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.extend(chars)
    tokens.extend(chars[index] + chars[index + 1] for index in range(len(chars) - 1))
    return tokens
