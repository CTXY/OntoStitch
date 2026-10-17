from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from cortex.config.models import (
    CortexConfig as SharedCortexConfig,
)
from cortex.config.models import (
    DatabaseConfig as SharedDatabaseConfig,
)
from cortex.config.models import (
    LLMConfig,
    MySQLDatabaseConfig,
    SQLiteDatabaseConfig,
    SqlTrainingConfig,
    SqlTrainingErrorAnalysisConfig,
    SqlTrainingValidationConfig,
)


class RuntimeDatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str | None = None
    include_tables: list[str] = Field(default_factory=list)
    dialect: str = "mysql"
    sqlite_schema_path: str | None = None
    sqlite_seed_path: str | None = None
    sqlite_overwrite: bool = False


class RuntimeValidationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dialect: str = "mysql"
    enable_query_execution: bool = True
    max_retry_count: int = 2


class SqlTrainingRuntimeConfig(BaseModel):
    """Compatibility view consumed by the migrated historical Node workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: str = "workspace/sql_training"
    database: RuntimeDatabaseConfig = Field(default_factory=RuntimeDatabaseConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    validation: RuntimeValidationConfig = Field(default_factory=RuntimeValidationConfig)
    train: SqlTrainingConfig = Field(default_factory=SqlTrainingConfig)
    error_analysis: SqlTrainingErrorAnalysisConfig = Field(
        default_factory=SqlTrainingErrorAnalysisConfig
    )

    @classmethod
    def from_cortex(cls, config: SharedCortexConfig) -> SqlTrainingRuntimeConfig:
        sql_training = config.services.sql_training
        return cls(
            workspace=str(sql_training.workspace),
            database=_database(config.database),
            llm=config.llm,
            validation=_validation(sql_training.validation),
            train=sql_training,
            error_analysis=sql_training.error_analysis,
        )


def _validation(config: SqlTrainingValidationConfig) -> RuntimeValidationConfig:
    return RuntimeValidationConfig(**config.model_dump(mode="python"))


def _database(config: SharedDatabaseConfig) -> RuntimeDatabaseConfig:
    if isinstance(config, MySQLDatabaseConfig):
        return RuntimeDatabaseConfig(
            url=_secret(config.url),
            include_tables=list(config.include_tables),
            dialect="mysql",
        )
    if isinstance(config, SQLiteDatabaseConfig):
        return RuntimeDatabaseConfig(
            url=f"sqlite:///{config.path}",
            include_tables=list(config.include_tables),
            dialect="sqlite",
            sqlite_schema_path=_path(config.schema_path),
            sqlite_seed_path=_path(config.seed_path),
            sqlite_overwrite=config.overwrite,
        )
    raise TypeError(f"unsupported database config: {type(config).__name__}")


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def _path(value: Path | None) -> str | None:
    return str(value) if value is not None else None


__all__ = [
    "CortexConfig",
    "DatabaseConfig",
    "ErrorAnalysisConfig",
    "LLMConfig",
    "RuntimeDatabaseConfig",
    "RuntimeValidationConfig",
    "SqlTrainingRuntimeConfig",
    "TrainConfig",
    "ValidationConfig",
]

# Historical names are kept private to the migrated runtime and its tests. They do
# not restore the removed top-level Cortex configuration contract.
CortexConfig = SqlTrainingRuntimeConfig
DatabaseConfig = RuntimeDatabaseConfig
ErrorAnalysisConfig = SqlTrainingErrorAnalysisConfig
TrainConfig = SqlTrainingConfig
ValidationConfig = RuntimeValidationConfig
