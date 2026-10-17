from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cortex.capabilities.sql_training.error_analysis.agent import ErrorAnalysisProvider
from cortex.capabilities.sql_training.error_analysis.execution_failure import (
    analyze_execution_failure,
)
from cortex.capabilities.sql_training.error_analysis.minimal_repair import run_minimal_repair
from cortex.capabilities.sql_training.evaluation import evaluate
from cortex.capabilities.sql_training.pipeline.execution import (
    GoldQueryCache,
    result_evaluation_contract,
)
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
from cortex.infrastructure.database.execution import execute_query


class TraceWriter:
    def __init__(self, path: str | Path | None, *, case_id: str | None = None):
        self.path = Path(path) if path else None
        self.case_id = case_id
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    def write(self, event: str, **payload: Any) -> None:
        if self.path is None:
            return
        record = {"event": event, **payload}
        if self.case_id is not None:
            record["case_id"] = self.case_id
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class ErrorAnalysisWorkflow:
    def __init__(
        self, config: SqlTrainingRuntimeConfig, *, llm_provider: ErrorAnalysisProvider | None = None
    ):
        self.config = config
        self.llm_provider = llm_provider

    def run(
        self,
        *,
        question: str,
        sql_gt: str,
        sql_ic: str,
        sql_timeout: float | None = None,
        trace_path: str | Path | None = None,
        trace_case_id: str | None = None,
        evaluation_contract: dict[str, Any] | None = None,
        working_assets: list[dict[str, Any]] | None = None,
        schema_context: dict[str, Any] | None = None,
        evidence: str | None = None,
        gold_column_metadata: dict[str, Any] | None = None,
        gold_cache: GoldQueryCache | None = None,
    ) -> dict[str, Any]:
        provider = self.llm_provider
        if provider is None:
            raise ValueError("llm provider is required for error analysis")
        if not self.config.database.url:
            raise ValueError("database.url is required for error analysis")

        trace = TraceWriter(trace_path, case_id=trace_case_id)
        timeout = sql_timeout if sql_timeout is not None else self.config.train.sql_timeout
        comparison_kwargs = {
            "timeout": timeout,
        }
        if gold_cache is not None:
            comparison_kwargs["gold_cache"] = gold_cache
        final = self._final_comparison(sql_gt, sql_ic, **comparison_kwargs)
        final["evaluation_contract"] = self._review_evaluation_contract(
            evaluation_contract,
            gold_result=final.get("gold_result"),
        )

        evaluation = final.get("evaluation")
        if isinstance(evaluation, dict) and evaluation.get("exec") == -1:
            raise ValueError(
                f"error analysis aborted: results are not comparable ({evaluation.get('reason')})"
            )
        return run_minimal_repair(
            provider,
            question=question,
            sql_gt=sql_gt,
            sql_ic=sql_ic,
            final=final,
            database_url=self.config.database.url or "",
            timeout=timeout,
            max_repair_rounds=self.config.error_analysis.max_repair_rounds,
            max_invalid_repair_rounds=self.config.error_analysis.max_invalid_repair_rounds,
            max_joint_repair_checks=self.config.error_analysis.max_joint_repair_checks,
            max_joint_repair_group_size=(self.config.error_analysis.max_joint_repair_group_size),
            trace=trace,
            working_assets=working_assets,
            schema_context=schema_context,
            evidence=evidence,
            gold_column_metadata=gold_column_metadata,
        )

    def run_execution_failure(
        self,
        *,
        question: str,
        sql_gt: str,
        sql_ic: str,
        execution_error: str,
        table_schema: dict[str, str],
        matched_ontology: dict[str, Any],
        draft_metrics: list[dict[str, Any]],
        evaluation_contract: dict[str, Any] | None = None,
        working_assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        provider = self.llm_provider
        if provider is None:
            raise ValueError("llm provider is required for error analysis")
        if _is_non_sql_generation_failure(sql_ic, execution_error):
            return {
                "summary": (
                    "The generator did not return an analyzable read-only SQL query; "
                    "there is no ontology-learnable SQL error."
                ),
                "confirmed_errors": [],
                "repair_groups": [],
                "error_candidates": [],
                "non_learnable_failure": {
                    "kind": "candidate_generation_protocol_failure",
                    "reason": execution_error,
                },
            }
        trace_capture: dict[str, Any] = {}
        result = analyze_execution_failure(
            provider,
            payload={
                "question": question,
                "gold_sql": sql_gt,
                "predicted_sql": sql_ic,
                "execution_error": execution_error,
                "database_dialect": self.config.database.dialect,
                "table_schema": table_schema,
                "matched_ontology": matched_ontology,
                "draft_metrics": draft_metrics,
                "evaluation_contract": self._review_evaluation_contract(evaluation_contract),
                "accepted_working_assets": list(working_assets or []),
            },
            trace_capture=trace_capture,
        )
        payload = result.model_dump(mode="json")
        payload["error_candidates"] = list(payload["confirmed_errors"])
        payload["__llm_trace"] = trace_capture
        return payload

    def _review_evaluation_contract(
        self,
        evaluation_contract: dict[str, Any] | None,
        *,
        gold_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gold_columns = (
            [str(item) for item in gold_result.get("columns", [])]
            if isinstance(gold_result, dict)
            else list((evaluation_contract or {}).get("gold_columns", []))
        )
        return result_evaluation_contract(gold_columns)

    def _final_comparison(
        self,
        sql_gt: str,
        sql_ic: str,
        *,
        timeout: float,
        gold_cache: GoldQueryCache | None = None,
    ) -> dict[str, Any]:
        database_url = self.config.database.url or ""
        if gold_cache is None:
            gold = execute_query(sql_gt, database_url=database_url, timeout=timeout)
        else:
            gold, _ = gold_cache.execute(
                sql_gt,
                database_url=database_url,
                timeout=timeout,
            )
        predicted = execute_query(sql_ic, database_url=database_url, timeout=timeout)

        final: dict[str, Any] = {
            "gold_executable": gold.success and gold.result is not None,
            "incorrect_executable": predicted.success and predicted.result is not None,
        }
        if not gold.success or gold.result is None:
            final["gold_error"] = gold.error
            final["reason"] = gold.error
            if predicted.success and predicted.result is not None:
                final["predicted_result"] = predicted.result
            else:
                final["predicted_error"] = predicted.error
            return final

        final["gold_result"] = gold.result
        if not predicted.success or predicted.result is None:
            final["predicted_error"] = predicted.error
            final["reason"] = predicted.error
            return final

        evaluation = evaluate(sql_gt, predicted.result, gold.result)
        final.update(
            {
                "result_equal": evaluation["exec"] == 1,
                "reason": evaluation["reason"],
                "evaluation": evaluation,
                "predicted_result": predicted.result,
            }
        )
        return final


def _is_non_sql_generation_failure(sql: str, execution_error: str) -> bool:
    normalized = sql.strip().lower()
    if normalized.startswith(("select", "with")):
        return False
    error = execution_error.lower()
    return (
        not normalized
        or "generated sql must be one read-only select or with query" in error
        or "generated sql contains a non-read-only statement" in error
    )
