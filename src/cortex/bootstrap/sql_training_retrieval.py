from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from cortex.application.retrieve_db import (
    RetrieveDbResult,
    RetrieveDbService,
    calculate_graph_fingerprint,
)
from cortex.capabilities.sql_training.validation.models import GenerationRequest, GenerationResponse
from cortex.infrastructure.retrieve import FileSystemRetrieveDbArtifactRecorder
from cortex.schema import GraphSpace


class CortexRetrieveDbGenerationClient:
    """Retrieve a final subGraph locally, then generate SQL from that graph."""

    def __init__(
        self,
        *,
        retrieve_db: RetrieveDbService,
        generation_client: Any,
        artifact_recorder: FileSystemRetrieveDbArtifactRecorder,
        cache_size: int = 32,
    ) -> None:
        if cache_size < 1:
            raise ValueError("retrieve_db cache size must be positive")
        self._retrieve_db = retrieve_db
        self._generation_client = generation_client
        self._artifact_recorder = artifact_recorder
        self._cache_size = cache_size
        self._cache: OrderedDict[tuple[str, str, str], RetrieveDbResult] = OrderedDict()
        self.config = getattr(generation_client, "config", None)
        self.transport = getattr(generation_client, "transport", None)
    def retrieve(
        self,
        *,
        question: str,
        graph: GraphSpace,
        dialect: str,
        trace_id: str,
    ) -> RetrieveDbResult:
        key = (
            question.strip(),
            dialect.strip().lower() or "mysql",
            calculate_graph_fingerprint(graph),
        )
        cached = self._cache.get(key)
        if cached is None:
            result = asyncio.run(
                self._retrieve_db.run(
                    question=question,
                    graph=graph,
                    dialect=dialect,
                    enable_clarification=False,
                )
            )
            if result.status == "completed":
                self._cache[key] = result.model_copy(deep=True)
                self._cache.move_to_end(key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
            result = cached.model_copy(deep=True)
        self._artifact_recorder.write(result, trace_id=trace_id)
        return result

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        return self._generation_client.generate(request)


__all__ = ["CortexRetrieveDbGenerationClient"]
