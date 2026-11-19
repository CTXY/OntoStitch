from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel

from cortex.application.retrieve_db import RetrieveDbEngine, RetrieveDbService
from cortex.capabilities.retrieve_db import RetrieveDbCapability
from cortex.config import CortexConfig, load_config
from cortex.infrastructure.llm import (
    DocumentVectorCache,
    EmbeddingSpaceIdentity,
    create_chat_model,
    create_embedding_model,
)
from cortex.infrastructure.retrieve import FileSystemRetrieveResultRecorder
from cortex.schema import load_root_schema

_UNSET = object()


@dataclass(frozen=True)
class RetrieveDbCliRuntime:
    service: RetrieveDbService
    result_recorder: FileSystemRetrieveResultRecorder


def build_retrieve_db_service(
    config_path: str | Path = "configs/cortex.yaml",
    *,
    config: CortexConfig | None = None,
    engine: RetrieveDbEngine | None = None,
    llm: BaseChatModel | None = None,
    embeddings: Embeddings | None | object = _UNSET,
    document_vector_cache: DocumentVectorCache | None = None,
    embedding_identity: EmbeddingSpaceIdentity | None = None,
    relevance_filter: bool = True,
) -> RetrieveDbService:
    resolved = config or load_config(config_path)
    settings = resolved.services.retrieve_db
    retrieval_limits = settings
    implementation = engine or RetrieveDbCapability(
        llm=llm or create_chat_model(resolved.llm, profile="retrieve_db"),
        relevance_filter=relevance_filter,
        embeddings=create_embedding_model(resolved.llm, model=resolved.embedding_model)
        if embeddings is _UNSET
        else embeddings,
        document_vector_cache=document_vector_cache,
        embedding_identity=embedding_identity,
        ontology_schema=load_root_schema(resolved.root_schema.external_path),
        checkpoint_root=settings.workspace / "checkpoints",
        limits={
            kind: getattr(retrieval_limits, kind + "_limit")
            for kind in (
                "concept",
                "metric",
                "generation_rule",
                "column",
                "dimension_value",
            )
        },
        context_settings={
            "selection_policy": settings.selection_policy,
            "embedding_model": resolved.embedding_model,
            "embedding": resolved.llm.embedding.model_dump(mode="json"),
            "llm": resolved.llm.resolve("retrieve_db").model_dump(
                mode="json", exclude={"api_key"}
            ),
        },
    )
    return RetrieveDbService(
        implementation,
        schema=load_root_schema(resolved.root_schema.external_path),
        default_enable_clarification=settings.enable_clarification,
    )


def build_retrieve_db_cli_runtime(
    config_path: str | Path = "configs/cortex.yaml",
    *,
    engine: RetrieveDbEngine | None = None,
    llm: BaseChatModel | None = None,
) -> RetrieveDbCliRuntime:
    config = load_config(config_path)
    return RetrieveDbCliRuntime(
        service=build_retrieve_db_service(config=config, engine=engine, llm=llm),
        result_recorder=FileSystemRetrieveResultRecorder(
            config.services.retrieve_db.workspace
        ),
    )


__all__ = [
    "RetrieveDbCliRuntime",
    "build_retrieve_db_cli_runtime",
    "build_retrieve_db_service",
]
