from __future__ import annotations

from pathlib import Path

from cortex.capabilities.sql_training.artifacts import Workspace, write_jsonl
from cortex.capabilities.sql_training.prepare import (
    load_qa_sql_records,
    standardize_qa_sql_records,
)
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig


class PrepareQASQLPipeline:
    def __init__(self, config: SqlTrainingRuntimeConfig):
        self.config = config
        self.workspace = Workspace(config.workspace)

    def run(
        self,
        *,
        qa_path: str | Path,
        dataset: str,
        output_path: str | Path | None = None,
    ) -> dict:
        records = load_qa_sql_records(qa_path)
        normalized = standardize_qa_sql_records(records, dataset=dataset)
        out_path = (
            Path(output_path)
            if output_path is not None
            else (self.workspace.inputs_dir / f"{dataset}.qa_sql.jsonl")
        )
        write_jsonl(out_path, [record.to_record() for record in normalized])

        return {
            "qa_records": len(normalized),
            "dataset": dataset,
            "output_path": str(out_path),
            "workspace": str(self.workspace.root),
        }
