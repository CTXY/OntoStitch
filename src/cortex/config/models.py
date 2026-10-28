from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RootSchemaConfig(_ConfigModel):
    external_path: Path | None = None


LLMProfileName = Literal[
    "schema_generation",
    "extraction",
    "retrieval",
    "retrieve_db",
    "sql_training",
    "graph_merge",
    "online_context",
    "simulated_user",
]


class LLMProfileConfig(_ConfigModel):
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0)
    enable_thinking: bool | None = None


class LLMProfilesConfig(_ConfigModel):
    schema_generation: LLMProfileConfig = Field(default_factory=LLMProfileConfig)
    extraction: LLMProfileConfig = Field(default_factory=LLMProfileConfig)
    retrieval: LLMProfileConfig = Field(default_factory=LLMProfileConfig)
    retrieve_db: LLMProfileConfig = Field(default_factory=LLMProfileConfig)
    sql_training: LLMProfileConfig = Field(default_factory=LLMProfileConfig)
    graph_merge: LLMProfileConfig = Field(default_factory=LLMProfileConfig)
    online_context: LLMProfileConfig = Field(default_factory=LLMProfileConfig)
    simulated_user: LLMProfileConfig = Field(default_factory=LLMProfileConfig)


class EmbeddingConfig(_ConfigModel):
    model: str | None = None
    deployment_namespace: str | None = None
    dimensions: int | None = Field(default=None, ge=1)


class ResolvedLLMConfig(_ConfigModel):
    model: str | None = None
    base_url: str | None = None
    api_key: SecretStr | None = None
    enable_thinking: bool = False
    temperature: float = Field(default=0.0, ge=0)
    timeout: float = Field(default=300.0, gt=0)
    max_retries: int = Field(default=2, ge=0)


class LLMConfig(_ConfigModel):
    provider: str = "openai"
    model: str | None = None
    base_url: str | None = None
    api_key: SecretStr | None = None
    enable_thinking: bool = False
    temperature: float = Field(default=0.0, ge=0)
    timeout: float = Field(default=300.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    profiles: LLMProfilesConfig = Field(default_factory=LLMProfilesConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)

    def resolve(self, profile: LLMProfileName | None = None) -> ResolvedLLMConfig:
        chain = {
            "online_context": ("online_context", "retrieve_db"),
            "simulated_user": ("simulated_user", "online_context", "retrieve_db"),
        }.get(profile, (profile,) if profile is not None else ())
        overrides = tuple(getattr(self.profiles, name) for name in chain)

        def value(field: str, default):
            return next(
                (
                    candidate
                    for override in overrides
                    if (candidate := getattr(override, field)) is not None
                ),
                default,
            )

        return ResolvedLLMConfig(
            model=value("model", self.model),
            base_url=self.base_url,
            api_key=self.api_key,
            enable_thinking=value("enable_thinking", self.enable_thinking),
            temperature=value("temperature", self.temperature),
            timeout=self.timeout,
            max_retries=self.max_retries,
        )


class DocumentParserConfig(_ConfigModel):
    endpoint: str | None = None
    backend: str = Field(default="vlm-http-client", min_length=1)
    server_url: str | None = None
    timeout: float = Field(default=1800.0, gt=0)
    retries: int = Field(default=2, ge=0)
    return_images: bool = True


class DocumentsConfig(_ConfigModel):
    parser: DocumentParserConfig = Field(default_factory=DocumentParserConfig)


class _DatabaseConfig(_ConfigModel):
    description_path: Path | None = None
    include_tables: tuple[str, ...] = ()

    @field_validator("include_tables", mode="before")
    @classmethod
    def parse_include_tables(cls, value: Any) -> Any:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value


class MySQLDatabaseConfig(_DatabaseConfig):
    type: Literal["mysql"] = "mysql"
    url: SecretStr | None = None


class SQLiteDatabaseConfig(_DatabaseConfig):
    type: Literal["sqlite"] = "sqlite"
    path: Path
    read_only: bool = False
    schema_path: Path | None = None
    seed_path: Path | None = None
    overwrite: bool = False


DatabaseConfig = Annotated[
    MySQLDatabaseConfig | SQLiteDatabaseConfig,
    Field(discriminator="type"),
]


class SqlTrainingValidationConfig(_ConfigModel):
    dialect: str = "mysql"
    enable_query_execution: bool = True
    max_retry_count: int = Field(default=2, ge=0)


class SqlTrainingErrorAnalysisConfig(_ConfigModel):
    max_repair_rounds: int = Field(default=8, ge=1)
    max_invalid_repair_rounds: int = Field(default=2, ge=1)
    max_joint_repair_checks: int = Field(default=6, ge=1)
    max_joint_repair_group_size: int = Field(default=3, ge=2)


class SchemaGenerationConfig(_ConfigModel):
    max_workers: int = Field(default=4, ge=1)
    max_input_tokens: int = Field(default=60_000, ge=1)


class ExtractionConfig(_ConfigModel):
    max_workers: int = Field(default=6, ge=1)
    link_max_workers: int = Field(default=6, ge=1)
    link_candidate_batch_size: int = Field(default=40, ge=1)
    link_batch_size: int = Field(default=20, ge=1)
    intra_document_recall_limit: int = Field(default=5, ge=1)
    cross_document_recall_limit: int = Field(default=5, ge=1)
    duplicate_confidence: float = Field(default=0.9, ge=0, le=1)
    related_to_confidence: float = Field(default=0.7, ge=0, le=1)
    enable_edge_extraction: bool = True


class RetrievalConfig(_ConfigModel):
    workspace: Path = Path("workspace/retrieval")
    max_rewrites: int = Field(default=3, ge=0)
    min_candidates: int = Field(default=6, ge=1)
    max_candidates: int = Field(default=100, ge=1)
    candidate_scale: float = Field(default=1.5, gt=0)
    rrf_k: int = Field(default=60, ge=1)
    rewrite_weight: float = Field(default=0.85, ge=0, le=1)
    max_evidence: int = Field(default=6, ge=1)
    rerank_character_budget: int = Field(default=24_000, ge=1)
    rerank_excerpt_characters: int = Field(default=1_800, ge=1)
    domain_query_boost: float = Field(default=1.12, ge=0)
    common_query_boost: float = Field(default=1.08, ge=0)
    error_similarity_threshold: float = Field(default=0.28, ge=0, le=1)

    @model_validator(mode="after")
    def validate_candidate_bounds(self) -> Self:
        if self.max_candidates < self.min_candidates:
            raise ValueError("retrieval max_candidates must be at least min_candidates")
        return self


class RetrieveDbConfig(_ConfigModel):
    workspace: Path = Path("workspace/retrieve_db")
    enable_clarification: bool = False
    selection_policy: Literal["training_recall", "unattended"] = "training_recall"
    mention_candidate_limit: int = Field(default=5, ge=1)
    sql_literal_concept_limit: int = Field(default=10, ge=1)
    sql_literal_value_limit: int = Field(default=10, ge=1)
    concept_limit: int = Field(default=12, ge=1)
    metric_limit: int = Field(default=10, ge=1)
    generation_rule_limit: int = Field(default=10, ge=1)
    column_limit: int = Field(default=20, ge=1)
    dimension_value_limit: int = Field(default=12, ge=1)
    edge_rule_per_seed_limit: int = Field(default=10, ge=1)
    edge_rule_total_limit: int = Field(default=20, ge=1)
    binding_concept_per_seed_limit: int = Field(default=10, ge=1)
    binding_concept_total_limit: int = Field(default=20, ge=1)
    embedding_model: str | None = None
    embedding_candidate_limit: int = Field(default=5, ge=1)
    embedding_minimum_score: float = Field(default=0.2, ge=-1, le=1)
    closure_max_nodes: int = Field(default=50, ge=1)
    closure_max_edges: int = Field(default=100, ge=0)
    metric_dependency_depth: int = Field(default=4, ge=1)


class DimensionValueExpansionConfig(_ConfigModel):
    enabled: bool = True
    batch_size: int = Field(default=1000, ge=1)
    max_distinct_values: int = Field(default=500, ge=1)
    max_distinct_ratio: float = Field(default=0.5, ge=0, le=1)


class SqlTrainingConfig(_ConfigModel):
    workspace: Path = Path("workspace/sql_training")
    dimension_values_path: Path | None = None
    dimension_value_expansion: DimensionValueExpansionConfig = Field(
        default_factory=DimensionValueExpansionConfig
    )
    max_rounds: int = Field(default=3, ge=1)
    max_iterations: int = Field(default=3, ge=1)
    sql_timeout: float = Field(default=30.0, gt=0)
    required_consecutive_correct_replays: int = Field(default=3, ge=1)
    case_max_workers: int | None = Field(default=None, ge=1)
    agent_max_steps: int = Field(default=10, ge=1)
    agent_read_concurrency: int = Field(default=4, ge=1, le=4)
    agent_episode_timeout: float = Field(default=180.0, gt=0)
    agent_context_characters: int = Field(default=64_000, ge=4_000)
    agent_notes_characters: int = Field(default=4_000, ge=256)
    agent_tool_characters: int = Field(default=12_000, ge=1_000)
    agent_database_calls: int = Field(default=12, ge=1)
    trace_level: Literal["", "info", "debug"] = ""
    validation: SqlTrainingValidationConfig = Field(
        default_factory=SqlTrainingValidationConfig
    )
    error_analysis: SqlTrainingErrorAnalysisConfig = Field(
        default_factory=SqlTrainingErrorAnalysisConfig
    )


class GraphMergeConfig(_ConfigModel):
    checkpoint_url: SecretStr = Field(
        default_factory=lambda: SecretStr(
            os.environ.get(
                "GRAPH_MERGE_CHECKPOINT_URL", "sqlite:///workspace/checkpoints/graph_merge.sqlite"
            )
        ),
        validate_default=True,
    )
    @field_validator("checkpoint_url")
    @classmethod
    def sqlite_checkpoint(cls, value: SecretStr) -> SecretStr:
        url = value.get_secret_value()
        if not url.startswith("sqlite:///") or url.removeprefix("sqlite:///") in ("", ":memory:"):
            raise ValueError("checkpoint_url must be a file-backed sqlite:/// URL")
        return value

    consolidation_max_rounds: int = Field(default=3, ge=1)
    consolidation_max_calls: int = Field(default=18, ge=1)
    candidate_top_k: int = Field(default=5, ge=1)
    max_evidence_items: int = Field(default=24, ge=1)
    max_evidence_characters: int = Field(default=48_000, ge=1)


class OnlineContextConfig(_ConfigModel):
    checkpoint_path: Path = Path("workspace/checkpoints/online_context.sqlite")
    questions_per_round: int = Field(default=1, ge=1)

    question_budget: int | None = Field(default=None, ge=0, strict=True)
    concept_limit: int = Field(default=12, ge=1)
    metric_limit: int = Field(default=10, ge=1)
    generation_rule_limit: int = Field(default=10, ge=1)
    column_limit: int = Field(default=20, ge=1)
    dimension_value_limit: int = Field(default=12, ge=1)
    evaluation_case_max_workers: int = Field(default=1, ge=1)
    query_example_top_k: int = Field(default=3, ge=0)
    query_example_source_root: Path | None = None


class ServicesConfig(_ConfigModel):
    online_context: OnlineContextConfig = Field(default_factory=OnlineContextConfig)
    schema_generation: SchemaGenerationConfig = Field(
        default_factory=SchemaGenerationConfig
    )
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    retrieve_db: RetrieveDbConfig = Field(default_factory=RetrieveDbConfig)
    sql_training: SqlTrainingConfig = Field(default_factory=SqlTrainingConfig)
    graph_merge: GraphMergeConfig = Field(default_factory=GraphMergeConfig)


class CortexConfig(_ConfigModel):
    root_schema: RootSchemaConfig = Field(default_factory=RootSchemaConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    documents: DocumentsConfig = Field(default_factory=DocumentsConfig)
    database: DatabaseConfig = Field(default_factory=MySQLDatabaseConfig)
    services: ServicesConfig = Field(default_factory=ServicesConfig)

    @property
    def embedding_model(self) -> str | None:
        return self.llm.embedding.model or self.services.retrieve_db.embedding_model
