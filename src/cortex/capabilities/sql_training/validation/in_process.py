from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import sqlglot
import yaml
from langchain_core.language_models.chat_models import BaseChatModel
from sqlglot import expressions

from cortex.capabilities.sql_training.runtime_config import RuntimeValidationConfig
from cortex.capabilities.sql_training.validation.models import (
    GenerationRequest,
    GenerationResponse,
    GenerationResult,
)
from cortex.infrastructure.database import DatabaseConnector


class OntologyGenerationError(RuntimeError):
    """An in-process ontology SQL generation attempt could not complete."""


class InProcessOntologySqlGenerator:
    """Train-only GraphSpace-to-SQL validator.

    The caller has already selected the GraphSpace through RetrieveDB.  This class
    deliberately has no retrieval, HTTP, token, or platform concerns.
    """

    def __init__(
        self,
        *,
        llm: BaseChatModel,
        database: DatabaseConnector | None,
        config: RuntimeValidationConfig,
    ) -> None:
        self._llm = llm
        self._database = database
        self.config = config

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        return asyncio.run(self.agenerate(request))

    async def agenerate(self, request: GenerationRequest) -> GenerationResponse:
        started = perf_counter()
        accepted_query = ""
        error: str | None = None
        accepted_result: dict[str, Any] | None = None
        execution_time: dict[str, Any] = {}
        attempt_history: list[dict[str, Any]] = []
        attempts = request.max_retry_count + 1
        wrong_query: str | None = None

        for attempt in range(attempts):
            attempt_record: dict[str, Any] = {"attempt": attempt + 1}
            attempt_history.append(attempt_record)
            query = ""
            generation_started = perf_counter()
            try:
                response = await self._llm.ainvoke(
                    _generation_messages(
                        request,
                        wrong_query=wrong_query,
                        error=error,
                    )
                )
                raw_output = _content(response)
                attempt_record["generation_output"] = raw_output
                query = _clean_sql(raw_output)
                if not query:
                    raise OntologyGenerationError("SQL generation returned an empty response")
                if query.casefold() in {"continue", "retry"}:
                    raise OntologyGenerationError(
                        "SQL generation returned a reflection control token instead of SQL"
                    )
                _ensure_read_only(query)
            except Exception as exc:
                error = f"SQL generation failed: {exc}"
                attempt_record["error"] = error
                wrong_query = query or None
                if attempt >= request.max_retry_count:
                    break
                continue
            execution_time[f"generation_{attempt + 1}"] = _elapsed(generation_started)
            attempt_record["query"] = query

            if not request.enable_query_execution:
                error = None
                accepted_query = query
                break
            if self._database is None:
                error = "SQL validation database is not configured"
                attempt_record["error"] = error
                break
            try:
                execution_started = perf_counter()
                rows = await asyncio.to_thread(self._database.run_validation_query, query)
                execution_time[f"query_execution_{attempt + 1}"] = _elapsed(execution_started)
                result_json = {"rows": rows}
                attempt_record["executed"] = True
                accepted_query = query
                accepted_result = result_json
                error = None
                break
            except Exception as exc:
                error = str(exc)
                attempt_record["error"] = error
                wrong_query = query

        execution_time["attempts"] = attempt_history
        execution_time["total"] = _elapsed(started)
        return GenerationResponse(
            code=200 if error is None else 400,
            message=error or "",
            data=[
                GenerationResult(
                    query=accepted_query,
                    exec_query=accepted_query or None,
                    result_json=accepted_result,
                    retry_count=attempt,
                    error_message=error,
                    execution_time=execution_time,
                )
            ],
        )


def _generation_messages(
    request: GenerationRequest,
    *,
    wrong_query: str | None,
    error: str | None,
) -> list[dict[str, str]]:
    graph_nodes, graph_relationships = _graph_prompt_values(
        request.graph_space.model_dump(mode="json")
    )
    error_parts = []
    if wrong_query:
        error_parts.append(f"Previous SQL: {wrong_query}")
    if error:
        error_parts.append(f"Execution error: {error}")
    return _render_chatbi_prompt(
        "ontology_query_generation.yaml",
        "ontology_sql_query",
        [
            "error",
            "current_time",
            "task",
            "graph_nodes",
            "graph_relationships",
            "join_paths",
            "dialect",
        ],
        {
            "error": "\n".join(error_parts),
            "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "task": request.question,
            "graph_nodes": graph_nodes,
            "graph_relationships": graph_relationships,
            "join_paths": json.dumps(request.join_paths, ensure_ascii=False, indent=2),
            "dialect": request.dialect,
        },
        "Please provide SQL only in the output, no any other information. Besides, do not wrap sql "
        "in code blocks (```).",
    )


def _render_chatbi_prompt(
    filename: str,
    version: str,
    input_keys: list[str],
    values: dict[str, Any],
    template_output: str,
) -> list[dict[str, str]]:
    content = yaml.safe_load((_PROMPT_DIR / filename).read_text(encoding="utf-8"))[version]
    information = "\n\n".join(f"# {key}\n{values[key]}" for key in input_keys)
    system = (
        content["system_prompt"]
        .replace("{input_keys}", str(input_keys))
        .format(dialect=values["dialect"], template_output=template_output)
    )
    user = content["user_prompt"].replace("{information_str}", information)
    return [{"role": "system", "content": system}, {"role": "human", "content": user}]


def _graph_prompt_values(graph_space: dict[str, Any]) -> tuple[str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw_node in graph_space.get("nodes", []):
        node = dict(raw_node)
        grouped.setdefault(str(node.get("node_type") or "unknown"), []).append(node)
    for nodes in grouped.values():
        nodes.sort(key=lambda node: str(node.get("id") or ""))
    edges = [dict(edge) for edge in graph_space.get("edges", [])]
    edges.sort(key=lambda edge: str(edge.get("id") or ""))
    return (
        json.dumps(grouped, ensure_ascii=False, indent=2),
        json.dumps(edges, ensure_ascii=False, indent=2),
    )


def _content(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", "")) for item in content
        )
    return str(content)


def _clean_sql(text: str) -> str:
    text = _reasoning_text(text)
    return re.sub(r"```(?:sql)?\n?(.*?)\n?```", r"\1", text, flags=re.DOTALL).strip()


def _reasoning_text(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    answers = re.findall(r"<answer>(.*?)</answer>", text, flags=re.DOTALL)
    if answers:
        return answers[-1].strip()
    return text.strip()


def _ensure_read_only(sql: str) -> None:
    try:
        statements = [statement for statement in sqlglot.parse(sql, read="sqlite") if statement]
    except sqlglot.errors.ParseError as exc:
        raise OntologyGenerationError(f"generated SQL failed to parse: {exc}") from exc
    if len(statements) != 1:
        raise OntologyGenerationError("generated SQL must be one read-only SELECT or WITH query")
    if not isinstance(statements[0], expressions.Query):
        raise OntologyGenerationError("generated SQL must be one read-only SELECT or WITH query")


def _elapsed(started: float) -> float:
    return round(perf_counter() - started, 6)


_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


__all__ = ["InProcessOntologySqlGenerator", "OntologyGenerationError"]
