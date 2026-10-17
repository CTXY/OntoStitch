from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel

from cortex.application.merge_graph import GraphMergeUseCase
from cortex.application.sql_training import (
    GraphStateRepository,
    SqlTrainingEngine,
    SqlTrainingService,
)
from cortex.bootstrap.graph_merge import build_graph_merge_operator
from cortex.bootstrap.retrieve_db import build_retrieve_db_service
from cortex.bootstrap.sql_training_retrieval import CortexRetrieveDbGenerationClient
from cortex.capabilities.sql_training import SqlTrainingCapability
from cortex.capabilities.sql_training.artifacts.workspace import Workspace
from cortex.capabilities.sql_training.builders import build_catalog_assets
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.adapter import NodeGraphAdapter
from cortex.capabilities.sql_training.graph_merge_evidence import (
    SqlTrainingGraphMergeEvidenceResolver,
    WorkspaceEvidenceRepository,
)
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
from cortex.capabilities.sql_training.training.initial.bootstrap import (
    TrainingDimensionValueBootstrap,
)
from cortex.capabilities.sql_training.training.initial.expansion import (
    ReferencedDimensionValueExpander,
    analyze_referenced_string_columns,
)
from cortex.capabilities.sql_training.validation import InProcessOntologySqlGenerator
from cortex.config import CortexConfig, LLMProfileName, config_fingerprint, load_config
from cortex.infrastructure.artifacts import FileSystemGraphStateRepository
from cortex.infrastructure.database import DatabaseConnector, SQLAlchemyDatabase
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.pool import (
    KeyScheduler,
    PooledChatModel,
    ThreadedChatModel,
    read_api_keys,
)
from cortex.infrastructure.retrieve import FileSystemRetrieveDbArtifactRecorder
from cortex.schema import GraphSpace
from cortex.schema.roots import load_root_schema


def build_sql_training_service(
    config_path: str | Path = "configs/cortex.yaml",
    *,
    workspace: str | Path | None = None,
    engine: SqlTrainingEngine | None = None,
    llm: BaseChatModel | None = None,
    database: DatabaseConnector | None = None,
    validation_client: object | None = None,
    graph_merge: GraphMergeUseCase | None = None,
    state_repository: GraphStateRepository | None = None,
) -> SqlTrainingService:
    config = load_config(config_path)
    return _build_sql_service(
        config,
        config_fingerprint(config),
        engine,
        llm,
        database,
        validation_client,
        graph_merge,
        state_repository,
        workspace=workspace,
    )


def build_sql_training_physical_graph(
    config_path: str | Path = "configs/cortex.yaml",
    *,
    projection_workspace: str | Path,
) -> GraphSpace:
    """Introspect the configured database and project its complete physical catalog."""

    config = load_config(config_path)
    runtime = SqlTrainingRuntimeConfig.from_cortex(config)
    if not runtime.database.url:
        raise ValueError("configured database URL is required for physical graph introspection")
    database = SQLAlchemyDatabase(runtime.database.url)
    catalog = database.introspect_catalog(include_tables=runtime.database.include_tables)
    assets = build_catalog_assets(catalog.tables, catalog.columns, catalog.joins)
    schema = load_root_schema(config.root_schema.external_path)
    graph, _ = NodeGraphAdapter(Workspace(projection_workspace)).to_graph(
        schema,
        committed_assets=(*assets.tables, *assets.columns),
        catalog_edges=assets.join_edges,
    )
    return graph


def build_sql_training_evaluation_physical_graph(
    config_path: str | Path | CortexConfig = "configs/cortex.yaml",
    *,
    projection_workspace: str | Path,
    sql_records: list[dict[str, object]],
) -> tuple[GraphSpace, dict[str, object]]:
    """Build physical catalog plus values for string columns referenced by evaluation SQL."""

    config = config_path if isinstance(config_path, CortexConfig) else load_config(config_path)
    runtime = SqlTrainingRuntimeConfig.from_cortex(config)
    if not runtime.database.url:
        raise ValueError("configured database URL is required for physical graph introspection")
    database = SQLAlchemyDatabase(runtime.database.url)
    catalog = database.introspect_catalog(include_tables=runtime.database.include_tables)
    assets = build_catalog_assets(catalog.tables, catalog.columns, catalog.joins)
    workspace = Workspace(projection_workspace)
    workspace.write_draft_assets(NodeType.TABLE, assets.tables)
    workspace.write_draft_assets(NodeType.COLUMN, assets.columns)
    literal_bootstrap = TrainingDimensionValueBootstrap(
        runtime,
        workspace,
        database,
    ).run(sql_records)
    literal_assets = workspace.read_draft_assets(NodeType.DIMENSION_VALUE)
    selected, ledger = analyze_referenced_string_columns(
        sql_records,
        column_assets=assets.columns,
        table_assets=assets.tables,
        dialect=database.get_dialect(),
    )
    expansion_config = config.services.sql_training.dimension_value_expansion
    expansion = ReferencedDimensionValueExpander(
        database,
        max_distinct_values=expansion_config.max_distinct_values,
        max_distinct_ratio=expansion_config.max_distinct_ratio,
        batch_size=expansion_config.batch_size,
    ).expand(selected)
    failures = [item for item in expansion.columns if item.status.startswith("FAILED")]
    if failures:
        failed_refs = ", ".join(item.column_ref for item in failures)
        raise ValueError(f"dimension value expansion failed for: {failed_refs}")
    dimension_values = {asset.id: asset for asset in (*expansion.assets, *literal_assets)}
    schema = load_root_schema(config.root_schema.external_path)
    graph, warnings = NodeGraphAdapter(workspace).to_graph(
        schema,
        committed_assets=(*assets.tables, *assets.columns, *dimension_values.values()),
        catalog_edges=assets.join_edges,
    )
    manifest: dict[str, object] = {
        "catalog": {
            "tables": len(assets.tables),
            "columns": len(assets.columns),
            "join_edges": len(assets.join_edges),
        },
        "referenced_string_columns": [item.__dict__ for item in selected],
        "column_reference_ledger": [item.__dict__ for item in ledger],
        "dimension_value_columns": [item.__dict__ for item in expansion.columns],
        "dimension_value_nodes": len(dimension_values),
        "expanded_dimension_value_nodes": len(expansion.assets),
        "verified_sql_literal_nodes": len(literal_assets),
        "skipped_unverified_literals": list(literal_bootstrap.skipped_unverified_literals),
        "expected_value_node_ids": sorted(dimension_values),
        "projection_warnings": list(warnings),
        "thresholds": {
            "max_distinct_values": expansion_config.max_distinct_values,
            "max_distinct_ratio": expansion_config.max_distinct_ratio,
        },
    }
    return graph, manifest


def _build_sql_service(
    config: CortexConfig,
    fingerprint: str,
    engine: SqlTrainingEngine | None,
    llm: BaseChatModel | None,
    database: DatabaseConnector | None,
    validation_client: object | None,
    graph_merge: GraphMergeUseCase | None = None,
    state_repository: GraphStateRepository | None = None,
    *,
    workspace: str | Path | None = None,
) -> SqlTrainingService:
    if engine is not None:
        implementation = engine
    else:
        runtime = SqlTrainingRuntimeConfig.from_cortex(config)
        if workspace is not None:
            resolved_workspace = Path(workspace).expanduser().resolve()
            runtime = runtime.model_copy(update={"workspace": str(resolved_workspace)})
        if database is None and runtime.database.url:
            database = SQLAlchemyDatabase(runtime.database.url)
        validation_client_is_default = validation_client is None
        pooled_models = {}
        models_by_profile = {}
        case_llm_factory = None
        key_path = config.services.sql_training.llm_key_pool_path
        if key_path is not None and llm is not None:
            raise ValueError("Cannot combine an injected LLM with llm_key_pool_path")
        if key_path is not None:
            keys = read_api_keys(key_path)
            scheduler = KeyScheduler(len(keys), wait_timeout=config.llm.timeout)
            for profile in ("sql_training", "retrieve_db"):
                models = tuple(
                    create_chat_model(
                        config.llm.model_copy(update={"api_key": key, "max_retries": 0}),
                        profile=profile,
                    )
                    for key in keys
                )
                pooled_models[profile] = PooledChatModel(
                    models, scheduler, max_retries=config.llm.max_retries
                )
                models_by_profile[profile] = models
            semantic_llm = pooled_models["sql_training"]
            runtime = runtime.model_copy(
                update={
                    "train": runtime.train.model_copy(
                        update={
                            "case_max_workers": min(
                                runtime.train.case_max_workers
                                or runtime.train.extraction_max_workers,
                                len(keys),
                            ),
                        }
                    )
                }
            )
        else:
            semantic_llm = llm or _create_llm(config, "sql_training")
            if config.services.sql_training.case_max_workers is not None:
                semantic_llm = ThreadedChatModel(semantic_llm)
                pooled_models["retrieve_db"] = ThreadedChatModel(
                    llm or _create_llm(config, "retrieve_db")
                )
        validation_client_factory = None
        if validation_client_is_default:

            def make_validation_client(training_model, retrieval_model):
                return CortexRetrieveDbGenerationClient(
                    retrieve_db=build_retrieve_db_service(
                        config=config,
                        llm=retrieval_model,
                    ),
                    generation_client=InProcessOntologySqlGenerator(
                        llm=training_model,
                        database=database,
                        config=runtime.validation,
                    ),
                    artifact_recorder=FileSystemRetrieveDbArtifactRecorder(
                        runtime.workspace,
                        load_root_schema(config.root_schema.external_path),
                    ),
                )

            def validation_client_factory():
                return make_validation_client(semantic_llm, pooled_models.get("retrieve_db", llm))

            validation_client = validation_client_factory()
            if key_path is not None:

                @contextmanager
                def case_llm_factory():
                    # A Case reserves its key through extraction, SQL execution and all repairs.
                    index = scheduler.acquire(wait_for_case=True)
                    local_scheduler = KeyScheduler(1, wait_timeout=config.llm.timeout)
                    try:
                        bound_models = {
                            profile: PooledChatModel(
                                (models[index],),
                                local_scheduler,
                                max_retries=config.llm.max_retries,
                            )
                            for profile, models in models_by_profile.items()
                        }
                        yield (
                            bound_models["sql_training"],
                            make_validation_client(
                                bound_models["sql_training"], bound_models["retrieve_db"]
                            ),
                        )
                    finally:
                        disabled, cooldown = local_scheduler.slot_state(0)
                        scheduler.release(index, disabled=disabled, cooldown=cooldown)
        elif key_path is not None:
            raise ValueError("A fixed Case key requires the default training validation client")
        implementation = SqlTrainingCapability(
            llm=semantic_llm,
            config=config,
            runtime_config=runtime,
            database=database,
            validation_client=validation_client,
            validation_client_factory=validation_client_factory,
            case_llm_factory=case_llm_factory,
        )
    graph_root = Path(workspace or config.services.sql_training.workspace) / "graph_state"
    schema = load_root_schema(config.root_schema.external_path)
    return SqlTrainingService(
        implementation,
        graph_merge=graph_merge
        or build_graph_merge_operator(
            config,
            llm=(semantic_llm if engine is None else llm),
            live_semantics=engine is None or llm is not None,
            evidence_resolver=(
                SqlTrainingGraphMergeEvidenceResolver(
                    WorkspaceEvidenceRepository(Workspace(runtime.workspace))
                )
                if engine is None
                else None
            ),
        ),
        state_repository=state_repository or FileSystemGraphStateRepository(graph_root, schema),
        config_fingerprint=fingerprint,
    )


def _create_llm(config: CortexConfig, profile: LLMProfileName) -> BaseChatModel:
    return create_chat_model(config.llm, profile=profile)
