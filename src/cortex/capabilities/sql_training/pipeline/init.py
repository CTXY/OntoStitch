from __future__ import annotations

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.builders import build_catalog_assets
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.prepare.sqlite import ensure_sqlite_database_ready
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
from cortex.infrastructure.database import DatabaseConnector


class ScanCatalogPipeline:
    def __init__(
        self,
        config: SqlTrainingRuntimeConfig,
        *,
        database: DatabaseConnector,
    ):
        self.config = config
        self.database = database
        self.workspace = Workspace(config.workspace)

    def run(self) -> dict:
        if not self.config.database.url:
            raise ValueError("database.url is required for cortex database build")
        ensure_sqlite_database_ready(self.config.database)
        catalog = self.database.introspect_catalog(
            include_tables=self.config.database.include_tables
        )
        assets = build_catalog_assets(
            catalog.tables, catalog.columns, catalog.joins,
            description_directory=self.config.database.description_path,
        )
        self.workspace.write_draft_assets(NodeType.TABLE, assets.tables)
        self.workspace.write_draft_assets(NodeType.COLUMN, assets.columns)
        self.workspace.write_catalog_edges(assets.join_edges)
        return {
            "tables": len(assets.tables),
            "columns": len(assets.columns),
            "join_edges": len(assets.join_edges),
            "workspace": str(self.workspace.root),
        }
