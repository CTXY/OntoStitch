from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Any

from cortex.capabilities.sql_training.evaluation import evaluate
from cortex.capabilities.sql_training.runtime_config import CortexConfig
from cortex.capabilities.sql_training.training.trace import TrainTraceLogger
from cortex.capabilities.sql_training.validation import build_generation_request
from cortex.capabilities.sql_training.validation.in_process import (
    OntologyGenerationError,
    OntologyGenerationUnavailable,
)
from cortex.capabilities.sql_training.validation.models import GenerationResponse
from cortex.infrastructure.database.execution import QueryExecutionResult, execute_query
from cortex.schema import GraphSpace

GenerateSQL = Callable[..., tuple[GenerationResponse | None, list[str]]]
RetrievalObserver = Callable[[dict[str, Any], set[str]], None]


class GoldQueryCache:
    """Run-local cache for Gold SQL execution results.

    Gold results are immutable for one training run. Caching both successes and failures keeps
    repeated induction/retention replays from executing the same expensive oracle query again;
    the cache is deliberately owned by one TrainPipeline.run call and never persisted.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], QueryExecutionResult] = {}
        self._lock = Lock()

    def execute(
        self,
        sql: str,
        *,
        database_url: str,
        timeout: float,
    ) -> tuple[QueryExecutionResult, bool]:
        key = (database_url, sql)
        with self._lock:
            cached = self._entries.get(key)
        if cached is not None:
            return cached, True
        result = execute_query(sql, database_url=database_url, timeout=timeout)
        with self._lock:
            self._entries.setdefault(key, result)
            return self._entries[key], False


class GenerationUnavailableReason(str):
    """String-compatible diagnostic with typed infrastructure facts in-process."""

    def __new__(cls, error: OntologyGenerationUnavailable):
        reason = super().__new__(cls, str(error))
        reason.error_type = error.error_type
        reason.http_status = error.http_status
        reason.retryable = error.retryable
        return reason


def evaluate_record(
    config: CortexConfig,
    record: dict[str, Any],
    *,
    generate_sql: GenerateSQL,
    trace_id: str,
    sql_timeout: float,
    source_id: str | None = None,
    case_index: int | None = None,
    phase: str | None = None,
    trace_logger: TrainTraceLogger | None = None,
    retrieval_graph: GraphSpace,
    force_generate: bool = False,
    gold_cache: GoldQueryCache | None = None,
) -> dict[str, Any]:
    baseline_query = str(record.get("generated_sql") or "").strip()
    if not force_generate and phase in {"baseline", "joint_baseline"} and baseline_query:
        errors: list[str] = []
        query = baseline_query
    else:
        generation, errors = generate_sql(
            record["question"],
            trace_id=trace_id,
            source_id=source_id,
            case_index=case_index,
            phase=phase,
            trace_logger=trace_logger,
            retrieval_graph=retrieval_graph,
        )
        query = first_exec_query(generation)
    if errors or not query:
        failure_message = "; ".join(errors) or "validation returned no SQL"
        if not query:
            query = candidate_sql_from_failure_message(failure_message)
        infrastructure_failure = bool(
            errors
            and all(
                isinstance(error, GenerationUnavailableReason) or error.startswith(
                    (
                        "validation service request failed:",
                        "validation service returned HTTP",
                        "validation service returned invalid JSON",
                        "validation service response must be an object",
                        "validation intent response schema is invalid:",
                        "validation generation response schema is invalid:",
                        "Cortex RetrieveDB failed:",
                        "Cortex RetrieveDB did not produce a completed subGraph:",
                        "SQL generation infrastructure unavailable:",
                    )
                )
                for error in errors
            )
        )
        failure_kind = "infrastructure" if infrastructure_failure else "generation" if not query else "validation"
        result = {
            "correct": False,
            "evaluation_available": not infrastructure_failure,
            "evaluation_status": (
                "evaluation_unavailable"
                if infrastructure_failure
                else f"candidate_{failure_kind}_failed"
            ),
            "trace_id": trace_id,
            "query": query,
            "reason": failure_message,
            "evaluation": {"exec": 0, "reason": failure_message},
            "candidate_failure": {
                "kind": failure_kind,
                "message": failure_message,
                "query": query,
                "retryable": all(getattr(error, "retryable", True) for error in errors) if infrastructure_failure else True,
            },
        }
        if infrastructure_failure:
            result["infrastructure_errors"] = [
                {"error_type": error.error_type, "http_status": error.http_status,
                 "retryable": error.retryable}
                for error in errors if isinstance(error, GenerationUnavailableReason)
            ]
        trace_sql_evaluation(
            trace_logger,
            source_id=source_id,
            case_index=case_index,
            phase=phase,
            record=record,
            query=query,
            result=result,
        )
        return result

    if gold_cache is None:
        gold = execute_query(
            record["sql"],
            database_url=config.database.url or "",
            timeout=sql_timeout,
        )
        gold_cache_hit = False
    else:
        gold, gold_cache_hit = gold_cache.execute(
            record["sql"],
            database_url=config.database.url or "",
            timeout=sql_timeout,
        )
    predicted = execute_query(
        query,
        database_url=config.database.url or "",
        timeout=sql_timeout,
    )
    if not gold.success or gold.result is None:
        reason = f"gold SQL execution failed: {gold.error}"
        result = execution_eval(False, trace_id, query, reason, gold, predicted)
        result.update(
            {
                "evaluation_available": False,
                "evaluation_status": (
                    "gold_execution_timeout"
                    if gold.error and "timed out" in gold.error
                    else "gold_execution_failed"
                ),
                "oracle_unavailable": True,
                "gold_cache_hit": gold_cache_hit,
            }
        )
        trace_sql_evaluation(
            trace_logger,
            source_id=source_id,
            case_index=case_index,
            phase=phase,
            record=record,
            query=query,
            result=result,
        )
        return result
    if not predicted.success or predicted.result is None:
        reason = f"predicted SQL execution failed: {predicted.error}"
        result = execution_eval(False, trace_id, query, reason, gold, predicted)
        result["evaluation_status"] = "predicted_sql_execution_failed"
        trace_sql_evaluation(
            trace_logger,
            source_id=source_id,
            case_index=case_index,
            phase=phase,
            record=record,
            query=query,
            result=result,
        )
        return result

    evaluation_contract = result_evaluation_contract(result_columns(gold.result))
    evaluation = evaluate(
        record["sql"],
        predicted.result,
        gold.result,
    )
    evaluation_unavailable = evaluation["exec"] == -1
    result = {
        "correct": evaluation["exec"] == 1,
        "evaluation_status": (
            "evaluation_unavailable"
            if evaluation_unavailable
            else "correct"
            if evaluation["exec"] == 1
            else "result_mismatch"
        ),
        "evaluation_available": not evaluation_unavailable,
        "trace_id": trace_id,
        "query": query,
        "reason": evaluation["reason"],
        "evaluation": evaluation,
        "evaluation_contract": evaluation_contract,
        "gold_result": gold.result,
        "predicted_result": predicted.result,
        "gold_cache_hit": gold_cache_hit,
    }
    trace_sql_evaluation(
        trace_logger,
        source_id=source_id,
        case_index=case_index,
        phase=phase,
        record=record,
        query=query,
        result=result,
    )
    return result


def generate_sql(
    config: CortexConfig,
    validation_client: Any,
    question: str,
    *,
    observe_retrieval: RetrievalObserver,
    trace_id: str,
    source_id: str | None = None,
    case_index: int | None = None,
    phase: str | None = None,
    trace_logger: TrainTraceLogger | None = None,
    retrieval_graph: GraphSpace,
) -> tuple[GenerationResponse | None, list[str]]:
    errors: list[str] = []
    try:
        retrieval = validation_client.retrieve(
            question=question,
            graph=retrieval_graph,
            dialect=config.validation.dialect,
            trace_id=trace_id,
        )
    except Exception as exc:
        trace_validation_error(
            trace_logger,
            source_id=source_id,
            case_index=case_index,
            phase=phase,
            step="cortex_retrieve_db",
            request={"question": question, "dialect": config.validation.dialect},
            error=str(exc),
        )
        return None, [f"Cortex RetrieveDB failed: {exc}"]

    if retrieval.status != "completed" or retrieval.subgraph is None:
        message = f"Cortex RetrieveDB did not produce a completed subGraph: {retrieval.status}"
        trace_validation_error(
            trace_logger,
            source_id=source_id,
            case_index=case_index,
            phase=phase,
            step="cortex_retrieve_db",
            request={"question": question, "dialect": config.validation.dialect},
            error=message,
        )
        return None, [message]

    graph_nodes = [node.model_dump(mode="json") for node in retrieval.subgraph.nodes]
    intent_output = {
        "data": {
            "raw_retrieved_asset_ids": list(retrieval.raw_retrieved_node_ids),
            "selected_asset_ids": list(retrieval.selected_node_ids),
            "matched_ontology": graph_nodes,
            "context_edge_ids": [edge.id for edge in retrieval.subgraph.edges],
        }
    }
    observe_retrieval(intent_output, set(retrieval.selected_node_ids))
    trace_validation_call(
        trace_logger,
        source_id=source_id,
        case_index=case_index,
        phase=phase,
        step="cortex_retrieve_db",
        request={"question": question, "dialect": config.validation.dialect},
        response=retrieval,
        summary={
            "nodes": len(retrieval.subgraph.nodes),
            "edges": len(retrieval.subgraph.edges),
            "selected_nodes": len(retrieval.selected_node_ids),
        },
    )

    generation_request = build_generation_request(
        config.validation,
        question=question,
        trace_id=trace_id,
        graph_space=retrieval.subgraph,
        join_paths=tuple(path.model_dump(mode="json") for path in retrieval.paths),
    )
    try:
        generation = validation_client.generate(generation_request)
    except OntologyGenerationUnavailable as exc:
        trace_validation_error(
            trace_logger, source_id=source_id, case_index=case_index,
            phase=phase, step="ontology_sql_generation",
            request=generation_request, error=str(exc),
        )
        return None, [GenerationUnavailableReason(exc)]
    except OntologyGenerationError as exc:
        trace_validation_error(
            trace_logger,
            source_id=source_id,
            case_index=case_index,
            phase=phase,
            step="ontology_sql_generation",
            request=generation_request,
            error=str(exc),
        )
        return None, [str(exc)]

    errors.extend(
        str(row.error_message)
        for row in generation.data
        if row.error_message is not None and str(row.error_message).strip()
    )
    query = first_exec_query(generation)
    trace_validation_call(
        trace_logger,
        source_id=source_id,
        case_index=case_index,
        phase=phase,
        step="ontology_sql_generation",
        request=generation_request,
        response=generation,
        summary={"queries": [query] if query else [], "errors": errors},
    )
    return generation, errors


def result_evaluation_contract(gold_columns: list[str]) -> dict[str, Any]:
    """Describe the single full-row-set comparison contract to repair agents."""
    return {
        "evaluator": "row_set_equality",
        "row_order": "ignore",
        "gold_columns": list(gold_columns),
        "non_learnable_obligations": [
            "Differences in result-row ordering and duplicate multiplicity are ignored."
        ],
    }


def result_columns(result: Any) -> list[str]:
    if isinstance(result, dict) and isinstance(result.get("columns"), list):
        return [str(column) for column in result["columns"]]
    return []


def first_exec_query(response: GenerationResponse | None) -> str | None:
    if response is None:
        return None
    for row in response.data:
        query = (row.exec_query or row.query or "").strip()
        if query:
            return query
    return None


def candidate_sql_from_failure_message(message: str) -> str | None:
    marker = "[SQL: "
    start = message.find(marker)
    if start < 0:
        return None
    start += len(marker)
    background = message.find("\n(Background on this error", start)
    end = background if background >= 0 else message.rfind("]")
    if end <= start:
        return None
    query = message[start:end].strip()
    if query.endswith("]"):
        query = query[:-1].rstrip()
    return query or None


def execution_eval(
    correct: bool,
    trace_id: str,
    query: str,
    reason: str,
    gold: QueryExecutionResult,
    predicted: QueryExecutionResult,
) -> dict[str, Any]:
    return {
        "correct": correct,
        "trace_id": trace_id,
        "query": query,
        "reason": reason,
        "evaluation": {"exec": 0, "reason": reason},
        "gold_result": gold.result,
        "predicted_result": predicted.result,
        "gold_error": gold.error,
        "predicted_error": predicted.error,
    }


def trace_validation_call(
    trace_logger: TrainTraceLogger | None,
    *,
    source_id: str | None,
    case_index: int | None,
    phase: str | None,
    step: str,
    request: Any,
    response: Any,
    summary: dict[str, Any],
) -> None:
    if trace_logger is None:
        return
    trace_logger.log(
        source_id=source_id,
        case_index=case_index,
        step=step,
        phase=phase,
        input=request,
        output=response,
        summary=summary,
    )


def trace_validation_error(
    trace_logger: TrainTraceLogger | None,
    *,
    source_id: str | None,
    case_index: int | None,
    phase: str | None,
    step: str,
    request: Any,
    error: str,
) -> None:
    if trace_logger is None:
        return
    trace_logger.log(
        source_id=source_id,
        case_index=case_index,
        step=step,
        phase=phase,
        input=request,
        output={"error": error},
        summary={"error": error},
    )


def trace_sql_evaluation(
    trace_logger: TrainTraceLogger | None,
    *,
    source_id: str | None,
    case_index: int | None,
    phase: str | None,
    record: dict[str, Any],
    query: str | None,
    result: dict[str, Any],
) -> None:
    if trace_logger is None:
        return
    trace_logger.log(
        source_id=source_id,
        case_index=case_index,
        step="sql_evaluation",
        phase=phase,
        input={
            "question": record.get("question"),
            "gold_sql": record.get("sql"),
            "predicted_sql": query,
        },
        output=result,
        summary={
            "correct": result.get("correct"),
            "reason": result.get("reason"),
            "query": query,
        },
    )
