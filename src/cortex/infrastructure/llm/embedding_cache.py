from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingSpaceIdentity:
    provider: str
    base_url_fingerprint: str
    model: str
    dimensions: int | None
    deployment_namespace: str | None
    request_schema_version: str = "openai-compatible-text-v1"

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        base_url: str | None,
        model: str,
        dimensions: int | None = None,
        deployment_namespace: str | None = None,
    ) -> EmbeddingSpaceIdentity:
        endpoint = (base_url or "").strip().encode()
        return cls(
            provider=provider,
            base_url_fingerprint=hashlib.sha256(endpoint).hexdigest(),
            model=model,
            dimensions=dimensions,
            deployment_namespace=deployment_namespace,
        )


class DocumentVectorCache:
    """Bounded async single-flight cache for immutable document vectors."""

    def __init__(self, *, max_size: int = 8) -> None:
        if max_size < 1:
            raise ValueError("embedding cache size must be positive")
        self._max_size = max_size
        self._values: OrderedDict[str, tuple[tuple[float, ...], ...]] = OrderedDict()
        self._in_flight: dict[str, asyncio.Task[tuple[tuple[float, ...], ...]]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        *,
        namespace: str,
        serializer_version: str,
        identity: EmbeddingSpaceIdentity,
        texts: Sequence[str],
        loader: Callable[[], Awaitable[Sequence[Sequence[float]]]],
    ) -> tuple[tuple[float, ...], ...]:
        key = _cache_key(namespace, serializer_version, identity, texts)
        async with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                self._values.move_to_end(key)
                return cached
            task = self._in_flight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._produce(key, loader, len(texts), identity.dimensions)
                )
                self._in_flight[key] = task
        return await asyncio.shield(task)

    async def _produce(self, key, loader, expected, dimensions):
        try:
            result = await self._load(loader, expected, dimensions)
            async with self._lock:
                self._values[key] = result
                self._values.move_to_end(key)
                while len(self._values) > self._max_size:
                    self._values.popitem(last=False)
            return result
        finally:
            async with self._lock:
                self._in_flight.pop(key, None)

    @staticmethod
    async def _load(loader, expected: int, dimensions: int | None):
        raw = await loader()
        if len(raw) != expected:
            raise ValueError("embedding provider returned the wrong document count")
        vectors = tuple(tuple(float(value) for value in vector) for vector in raw)
        actual_dimensions = len(vectors[0]) if vectors else dimensions
        if any(
            (dimensions is not None and len(vector) != dimensions)
            or len(vector) != actual_dimensions
            or any(not math.isfinite(value) for value in vector)
            for vector in vectors
        ):
            raise ValueError("embedding provider returned invalid document vectors")
        return vectors


def _cache_key(namespace, serializer_version, identity, texts) -> str:
    payload = {
        "namespace": namespace,
        "serializer_version": serializer_version,
        "identity": {
            "provider": identity.provider,
            "base_url_fingerprint": identity.base_url_fingerprint,
            "model": identity.model,
            "dimensions": identity.dimensions,
            "deployment_namespace": identity.deployment_namespace,
            "request_schema_version": identity.request_schema_version,
        },
        "texts": list(texts),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


__all__ = ["DocumentVectorCache", "EmbeddingSpaceIdentity"]
