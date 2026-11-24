from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from time import perf_counter, sleep
from typing import Any

from langchain_openai import OpenAIEmbeddings

from nl2sql_baseline.amem import AmemRetrievalAdapter
from nl2sql_baseline.config import Settings
from nl2sql_baseline.database import Database, SchemaSnapshot
from nl2sql_baseline.evaluation import evaluate_results
from nl2sql_baseline.generation import SqlGenerator
from nl2sql_baseline.io import load_qa_records, write_json, write_jsonl
from nl2sql_baseline.models import QARecord, RetrievedExample
from nl2sql_baseline.retrieval import (
    BM25Retriever,
    CharacterTfidfRetriever,
    EmbeddingRetriever,
)
from nl2sql_baseline.schema_linking import select_schema
from nl2sql_baseline.tk_boost import TkBoostRepairRuleAdapter


class Workflow:
    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        generator: SqlGenerator | None = None,
    ):
        self.settings = settings
        self._injected_database = database
        self._databases: dict[str, Database] = {}
        self._schemas: dict[str, SchemaSnapshot] = {}
        self.generator = generator or SqlGenerator(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            temperature=settings.temperature,
            timeout=settings.timeout,
        )
        self._amem: AmemRetrievalAdapter | None = None
        self._tk_boost: TkBoostRepairRuleAdapter | None = None

    def preflight(self, *, execute_gold: bool = True) -> dict[str, Any]:
        examples = self._load_examples()
        tests = load_qa_records(self.settings.test_set_path)
        guidance = self._load_downstream_guidance(tests)
        if self.settings.retriever_type != "none" and len(examples) < self.settings.top_k:
            raise ValueError(f"top_k={self.settings.top_k} exceeds example count={len(examples)}")
        gold_checks = 0
        database_rows: list[dict[str, Any]] = []
        for database_id in sorted({record.database_id for record in tests}):
            database = self._database_for(database_id)
            database.ping()
            schema = self._schema_for(database_id)
            database_rows.append(
                {
                    "database_id": database_id,
                    "tables": len(schema.catalog["tables"]),
                    "schema_sha256": _sha256(schema.ddl),
                }
            )
        if execute_gold:
            for record in tests:
                self._database_for(record.database_id).execute_read_only(record.sql)
                gold_checks += 1
        return {
            "status": "ready",
            "dataset": self.settings.dataset_type,
            "examples": len(examples),
            "test_cases": len(tests),
            "databases": database_rows,
            "gold_queries_checked": gold_checks,
            "guidance_cases": len(guidance),
            "missing_guidance": self._missing_guidance(tests, guidance),
        }

    def evaluate(
        self,
        output: Path | None = None,
        *,
        resume_sql_attempts: dict[str, list[dict[str, Any]]] | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        examples = self._load_examples()
        tests = load_qa_records(self.settings.test_set_path)
        guidance = self._load_downstream_guidance(tests)
        run = _reserve_run(output or self.settings.workspace_root)
        retrievers = self._build_retrievers(examples)
        predictions: list[dict[str, Any]] = []
        retrieval_rows: list[dict[str, Any]] = []
        linking_rows: list[dict[str, Any]] = []
        for record in tests:
            started = perf_counter()
            database = self._database_for(record.database_id)
            schema = self._schema_for(record.database_id)
            _write_input_schema(run, record.database_id, schema)
            retrieved = self._retrieve(retrievers, record)
            retrieval_rows.append(
                {
                    "source_id": record.source_id,
                    "database_id": record.database_id,
                    "question": record.question,
                    "examples": [
                        {"source_id": item.record.source_id, "rank": item.rank, "score": item.score}
                        for item in retrieved
                    ],
                }
            )
            selection = select_schema(
                schema,
                database,
                question=record.question,
                evidence=record.evidence,
                mode=self.settings.schema_linking_type,
                table_top_k=self.settings.schema_table_top_k,
                column_top_k=self.settings.schema_column_top_k,
                value_top_k=self.settings.schema_value_top_k,
                values_per_column=self.settings.schema_values_per_column,
            )
            linking_rows.append(
                {
                    "source_id": record.source_id,
                    "database_id": record.database_id,
                    "mode": selection.mode,
                    "tables": selection.tables,
                    "value_matches": selection.value_matches,
                }
            )
            predicted_sql = ""
            sql_attempts = [dict(item) for item in (resume_sql_attempts or {}).get(record.source_id, [])]
            if len(sql_attempts) > 2 or any(
                not isinstance(item.get("sql"), str) or not item.get("error")
                for item in sql_attempts
            ):
                raise ValueError("Only unfinished SQL correction attempts may be resumed")
            evaluation: dict[str, Any]
            error: str | None = None
            error_stage: str | None = None
            stage = "generation"
            try:
                case_guidance = guidance.get(record.source_id, "")
                if self.settings.memory_type == "amem_retrieval":
                    assert self._amem is not None
                    case_guidance = self._amem.memory_for(record)
                elif self.settings.memory_type == "tk_boost_repair_rules":
                    assert self._tk_boost is not None
                    case_guidance = self._tk_boost.memory_for(record)
                generation_args = {
                    "question": record.question,
                    "evidence": record.evidence,
                    "schema_ddl": selection.ddl,
                    "examples": retrieved,
                    "reference_time": self.settings.reference_time,
                    "dialect": schema.catalog["dialect"],
                    "downstream_guidance": (
                        case_guidance
                        if self.settings.memory_type == "downstream_guidance"
                        else ""
                    ),
                    "reference_memory": (
                        case_guidance
                        if self.settings.memory_type
                        in {"reference_guidance", "amem_retrieval", "tk_boost_repair_rules"}
                        else ""
                    ),
                }
                prompt_name = _artifact_name(record)
                (run / "linked_schemas").mkdir(parents=True, exist_ok=True)
                (run / "linked_schemas" / f"{prompt_name}.sql").write_text(
                    selection.ddl + "\n", encoding="utf-8"
                )
                if sql_attempts:
                    generation_args.update(
                        previous_sql=sql_attempts[-1]["sql"],
                        execution_error=sql_attempts[-1]["error"],
                    )
                for attempt in range(len(sql_attempts), 3):
                    stage = "generation"
                    predicted_sql, messages = self._generate_with_retries(**generation_args)
                    write_json(run / "prompts" / f"{prompt_name}.json", messages)
                    write_json(
                        run / "prompts" / f"{prompt_name}-attempt-{attempt + 1}.json",
                        messages,
                    )
                    stage = "sql_execution"
                    try:
                        predicted = database.execute_read_only(predicted_sql)
                    except Exception as exc:  # SQL errors, never EA feedback, request a correction.
                        sql_attempts.append({"sql": predicted_sql, "error": str(exc)})
                        write_json(run / "sql_attempts" / f"{prompt_name}.json", sql_attempts)
                        if attempt == 2:
                            raise
                        generation_args.update(previous_sql=predicted_sql, execution_error=str(exc))
                    else:
                        sql_attempts.append({"sql": predicted_sql, "error": None})
                        write_json(run / "sql_attempts" / f"{prompt_name}.json", sql_attempts)
                        break
                stage = "gold_execution"
                gold = database.execute_read_only(record.sql)
                stage = "evaluation"
                evaluation = evaluate_results(predicted, gold)
            except Exception as exc:  # noqa: BLE001 - keep one failed case reportable.
                error = str(exc)
                error_stage = stage
                evaluation = {"exec": 0, "reason": error}
            predictions.append(
                {
                    "source_id": record.source_id,
                    "database_id": record.database_id,
                    "question": record.question,
                    "gold_sql": record.sql,
                    "predicted_sql": predicted_sql,
                    "sql_attempts": sql_attempts,
                    "evaluator": "row_set_equality",
                    "evaluation": evaluation,
                    "error": error,
                    "error_stage": error_stage,
                    "guidance_available": bool(guidance.get(record.source_id)),
                    "latency_seconds": round(perf_counter() - started, 6),
                }
            )
            write_jsonl(run / "predictions.jsonl", predictions)
            write_jsonl(run / "retrievals.jsonl", retrieval_rows)
            write_jsonl(run / "schema_linking.jsonl", linking_rows)
        correct = sum(row["evaluation"]["exec"] == 1 for row in predictions)
        report = {
            "total": len(predictions),
            "correct": correct,
            "ea": correct / len(predictions) if predictions else 0,
            "evaluator": "row_set_equality",
            "failed": len(predictions) - correct,
            "dataset": self.settings.dataset_type,
            "schema_linking": self.settings.schema_linking_type,
            "memory": self.settings.memory_type,
            "guidance_cases": len(guidance),
            "missing_guidance": self._missing_guidance(tests, guidance),
            "retriever": self.settings.retriever_type,
            "top_k": self.settings.top_k if self.settings.retriever_type != "none" else 0,
        }
        write_json(run / "report.json", report)
        write_json(
            run / "run.json",
            {
                "config": str(self.settings.config_path),
                "examples": (
                    str(self.settings.examples_path) if self.settings.examples_path else None
                ),
                "test_set": str(self.settings.test_set_path),
                "database_root": (
                    str(self.settings.database_root) if self.settings.database_root else None
                ),
                "include_tables": list(self.settings.include_tables),
                "llm_model": self.settings.llm_model,
                "reference_time": self.settings.reference_time,
                **report,
            },
        )
        return run, report

    def _load_examples(self) -> list[QARecord]:
        if self.settings.retriever_type == "none" or self.settings.examples_path is None:
            return []
        return load_qa_records(self.settings.examples_path)

    def _generate_with_retries(self, **kwargs: Any) -> tuple[str, list[dict[str, str]]]:
        last_error: Exception | None = None
        for attempt in range(1, 6):
            try:
                return self.generator.generate(**kwargs)
            except Exception as exc:
                if not _is_transient_generation_error(exc) or attempt == 5:
                    raise
                last_error = exc
                sleep(min(60, 2 ** attempt))
        assert last_error is not None
        raise last_error

    def _load_downstream_guidance(self, tests: list[QARecord]) -> dict[str, str]:
        if self.settings.memory_type == "amem_retrieval":
            assert self.settings.guidance_dir is not None
            self._amem = AmemRetrievalAdapter(
                self.settings.guidance_dir, tests, top_k=self.settings.top_k
            )
            return {record.source_id: "<A-MEM retrieval loaded>" for record in tests}
        if self.settings.memory_type == "tk_boost_repair_rules":
            assert self.settings.guidance_dir is not None
            self._tk_boost = TkBoostRepairRuleAdapter(
                self.settings.guidance_dir, tests, top_k=self.settings.top_k
            )
            return {record.source_id: "<TK-Boost repair rules loaded>" for record in tests}
        if self.settings.memory_type not in {"downstream_guidance", "reference_guidance"}:
            return {}
        assert self.settings.guidance_dir is not None
        if self.settings.guidance_dir.is_file():
            return self._load_consolidated_guidance(tests, self.settings.guidance_dir)
        if not self.settings.guidance_dir.is_dir():
            raise ValueError(
                f"downstream guidance path does not exist: {self.settings.guidance_dir}"
            )
        guidance: dict[str, str] = {}
        for record in tests:
            path = self.settings.guidance_dir / f"{record.source_id}.json"
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            source_id = str(payload.get("source_id") or "")
            if source_id and source_id != record.source_id:
                raise ValueError(
                    f"{path}: source_id {source_id!r} does not match {record.source_id!r}"
                )
            artifact_question = str(payload.get("question") or "").strip()
            if artifact_question and artifact_question != record.question:
                raise ValueError(f"{path}: question does not match test case {record.source_id}")
            downstream = payload.get("downstream_guidance") or {}
            handoff = downstream.get("agent_handoff") or {}
            content = str(handoff.get("guidance_content") or "").strip()
            if content:
                guidance[record.source_id] = content
        return guidance

    def _load_consolidated_guidance(
        self, tests: list[QARecord], path: Path
    ) -> dict[str, str]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("cases") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"{path}: consolidated guidance must contain a cases array")
        expected = {record.source_id: record for record in tests}
        guidance: dict[str, str] = {}
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise ValueError(f"{path}: cases[{index}] must be an object")
            source_id = str(row.get("source_id") or "").strip()
            if not source_id:
                raise ValueError(f"{path}: cases[{index}] requires source_id")
            if source_id in guidance:
                raise ValueError(f"{path}: duplicate source_id {source_id!r}")
            if source_id not in expected:
                continue
            artifact_question = str(row.get("question") or "").strip()
            if artifact_question and artifact_question != expected[source_id].question:
                raise ValueError(f"{path}: question does not match test case {source_id}")
            downstream = row.get("downstream_guidance") or {}
            handoff = row.get("agent_handoff") or downstream.get("agent_handoff") or {}
            if not isinstance(handoff, dict):
                raise ValueError(f"{path}: {source_id} agent_handoff must be an object")
            content = str(
                row.get("guidance_content") or handoff.get("guidance_content") or ""
            ).strip()
            if content:
                guidance[source_id] = content
        return guidance

    def _missing_guidance(
        self, tests: list[QARecord], guidance: dict[str, str]
    ) -> list[str]:
        if self.settings.memory_type not in {
            "downstream_guidance",
            "reference_guidance",
            "tk_boost_repair_rules",
        }:
            return []
        return [record.source_id for record in tests if record.source_id not in guidance]

    def _database_for(self, database_id: str) -> Database:
        if self._injected_database is not None:
            return self._injected_database
        if database_id not in self._databases:
            assert self.settings.database_root is not None
            path = self.settings.database_root / database_id / f"{database_id}.sqlite"
            if not path.is_file():
                raise ValueError(f"Dataset database does not exist: {path}")
            self._databases[database_id] = Database(
                f"sqlite:///{path}", query_timeout=self.settings.sql_timeout
            )
        return self._databases[database_id]

    def _schema_for(self, database_id: str) -> SchemaSnapshot:
        if database_id not in self._schemas:
            assert self.settings.database_root is not None
            description_dir = self.settings.database_root / database_id / "database_description"
            self._schemas[database_id] = self._database_for(database_id).introspect_schema(
                self.settings.include_tables,
                description_dir=description_dir,
            )
        return self._schemas[database_id]

    def _build_retrievers(self, examples: list[QARecord]) -> dict[str, Any]:
        if self.settings.retriever_type == "none":
            return {}
        grouped: dict[str, list[QARecord]] = {}
        for example in examples:
            grouped.setdefault(example.database_id, []).append(example)
        retrievers: dict[str, Any] = {}
        for database_id, records in grouped.items():
            if len(records) < self.settings.top_k:
                continue
            if self.settings.retriever_type == "char_tfidf":
                retrievers[database_id] = CharacterTfidfRetriever(records)
            elif self.settings.retriever_type == "bm25":
                retrievers[database_id] = BM25Retriever(records)
            elif self.settings.retriever_type == "embedding":
                if not self.settings.embedding_model:
                    raise ValueError("retrieval.model is required for embedding retrieval")
                embeddings = OpenAIEmbeddings(
                    model=self.settings.embedding_model,
                    api_key=self.settings.llm_api_key,
                    base_url=self.settings.llm_base_url,
                )
                retrievers[database_id] = EmbeddingRetriever(records, embeddings)
            else:
                raise ValueError(f"unsupported retrieval.type: {self.settings.retriever_type}")
        return retrievers

    def _retrieve(self, retrievers: dict[str, Any], record: QARecord) -> list[RetrievedExample]:
        if self.settings.retriever_type == "none":
            return []
        retriever = retrievers.get(record.database_id)
        if retriever is None:
            return []
        return retriever.retrieve(record.question, top_k=self.settings.top_k)


def _write_input_schema(run: Path, database_id: str, schema: SchemaSnapshot) -> None:
    inputs = run / "inputs" / database_id
    ddl_path = inputs / "database_schema.sql"
    if ddl_path.exists():
        return
    inputs.mkdir(parents=True, exist_ok=True)
    ddl_path.write_text(schema.ddl + "\n", encoding="utf-8")
    write_json(inputs / "database_schema.json", schema.catalog)


def _reserve_run(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    prefix = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    candidate = root / prefix
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = root / f"{prefix}-{suffix}"
    candidate.mkdir()
    return candidate


def _artifact_name(record: QARecord) -> str:
    raw = f"{record.database_id}-{record.source_id}"
    return "".join(
        character if character.isalnum() or character in "-_" else "_" for character in raw
    )


def _is_transient_generation_error(exc: Exception) -> bool:
    text = " ".join(
        str(item)
        for item in (
            type(exc).__name__,
            exc,
            getattr(exc, "__cause__", ""),
            getattr(exc, "__context__", ""),
        )
    ).lower()
    return any(
        marker in text
        for marker in (
            "connection error",
            "api connection",
            "connecterror",
            "timeout",
            "timed out",
            "rate limit",
            "temporarily unavailable",
        )
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
