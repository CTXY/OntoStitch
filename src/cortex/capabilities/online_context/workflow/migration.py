"""Explicit, non-destructive migration when retrieval policy changes.

Only initial SQL and recall are reused. Selection and every downstream online
decision depend on the recall-selection policy and must run under the new code.
The source databases are opened read-only; native LangGraph updates seed a new
thread in separate destination databases, preserving the original histories.
"""

import hashlib
import sqlite3
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from cortex.application.online_context import OnlineCheckpointCompatibilityError
from cortex.application.retrieval import RetrievedRepresentation
from cortex.application.retrieve_db import calculate_graph_fingerprint
from cortex.schema import GraphState
from cortex.capabilities.online_context.workflow.graph import build_online_graph
from cortex.capabilities.retrieve_db.workflow.graph import build_retrieval_graph
from cortex.capabilities.retrieve_db.workflow.runtime import WORKFLOW_VERSION


def checkpoint_values(path, run_id):
    path = Path(path)
    if not path.is_file():
        return {}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT type, checkpoint FROM checkpoints WHERE thread_id=? "
            "AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1", (run_id,),
        ).fetchone()
    return JsonPlusSerializer().loads_typed(row)["channel_values"] if row else {}


async def migrate_recalled_state(
    *, source_online_path, source_retrieval_root, target_engine,
    source_run_id, target_run_id, expected_request,
    source_stage_contract=None, target_stage_contract=None,
):
    online = target_engine._workflow
    retrieval = target_engine._retrieval._engine._workflow
    if Path(source_online_path).resolve() == online.path.resolve():
        raise ValueError("Migration requires a separate destination checkpoint")
    if Path(source_retrieval_root).resolve() == retrieval.checkpoint_root.resolve():
        raise ValueError("Migration requires a separate retrieval checkpoint root")
    if not target_run_id.isalnum() or target_run_id == source_run_id:
        raise ValueError("Migration requires a distinct alphanumeric target run ID")
    saved = checkpoint_values(source_online_path, source_run_id)
    if saved.get("request") != expected_request:
        raise OnlineCheckpointCompatibilityError(
            "migration rejected: source graph, question, dialect, initial SQL, columns or budget differs"
        )
    if checkpoint_values(online.path, target_run_id):
        raise ValueError("Destination online thread already exists; never overwrite it")
    source_retrieval_id = "online-retrieval-" + source_run_id
    target_retrieval_id = "online-retrieval-" + target_run_id
    source_file = Path(source_retrieval_root) / (hashlib.sha256(source_retrieval_id.encode()).hexdigest() + ".sqlite")
    prior = checkpoint_values(source_file, source_retrieval_id)
    reused = []
    if prior:
        if not source_stage_contract or source_stage_contract != target_stage_contract:
            raise OnlineCheckpointCompatibilityError(
                "migration rejected: initial SQL/recall code, model settings, schema, embeddings or limits changed"
            )
        source_request = prior["request"]
        required = {
            "question": expected_request["question"],
            "graph_state": expected_request["graph_state"],
            "graph_fingerprint": calculate_graph_fingerprint(
                GraphState.model_validate(expected_request["graph_state"]).graph
            ),
            "dialect": expected_request["dialect"],
            "initial_sql": expected_request["initial_sql"],
            "column_context": expected_request["column_context"],
            "run_id": source_retrieval_id,
            "workflow_version": WORKFLOW_VERSION,
        }
        if any(source_request.get(k) != v for k, v in required.items()):
            raise OnlineCheckpointCompatibilityError("migration rejected: nested retrieval request differs")
        target_request = {
            **required, "run_id": target_retrieval_id, "context_id": retrieval.context_id,
        }
        values = {"request": target_request}
        last_node = None
        if "initial_sql" in prior:
            values["initial_sql"] = prior["initial_sql"]
            reused.append("initial_sql")
            last_node = "signals"
        if "representations" in prior:
            if last_node is None:
                raise OnlineCheckpointCompatibilityError("recall checkpoint has no initial SQL stage")
            node_ids = {n["id"] for n in expected_request["graph_state"]["graph"]["nodes"]}
            for raw in prior["representations"]:
                item = RetrievedRepresentation.model_validate(raw)
                if item.document.canonical_node_id not in node_ids:
                    raise OnlineCheckpointCompatibilityError("recalled document references a different graph")
            values["representations"] = prior["representations"]
            reused.append("recall")
            last_node = "recall"
        if last_node is not None:
            root = retrieval.checkpoint_root
            root.mkdir(parents=True, exist_ok=True)
            destination = root / (hashlib.sha256(target_retrieval_id.encode()).hexdigest() + ".sqlite")
            if destination.exists():
                raise ValueError("Destination retrieval checkpoint already exists")
            async with AsyncSqliteSaver.from_conn_string(str(destination)) as saver:
                graph = build_retrieval_graph(retrieval.nodes, saver)
                await graph.aupdate_state(
                    {"configurable": {"thread_id": target_retrieval_id}}, values,
                    as_node=last_node,
                )
    online.path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(online.path)) as saver:
        graph = build_online_graph(target_engine, saver, run_id=target_run_id, identity=online.identity)
        await graph.aupdate_state(
            {"configurable": {"thread_id": target_run_id}},
            {"request": expected_request, "identity": online.identity}, as_node="__start__",
        )
    return {
        "source_run_id": source_run_id, "target_run_id": target_run_id,
        "source_identity": saved["identity"], "target_identity": online.identity,
        "reused_stages": reused,
        "invalidated_stages": ["select", "assemble", "discover", "filter_questions", "interaction", "finalize"],
        "reason": "Explicit provider/prompt/relevance-policy migration; downstream decisions must use new recall selection",
        "original_checkpoints_modified": False,
        "reused_stage_contract": source_stage_contract if reused else None,
    }
