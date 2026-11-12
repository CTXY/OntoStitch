from __future__ import annotations

from functools import partial
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
from cortex.capabilities.sql_training.graph.nodes import build_graph
from cortex.capabilities.sql_training.graph_merge_evidence import (
    SqlTrainingGraphMergeEvidenceResolver,
    WorkspaceEvidenceRepository,
)
from cortex.capabilities.sql_training.pipeline.grounding.bootstrap import (
    TrainingDimensionValueBootstrap,
)
from cortex.capabilities.sql_training.pipeline.grounding.expansion import (
    ReferencedDimensionValueExpander,
    analyze_referenced_string_columns,
)
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
from cortex.capabilities.sql_training.validation import InProcessOntologySqlGenerator
from cortex.config import CortexConfig, config_fingerprint, load_config
from cortex.infrastructure.artifacts import FileSystemGraphStateRepository
from cortex.infrastructure.database import DatabaseConnector, SQLAlchemyDatabase
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.pool import ThreadedChatModel
from cortex.infrastructure.retrieve import FileSystemRetrieveDbArtifactRecorder
from cortex.schema import GraphSpace
from cortex.schema.roots import load_root_schema


def build_sql_training_service(
    config_path: str | Path | CortexConfig = "configs/cortex.yaml",
    *,
    workspace: str | Path | None = None,
    engine: SqlTrainingEngine | None = None,
    llm: BaseChatModel | None = None,
    database: DatabaseConnector | None = None,
    validation_client: object | None = None,
    graph_merge: GraphMergeUseCase | None = None,
    state_repository: GraphStateRepository | None = None,
) -> SqlTrainingService:
    """Build the full extraction → merge → publication service.

    Extraction construction is delegated to build_sql_extraction. Merge
    resources remain lazy so fatal extraction cannot initialize or invoke them.
    Pass an engine to reuse one already assembled by the caller.
    """
    config = (
        config_path
        if isinstance(config_path, CortexConfig)
        else load_config(config_path)
    )
    implementation = (
        engine
        if engine is not None
        else build_sql_extraction(
            config,
            workspace=workspace,
            llm=llm,
            database=database,
            validation_client=validation_client,
        )
    )
    training_workspace = (
        Path(workspace or config.services.sql_training.workspace).expanduser().resolve()
    )
    graph_root = training_workspace / "graph_state"
    schema = load_root_schema(config.root_schema.external_path)
    return SqlTrainingService(
        implementation,
        graph_merge=graph_merge,
        graph_merge_factory=None
        if graph_merge is not None
        else partial(
            build_graph_merge_operator,
            config,
            llm=llm,
            evidence_resolver=SqlTrainingGraphMergeEvidenceResolver(
                WorkspaceEvidenceRepository(Workspace(training_workspace))
            ),
        ),
        state_repository=(
            state_repository
            if state_repository is not None
            else FileSystemGraphStateRepository(graph_root, schema)
        ),
        config_fingerprint=config_fingerprint(config),
    )


def build_sql_extraction(
    config_path: str | Path | CortexConfig = "configs/cortex.yaml",
    *,
    workspace: str | Path | None = None,
    llm: BaseChatModel | None = None,
    database: DatabaseConnector | None = None,
    validation_client: object | None = None,
) -> SqlTrainingCapability:
    """Build extraction resources only; no graph merge, embeddings or repository."""
    config = (
        config_path
        if isinstance(config_path, CortexConfig)
        else load_config(config_path)
    )
    runtime = SqlTrainingRuntimeConfig.from_cortex(config)
    if workspace is not None:
        resolved_workspace = Path(workspace).expanduser().resolve()
        runtime = runtime.model_copy(update={"workspace": str(resolved_workspace)})
    if database is None and runtime.database.url:
        database = SQLAlchemyDatabase(runtime.database.url)
    validation_client_is_default = validation_client is None
    # An injected model can be shared across cases. Use its synchronous client
    # from retrieval's async workers so native async transports never cross loops.
    retrieval_llm = ThreadedChatModel(llm) if llm is not None else None
    semantic_llm = llm or create_chat_model(config.llm, profile="sql_training")
    if config.services.sql_training.case_max_workers is not None:
        semantic_llm = ThreadedChatModel(semantic_llm)
        retrieval_llm = retrieval_llm or ThreadedChatModel(
            create_chat_model(config.llm, profile="retrieve_db")
        )
    validation_client_factory = None
    if validation_client_is_default:

        def validation_client_factory():
            return CortexRetrieveDbGenerationClient(
                retrieve_db=build_retrieve_db_service(
                    config=config,
                    llm=retrieval_llm,
                ),
                generation_client=InProcessOntologySqlGenerator(
                    llm=semantic_llm,
                    database=database,
                    config=runtime.validation,
                ),
                artifact_recorder=FileSystemRetrieveDbArtifactRecorder(
                    runtime.workspace,
                    load_root_schema(config.root_schema.external_path),
                ),
            )

        validation_client = validation_client_factory()
    return SqlTrainingCapability(
        llm=semantic_llm,
        config=config,
        runtime_config=runtime,
        database=database,
        validation_client=validation_client,
        validation_client_factory=validation_client_factory,
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
        raise ValueError(
            "configured database URL is required for physical graph introspection"
        )
    database = SQLAlchemyDatabase(runtime.database.url)
    catalog = database.introspect_catalog(
        include_tables=runtime.database.include_tables
    )
    assets = build_catalog_assets(
        catalog.tables,
        catalog.columns,
        catalog.joins,
        description_directory=runtime.database.description_path,
    )
    schema = load_root_schema(config.root_schema.external_path)
    graph = build_graph((*assets.tables, *assets.columns), assets.join_edges, schema)
    return graph


def build_sql_training_evaluation_physical_graph(
    config_path: str | Path | CortexConfig = "configs/cortex.yaml",
    *,
    projection_workspace: str | Path,
    sql_records: list[dict[str, object]],
) -> tuple[GraphSpace, dict[str, object]]:
    """Build physical catalog plus values for string columns referenced by evaluation SQL."""

    config = (
        config_path
        if isinstance(config_path, CortexConfig)
        else load_config(config_path)
    )
    runtime = SqlTrainingRuntimeConfig.from_cortex(config)
    if not runtime.database.url:
        raise ValueError(
            "configured database URL is required for physical graph introspection"
        )
    database = SQLAlchemyDatabase(runtime.database.url)
    catalog = database.introspect_catalog(
        include_tables=runtime.database.include_tables
    )
    assets = build_catalog_assets(
        catalog.tables,
        catalog.columns,
        catalog.joins,
        description_directory=runtime.database.description_path,
    )
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
    dimension_values = {
        asset.id: asset for asset in (*expansion.assets, *literal_assets)
    }
    schema = load_root_schema(config.root_schema.external_path)
    graph = build_graph(
        (*assets.tables, *assets.columns, *dimension_values.values()),
        assets.join_edges,
        schema,
    )
    warnings = ()
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
        "skipped_unverified_literals": list(
            literal_bootstrap.skipped_unverified_literals
        ),
        "expected_value_node_ids": sorted(dimension_values),
        "projection_warnings": list(warnings),
        "thresholds": {
            "max_distinct_values": expansion_config.max_distinct_values,
            "max_distinct_ratio": expansion_config.max_distinct_ratio,
        },
    }
    return graph, manifest
