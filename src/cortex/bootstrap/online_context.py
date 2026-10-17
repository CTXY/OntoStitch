from __future__ import annotations

import random
from asyncio import Condition
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel

from cortex.application.online_context import OnlineContextService
from cortex.capabilities.online_context.discovery.organizer import LlmChoiceOrganizer
from cortex.capabilities.online_context.engine import OnlineContextCapability
from cortex.capabilities.online_context.interaction.simulated_user import SimulatedUserAgent
from cortex.capabilities.online_context.output.guidance import GuidanceAgent
from cortex.capabilities.online_context.retrieval.recall import TypedRecall
from cortex.capabilities.online_context.retrieval.selection import LlmRelevanceSelector
from cortex.capabilities.query_signals import (
    GraphPhysicalSchemaRenderer,
    InitialSqlGenerator,
)
from cortex.config import CortexConfig, load_config
from cortex.infrastructure.llm import (
    DocumentVectorCache,
    EmbeddingSpaceIdentity,
    create_chat_model,
    create_embedding_model,
)
from cortex.infrastructure.llm.pool import (
    CredentialBoundEmbeddings,
    KeyScheduler,
    PooledChatModel,
    read_api_keys,
)
from cortex.infrastructure.llm.providers import LLMInvocationError
from cortex.infrastructure.online_context import load_query_example_index
from cortex.schema import load_root_schema

_UNSET = object()


def build_online_context_service(
    config_path: str | Path = "configs/cortex.yaml",
    *,
    config: CortexConfig | None = None,
    llm: BaseChatModel | None = None,
    trace_sink=None,
    document_vector_cache: DocumentVectorCache | None = None,
    embedding_identity: EmbeddingSpaceIdentity | None = None,
    embeddings: Embeddings | None | object = _UNSET,
) -> OnlineContextService:
    resolved = config or load_config(config_path)
    model = llm or create_chat_model(resolved.llm, profile="online_context")
    resolved_embeddings = (
        create_embedding_model(resolved.llm, model=resolved.embedding_model)
        if embeddings is _UNSET
        else embeddings
    )
    engine = OnlineContextCapability(
        relevance_selector=LlmRelevanceSelector(model),
        choice_organizer=LlmChoiceOrganizer(model, trace_sink=trace_sink, audit_slots=True),
        question_budget=resolved.services.online_context.question_budget,
        guidance_synthesizer=GuidanceAgent(model),
        initial_sql_generator=InitialSqlGenerator(model),
        schema_renderer=GraphPhysicalSchemaRenderer(),
        typed_recall=TypedRecall(
            resolved_embeddings,
            {
                kind: getattr(resolved.services.online_context, kind + "_limit")
                for kind in ("concept", "metric", "generation_rule", "column", "dimension_value")
            },
            document_cache=document_vector_cache,
            embedding_identity=embedding_identity,
        ),
        query_example_index=load_query_example_index(
            resolved.services.online_context.query_example_source_root
        ),
        query_example_top_k=resolved.services.online_context.query_example_top_k,
    )
    return OnlineContextService(
        engine,
        schema=load_root_schema(resolved.root_schema.external_path),
    )


def build_simulated_user_agent(
    config_path: str | Path = "configs/cortex.yaml",
    *,
    config: CortexConfig | None = None,
    llm: BaseChatModel | None = None,
) -> SimulatedUserAgent:
    resolved = config or load_config(config_path)
    return SimulatedUserAgent(
        llm or create_chat_model(resolved.llm, profile="simulated_user")
    )


@dataclass
class OnlineContextEvaluationWorker:
    service: OnlineContextService
    simulator: SimulatedUserAgent
    traces: list[dict[str, Any]]
    credential_scheduler: KeyScheduler


class OnlineContextEvaluationPool:
    """Async pool whose workers keep one credential for a complete evaluation Case."""

    def __init__(self, workers: tuple[OnlineContextEvaluationWorker, ...]) -> None:
        if not workers:
            raise ValueError("online context evaluation requires at least one worker")
        self._size = len(workers)
        self._available = list(workers)
        self._active = len(workers)
        self._condition = Condition()

    @property
    def max_workers(self) -> int:
        return self._size

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[OnlineContextEvaluationWorker]:
        async with self._condition:
            while not self._available:
                if self._active == 0:
                    raise LLMInvocationError("All evaluation credential workers are disabled")
                await self._condition.wait()
            worker = self._available.pop()
        worker.traces.clear()
        try:
            yield worker
        finally:
            disabled, _ = worker.credential_scheduler.slot_state(0)
            async with self._condition:
                if disabled:
                    self._active -= 1
                else:
                    self._available.append(worker)
                self._condition.notify_all()


def build_online_context_evaluation_pool(
    config_path: str | Path = "configs/cortex.yaml",
    *,
    config: CortexConfig | None = None,
) -> OnlineContextEvaluationPool:
    resolved = config or load_config(config_path)
    settings = resolved.services.online_context
    key_path = settings.evaluation_key_pool_path
    if key_path is None:
        configs = [resolved]
    else:
        keys = list(read_api_keys(key_path))
        random.SystemRandom().shuffle(keys)
        configs = [
            resolved.model_copy(
                update={
                    "llm": resolved.llm.model_copy(update={"api_key": key})
                }
            )
            for key in keys[: settings.evaluation_case_max_workers]
        ]

    workers = []
    document_vector_cache = DocumentVectorCache()
    embedding_model = resolved.embedding_model
    embedding_identity = (
        EmbeddingSpaceIdentity.create(
            provider=resolved.llm.provider,
            base_url=resolved.llm.base_url,
            model=embedding_model,
            dimensions=resolved.llm.embedding.dimensions,
            deployment_namespace=resolved.llm.embedding.deployment_namespace,
        )
        if embedding_model
        else None
    )
    for worker_config in configs:
        traces: list[dict[str, Any]] = []
        scheduler = KeyScheduler(1, wait_timeout=worker_config.llm.timeout)
        online_provider_model = create_chat_model(
            worker_config.llm.model_copy(update={"max_retries": 0}),
            profile="online_context",
        )
        simulator_provider_model = create_chat_model(
            worker_config.llm.model_copy(update={"max_retries": 0}),
            profile="simulated_user",
        )
        online_model = PooledChatModel(
            (online_provider_model,),
            scheduler,
            max_retries=worker_config.llm.max_retries,
        )
        simulator_model = PooledChatModel(
            (simulator_provider_model,),
            scheduler,
            max_retries=worker_config.llm.max_retries,
        )
        embedding_provider = create_embedding_model(
            worker_config.llm, model=worker_config.embedding_model
        )
        worker_embeddings = (
            CredentialBoundEmbeddings(embedding_provider, scheduler)
            if embedding_provider is not None
            else None
        )
        workers.append(
            OnlineContextEvaluationWorker(
                service=build_online_context_service(
                    config=worker_config,
                    llm=online_model,
                    trace_sink=lambda stage, payload, sink=traces: sink.append(
                        {"stage": stage, "payload": payload}
                    ),
                    document_vector_cache=(
                        document_vector_cache if embedding_identity is not None else None
                    ),
                    embedding_identity=embedding_identity,
                    embeddings=worker_embeddings,
                ),
                simulator=build_simulated_user_agent(
                    config=worker_config, llm=simulator_model
                ),
                traces=traces,
                credential_scheduler=scheduler,
            )
        )
    return OnlineContextEvaluationPool(tuple(workers))


__all__ = [
    "OnlineContextEvaluationPool",
    "OnlineContextEvaluationWorker",
    "build_online_context_evaluation_pool",
    "build_online_context_service",
    "build_simulated_user_agent",
]
