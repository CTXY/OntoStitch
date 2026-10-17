import random
from pathlib import Path

from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
from cortex.config import load_config
from cortex.infrastructure.database.execution import execute_query
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.pool import read_api_keys


async def check(config_path: Path) -> dict:
    config = load_config(config_path)
    runtime = SqlTrainingRuntimeConfig.from_cortex(config)
    result = execute_query("SELECT 1", database_url=runtime.database.url)
    if not result.success:
        raise RuntimeError("SQL Training database preflight failed")
    key_path = config.services.sql_training.llm_key_pool_path
    if key_path is None:
        raise ValueError("set services.sql_training.llm_key_pool_path to a local credential pool")
    key = random.SystemRandom().choice(read_api_keys(key_path))
    model = create_chat_model(
        config.llm.model_copy(update={"api_key": key}), profile="sql_training"
    )
    await model.ainvoke("Reply with OK.")
    return {"database": "connected", "sql_training_llm": "connected"}
