from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel

from cortex.application.retrieve_db import RetrieveDbEngine, RetrieveDbService
from cortex.capabilities.retrieve_db import RetrieveDbCapability
from cortex.config import CortexConfig, load_config
from cortex.infrastructure.llm import create_chat_model, create_embedding_model
from cortex.infrastructure.retrieve import FileSystemRetrieveResultRecorder
from cortex.schema import load_root_schema


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
    embeddings: Embeddings | None = None,
) -> RetrieveDbService:
    resolved = config or load_config(config_path)
    settings = resolved.services.retrieve_db
    implementation = engine or RetrieveDbCapability(
        llm=llm or create_chat_model(resolved.llm, profile="retrieve_db"),
        mention_candidate_limit=settings.mention_candidate_limit,
        sql_literal_concept_limit=settings.sql_literal_concept_limit,
        sql_literal_value_limit=settings.sql_literal_value_limit,
        generation_rule_limit=settings.generation_rule_limit,
        edge_rule_per_seed_limit=settings.edge_rule_per_seed_limit,
        edge_rule_total_limit=settings.edge_rule_total_limit,
        binding_concept_per_seed_limit=settings.binding_concept_per_seed_limit,
        binding_concept_total_limit=settings.binding_concept_total_limit,
        embeddings=(
            embeddings
            if embeddings is not None
            else create_embedding_model(resolved.llm, model=resolved.embedding_model)
        ),
        embedding_candidate_limit=settings.embedding_candidate_limit,
        embedding_minimum_score=settings.embedding_minimum_score,
        closure_max_nodes=settings.closure_max_nodes,
        closure_max_edges=settings.closure_max_edges,
        metric_dependency_depth=settings.metric_dependency_depth,
        selection_policy=settings.selection_policy,
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
        result_recorder=FileSystemRetrieveResultRecorder(config.services.retrieve_db.workspace),
    )


__all__ = ["RetrieveDbCliRuntime", "build_retrieve_db_cli_runtime", "build_retrieve_db_service"]
