"""Adapters to existing SQL generation, comparison and Diagnosis domain services."""

from __future__ import annotations

from pathlib import Path
import os
from typing import Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel

from cortex.capabilities.sql_training.artifacts.jsonl import read_json
from cortex.capabilities.sql_training.error_analysis.workflow import (
    ErrorAnalysisWorkflow,
)
from cortex.capabilities.sql_training.graph.snapshots import (
    GraphSnapshot,
    GraphStore,
    fingerprint,
    table_schema,
)
from cortex.capabilities.sql_training.learning.inputs import (
    ontology_contract,
    system_prompt,
)
from cortex.capabilities.sql_training.learning.prompt_rendering import (
    render_type_guide,
    render_write_guide,
)
from cortex.capabilities.sql_training.llm import NodeLLMProvider
from cortex.capabilities.sql_training.llm.prompts import prompt_fingerprint
from cortex.capabilities.sql_training.pipeline.execution import (
    GoldQueryCache,
    evaluate_record,
    generate_sql,
)
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
from cortex.capabilities.sql_training.training.evidence import EvidenceStore
from cortex.capabilities.sql_training.training.feedback import assessment_policy_id
from cortex.capabilities.sql_training.training.replay import (
    ReplayAttempt,
    StableReplayResult,
    run_stable_replay,
)
from cortex.capabilities.sql_training.training.trace import TrainTraceLogger


def database_stamp(url: str) -> dict[str, Any]:
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if (
        parsed.drivername.startswith("sqlite")
        and parsed.database
        and parsed.database != ":memory:"
    ):
        from urllib.parse import unquote
        path = Path(unquote(parsed.database.removeprefix("file:")))
        return {
            str(p): (p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None
            for p in (path, Path(str(path) + "-wal"))
        }
    # Non-SQLite experiments require a fixed external database snapshot for a run.
    return {
        "database": parsed.render_as_string(hide_password=True),
        "policy": "fixed_run_snapshot",
    }


class CaseServices:
    def __init__(
        self,
        *,
        config: SqlTrainingRuntimeConfig,
        model: BaseChatModel,
        validation_client: Any,
        graph: GraphStore,
        evidence: EvidenceStore,
        record: dict[str, Any],
        trace: TrainTraceLogger | None = None,
    ):
        self.config, self.model, self.validation_client = (
            config,
            model,
            validation_client,
        )
        self.graph, self.evidence, self.record, self.trace = (
            graph,
            evidence,
            record,
            trace,
        )
        self.gold_cache = GoldQueryCache()
        self._data_stamp = database_stamp(config.database.url or "")
        from cortex.capabilities.sql_training.llm.prompts.asset_contract import (
            ONTOLOGY_ASSET_DEFINITION_VERSION,
        )

        input_contract = ontology_contract(graph.schema)
        # Keep the exact compatibility inputs available for audited, explicit
        # policy upgrades; normal resume still requires an identical fingerprint.
        self.context_contract = {
            "config": config.model_dump(mode="json"),
            "source": record,
            "data": self._data_stamp,
            "gate_policy": "stable-v1",
            "assessment_policy": assessment_policy_id(),
            "asset_contract": ONTOLOGY_ASSET_DEFINITION_VERSION,
            "graph_schema": graph.schema_version,
            "graph_storage_contract": "inline-node-conflicts-v1",
            "learning_input_contract": fingerprint(input_contract),
            "learning_guides": {
                kind: fingerprint(render_type_guide(input_contract, kind))
                for kind in input_contract["editable_node_types"]
            },
            "learning_write_guides": {
                kind: fingerprint(render_write_guide(input_contract, kind))
                for kind in ("dimension_value", "relations")
            },
            "learning_rendered_prompts": {
                mode: fingerprint(system_prompt(mode, graph.schema))
                for mode in ("direct", "repair")
            },
            "learning_prompts": {
                name: prompt_fingerprint(name)
                for name in (
                    "learning_system",
                    "learning_direct",
                    "learning_repair",
                    "learning_assessment",
                )
            },
            "generation_prompt": fingerprint(
                (
                    Path(__file__).parents[1]
                    / "validation"
                    / "prompts"
                    / "ontology_query_generation.yaml"
                ).read_text()
            ),
        }
        self.context_id = fingerprint(self.context_contract)
        if transport_plan := os.environ.get('CORTEX_SQL_TRAINING_TRANSPORT_RESUME_PLAN'):
            from cortex.capabilities.sql_training.training.transport_resume import resume_transport_context
            self.context_contract, self.context_id = resume_transport_context(
                self.context_contract, case_root=evidence.root.parent, plan_path=transport_plan)
        self._index = evidence.read_index()
        self._restore_completed_validations()

    def _restore_completed_validations(self) -> None:
        # A validation fact can be durable before its lookup index is written.
        # Rebuild only that derived lookup, never workflow position or graph heads.
        # Keep this in memory until the workflow has checked resume compatibility.
        for path in self.evidence.root.glob("*.json"):
            record = read_json(path)
            if record.get("kind") != "validation":
                continue
            payload = record["payload"]
            if (
                payload.get("case_id") != self.record["source_id"]
                or payload.get("context_id") != self.context_id
            ):
                continue
            replay = self.replay(record["evidence_id"])
            if replay.status.value == "unavailable":
                continue
            key = fingerprint(
                {"context": self.context_id, "graph": payload["content_fingerprint"]}
            )
            existing = self._index.get(key)
            if existing:
                previous = self.replay(existing["evidence_ref"])
                if previous.status.value != "unavailable":
                    if previous != replay:
                        raise ValueError("conflicting completed validation evidence")
                    continue
            self._index[key] = {
                "evidence_ref": record["evidence_id"],
                "attempts": existing["attempts"] if existing else 1,
            }

    def validate(self, snapshot: GraphSnapshot) -> str:
        if database_stamp(self.config.database.url or "") != self._data_stamp:
            raise ValueError(
                "database changed during the case; start from a fixed data snapshot"
            )
        key = fingerprint(
            {"context": self.context_id, "graph": snapshot.content_fingerprint}
        )
        entry = self._index.get(key)
        retries = entry["attempts"] if entry else 0
        max_attempts = self.config.validation.max_retry_count + 1
        if entry and (
            self.replay(entry["evidence_ref"]).status.value != "unavailable"
            or self.replay(entry["evidence_ref"]).terminal_evaluation.get("candidate_failure", {}).get("retryable") is False
            or retries >= max_attempts
        ):
            return entry["evidence_ref"]
        graph = self.graph.validation_graph(snapshot.revision)

        def attempt(number: int) -> ReplayAttempt:
            observed: dict[str, Any] = {}

            def observe(intent, selected):
                observed.update(intent=intent, selected=sorted(selected))

            def generate(question, **kwargs):
                return generate_sql(
                    self.config,
                    self.validation_client,
                    question,
                    observe_retrieval=observe,
                    **kwargs,
                )

            trace_id = f"case_{self.record['source_id']}_{uuid4().hex}_{number}"
            evaluation = evaluate_record(
                self.config,
                self.record,
                generate_sql=generate,
                trace_id=trace_id,
                sql_timeout=self.config.train.sql_timeout,
                retrieval_graph=graph,
                force_generate=True,
                gold_cache=self.gold_cache,
                source_id=str(self.record["source_id"]),
                trace_logger=self.trace,
                phase="agent_validation",
            )
            intent = observed.get("intent") or {}
            raw = (intent.get("data") or {}).get("raw_retrieved_asset_ids", [])
            return ReplayAttempt(
                attempt=number,
                trace_id=trace_id,
                evaluation=evaluation,
                intent_output=intent,
                selected_ontology_ids=observed.get("selected", []),
                raw_retrieved_ontology_ids=raw if isinstance(raw, list) else [],
            )

        while retries < max_attempts:
            if retries:
                self.gold_cache = (
                    GoldQueryCache()
                )  # a cached oracle outage must not defeat infrastructure retry
            replay = run_stable_replay(
                attempt,
                required_correct_attempts=self.config.train.required_consecutive_correct_replays,
            )
            evidence_id = self.evidence.put(
                "validation",
                {
                    "case_id": self.record["source_id"],
                    "source_fingerprint": fingerprint(self.record),
                    "context_id": self.context_id,
                    "graph_revision": snapshot.revision,
                    "content_fingerprint": snapshot.content_fingerprint,
                    "replay": replay.model_dump(mode="json"),
                },
            )
            retries += 1
            self._index[key] = {"evidence_ref": evidence_id, "attempts": retries}
            self.evidence.write_index(self._index)
            if replay.status.value != "unavailable" or replay.terminal_evaluation.get("candidate_failure", {}).get("retryable") is False:
                break
        return evidence_id

    def reset_unavailable_validation(self) -> None:
        self._index = {
            key: value
            for key, value in self._index.items()
            if self.replay(value["evidence_ref"]).status.value != "unavailable"
        }
        self.evidence.write_index(self._index)

    def replay(self, evidence_id: str) -> StableReplayResult:
        payload = self.evidence.read(evidence_id)["payload"]
        if (
            payload["case_id"] != self.record["source_id"]
            or payload["context_id"] != self.context_id
        ):
            raise ValueError("validation evidence belongs to a different case/context")
        return StableReplayResult.model_validate(payload["replay"])

    def compact_validation(self, evidence_id: str) -> dict[str, Any]:
        replay = self.replay(evidence_id)
        return {
            "evidence_ref": evidence_id,
            "status": replay.status.value,
            "attempts": [
                {
                    "attempt": a.attempt,
                    "sql": a.evaluation.get("query"),
                    "correct": a.evaluation.get("correct"),
                    "reason": a.evaluation.get("reason"),
                    "selected_ids": a.selected_ontology_ids,
                    "raw_retrieved_ids": a.raw_retrieved_ontology_ids,
                    **({"context_ids": [n["id"] for n in a.intent_output["data"]["matched_ontology"]]} if "matched_ontology" in (a.intent_output or {}).get("data", {}) else {}),
                    **({"context_edge_ids": a.intent_output["data"]["context_edge_ids"]} if "context_edge_ids" in (a.intent_output or {}).get("data", {}) else {}),
                    "gold_result": _result_summary(a.evaluation.get("gold_result")),
                    "predicted_result": _result_summary(
                        a.evaluation.get("predicted_result")
                    ),
                }
                for a in replay.attempts
            ],
        }

    def validation_feedback(self, evidence_id):
        from cortex.capabilities.sql_training.training.assessment_inputs import sql_attempts
        compact = self.compact_validation(evidence_id)
        return {**compact, "attempts": sql_attempts(compact)}

    def prior_assessment_evidence(self, baseline_revision, trial_revision):
        """Reuse available comparison evidence; never run a new query for assessment."""
        revisions = {baseline_revision, self.graph.accepted_revision} - {trial_revision}
        rows = []
        for revision in sorted(revisions):
            snapshot = self.graph.read(revision)
            for entry in self._index.values():
                ref = entry["evidence_ref"]
                payload = self.evidence.read(ref)["payload"]
                if payload.get("context_id") != self.context_id or payload.get("content_fingerprint") != snapshot.content_fingerprint:
                    continue
                compact = self.validation_feedback(ref)
                rows.append({"revision": revision, **compact})
                break
        return rows

    def diagnose(
        self, snapshot: GraphSnapshot, validation_ref: str
    ) -> tuple[list[dict[str, Any]], str, str]:
        replay = self.replay(validation_ref)
        evaluation = replay.terminal_evaluation
        if (
            not evaluation.get("query")
            or evaluation.get("evaluation_available") is False
        ):
            return (
                [],
                self.evidence.put(
                    "diagnosis", {"status": "unavailable", "targets": []}
                ),
                "unavailable",
            )
        workflow = ErrorAnalysisWorkflow(
            self.config, llm_provider=NodeLLMProvider(self.model)
        )
        common = {
            "question": self.record["question"],
            "sql_gt": self.record["sql"],
            "sql_ic": evaluation["query"],
            "working_assets": [
                a.model_dump(mode="json") for a in self.graph.accepted.semantic_assets
            ],
        }
        if evaluation.get("predicted_error"):
            report = workflow.run_execution_failure(
                **common,
                execution_error=str(evaluation["predicted_error"]),
                table_schema=table_schema(snapshot.assets),
                matched_ontology=replay.terminal_attempt.intent_output or {},
                draft_metrics=[
                    a.model_dump(mode="json")
                    for a in snapshot.semantic_assets
                    if a.node_type.value == "metric"
                ],
                evaluation_contract=evaluation.get("evaluation_contract"),
            )
        else:
            report = workflow.run(
                **common,
                sql_timeout=self.config.train.sql_timeout,
                evaluation_contract=evaluation.get("evaluation_contract"),
                schema_context=table_schema(snapshot.assets),
                evidence=self.record.get("evidence"),
                gold_column_metadata=self.record.get("gold_column_metadata", {}),
                gold_cache=self.gold_cache,
            )
        targets = diagnosis_targets(report)
        ref = self.evidence.put(
            "diagnosis",
            {
                "graph_revision": snapshot.revision,
                "validation_ref": validation_ref,
                "report": report,
                "targets": targets,
            },
        )
        status = str(
            (report.get("completeness") or {}).get("status")
            or report.get("status")
            or "completed"
        )
        return targets, ref, status


def diagnosis_targets(report: dict[str, Any]) -> list[dict[str, Any]]:
    # Legacy Diagnosis may suggest an owner; it is evidence, never an agent route.
    rows = report.get("confirmed_errors", [])
    if not isinstance(rows, list):
        return []
    targets = []
    for row in rows:
        if hasattr(row, "model_dump"):
            row = row.model_dump(mode="json")
        if isinstance(row, dict) and row.get("id") and row.get("gold_logic"):
            grouped = next(
                (
                    g
                    for g in report.get("repair_groups", [])
                    if row["id"] in g.get("error_ids", [])
                ),
                {},
            )
            targets.append(
                {
                    "target_id": str(row["id"]),
                    "instruction": row["gold_logic"],
                    "difference": row.get("difference_point"),
                    "predicted_logic": row.get("predicted_logic"),
                    "evidence": row.get("necessity"),
                    "group_id": grouped.get("id"),
                    "group_requirement": grouped.get("repair_instruction"),
                }
            )
    if len({t["target_id"] for t in targets}) != len(targets):
        raise ValueError("Diagnosis returned duplicate target IDs")
    return targets


def _result_summary(value: Any) -> Any:
    if isinstance(value, dict):
        rows = value.get("rows", [])
        return {
            "columns": value.get("columns", []),
            "row_count": len(rows),
            "sample_rows": rows[:5],
        }
    return None
