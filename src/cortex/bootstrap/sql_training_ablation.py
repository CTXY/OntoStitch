"""Assemble experiment training resources through the normal Bootstrap configuration."""

from pathlib import Path

from cortex.bootstrap.knowledge import build_sql_training_service
from cortex.capabilities.sql_training.ablation import RepairOnlyTrainPipeline
from cortex.capabilities.sql_training.engine import SqlTrainingCapability
from cortex.config import load_config
from cortex.infrastructure.database.execution import execute_query
from cortex.infrastructure.llm import create_chat_model


def build_repair_only_capability(config_path: Path, *, workspace: Path, physical_workspace: Path):
    service = build_sql_training_service(config_path, workspace=workspace)
    original = service._engine
    if not isinstance(original, SqlTrainingCapability):
        raise TypeError("repair-only requires SQL Training Capability")
    return SqlTrainingCapability(
        llm=original.llm,
        config=original.config,
        runtime_config=original.runtime_config,
        validation_client=original.validation_client,
        validation_client_factory=original.validation_client_factory,
        case_llm_factory=original.case_llm_factory,
        database=original.database,
        pipeline_factory=RepairOnlyTrainPipeline,
        prepared_workspace=physical_workspace,
    )


async def preflight_repair_only(capability, config_path: Path):
    result = execute_query("SELECT 1", database_url=capability.runtime_config.database.url)
    if not result.success:
        raise ValueError("database preflight failed")
    config = load_config(config_path)
    model = create_chat_model(config.llm, profile="sql_training")
    await model.ainvoke("Reply with OK.")
    return {"database": "connected", "sql_training_llm": "connected"}
