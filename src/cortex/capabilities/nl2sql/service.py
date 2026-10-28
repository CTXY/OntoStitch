"""Durable interactive agent orchestration and online-context handoff."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from cortex.application.online_context import (
    InteractionAnswer,
    OnlineContextPending,
    _validate_answers,
)
from cortex.capabilities.nl2sql.agent import NL2SQLAgent
from cortex.capabilities.nl2sql.database import QueryDatabase
from cortex.capabilities.nl2sql.grounding import graph_schema_issues, ground_sql
from cortex.capabilities.nl2sql.store import QueryStore, now
from cortex.schema import GraphState

ACTIVE = {"queued", "running", "awaiting_user"}
PRIVATE = {
    "pending",
    "pending_answers",
    "context_result",
    "observations",
    "schema",
    "history",
    "step",
    "resume_step",
    "answer_receipts",
    "call_count",
    "repair_count",
    "agent_feedback",
    "declarations",
}


def conversation_title(question: str) -> str:
    """A short, stable label from the first question; no model call or inferred meaning."""
    title = re.split(r"[。！？!?\n]", question.strip(), maxsplit=1)[0]
    title = re.sub(r"^(?:请帮我|麻烦你|麻烦|帮我|请你|请)\s*", "", title).strip()
    title = re.sub(r"\s+", " ", title)
    limit = 42 if title.isascii() else 24
    return (
        title[:limit].rstrip("，,；;：: ") + ("…" if len(title) > limit else "")
    ) or "新会话"


def retrieval_display_selection(result):
    """Keep explicit model relevance separate from protected seeds and closure."""
    decisions = {d["document_id"]: d for d in result.get("relevance", [])}
    if not decisions:
        return None
    node_ids = {
        item["document"]["canonical_node_id"]
        for item in result.get("representations", [])
        if decisions.get(item["document"]["document_id"], {}).get("status")
        == "relevant"
    }
    return {"relevant_node_ids": sorted(node_ids)}


def task_context(task):
    """Re-render saved handoffs without replaying model calls or user decisions."""
    context = task.get("context")
    raw = task.get("context_result") or {}
    if not context or not context.get("guidance") or not raw.get("downstream_guidance"):
        return context
    from cortex.application.online_context import ChoicePoint, DownstreamGuidance
    from cortex.capabilities.online_context.output.projection import (
        build_downstream_guidance,
        guidance_selection_refs,
    )
    from cortex.schema import GraphSpace

    old = DownstreamGuidance.model_validate(raw["downstream_guidance"])
    points = tuple(ChoicePoint.model_validate(p) for p in raw.get("choice_points", []))
    selected = {
        p["choice_point_id"]: tuple(p["selected_option_ids"])
        for p in raw.get("projection_trace", [])
    }
    confirmed, unselected = guidance_selection_refs(points, selected)
    guidance = build_downstream_guidance(
        question=task.get("question", raw.get("question", "")),
        initial_sql=raw.get("initial_sql"),
        graph=GraphSpace.model_validate(
            task["graphs"].get("processed", task["graphs"].get("retrieved"))
        ),
        relevant_semantic_node_ids=set(raw.get("relevant_semantic_node_ids", [])),
        relevant_grounding_node_ids=set(raw.get("relevant_grounding_node_ids", [])),
        confirmed_graph_refs=confirmed,
        unselected_graph_refs=unselected,
        interaction_guidance=old.interaction_guidance,
        resolved_guidance_content="\n".join(
            "\n".join((g.should_be, *g.must_not_be)) for g in old.interaction_guidance
        ),
        resolved_join_paths=old.resolved_join_paths,
        query_examples=old.query_examples,
    )
    return {**context, "guidance": guidance.model_dump(mode="json")}


def agent_graph(task, context):
    """The Agent sees applicable node content; full inline variants stay inspectable."""
    graph = task["graphs"].get("processed", task["graphs"].get("retrieved"))
    if not graph or not context or not context.get("guidance"):
        return None
    assets = {
        a["node_id"]
        for a in context["guidance"]["ontology_assets"]
        if a.get("status") != "source_only"
    }
    value_ids = set(
        (task.get("context_result") or {}).get("relevant_grounding_node_ids", [])
    )
    for asset in context["guidance"]["ontology_assets"]:
        if asset.get("status") != "source_only":
            value_ids.update(asset.get("reference_bindings", {}))
    nodes = [
        n
        for n in graph["nodes"]
        if not n.get("alternatives")
        and (
            n["node_type"] in {"column", "table"}
            or n["id"] in assets
            or n["id"] in value_ids
        )
    ]
    ids = {n["id"] for n in nodes}
    return {
        "nodes": nodes,
        "edges": [
            e
            for e in graph["edges"]
            if e["from_node_id"] in ids and e["to_node_id"] in ids
        ],
    }


def public_task(task):
    public = {k: v for k, v in task.items() if k not in PRIVATE}
    public["context"] = task_context(task)
    public["retrieval_selection"] = task.get(
        "retrieval_selection"
    ) or retrieval_display_selection(task.get("context_result") or {})
    # Older saved runs used whole-clause links. Rebuild display metadata from the
    # exact stored SQL; query text, execution results and task state stay untouched.
    revisions = []
    for revision in task.get("sql_revisions", []):
        grounding = revision.get("grounding")
        if grounding and grounding.get("version") != 3:
            graph = task.get("graphs", {}).get(
                revision.get("graph_snapshot"), {"nodes": [], "edges": []}
            )
            revision = {
                **revision,
                "grounding": ground_sql(
                    revision["sql"], graph, grounding.get("declarations", [])
                ),
            }
        revisions.append(revision)
    public["sql_revisions"] = revisions
    return public


def default_runtime(directory, *, on_retrieved=None):
    from dotenv import load_dotenv

    from cortex.bootstrap.online_context import build_online_context_service
    from cortex.config import load_config
    from cortex.infrastructure.llm import create_chat_model

    project = Path(
        os.environ.get("CORTEX_PROJECT_ROOT", Path(__file__).resolve().parents[4])
    )
    load_dotenv(project / ".env", override=False)
    config = load_config(
        os.environ.get("CORTEX_NL2SQL_CONFIG", project / "configs/cortex.yaml")
    )
    online = config.services.online_context.model_copy(
        update={"checkpoint_path": directory / "online.sqlite"}
    )
    config = config.model_copy(
        update={
            "services": config.services.model_copy(update={"online_context": online})
        }
    )
    # An unfilled example value is not a configured embedding provider. Existing
    # retrieve_db supports lexical retrieval without embeddings; real provider
    # failures remain errors rather than silently changing the retrieval mode.
    lexical_only = (
        not config.embedding_model or config.embedding_model == "your-embedding-model"
    )
    agent = NL2SQLAgent(create_chat_model(config.llm))
    agent.retrieval_mode = "lexical" if lexical_only else "hybrid"
    return agent, build_online_context_service(
        config=config,
        on_retrieved=on_retrieved,
        **({"embeddings": None} if lexical_only else {}),
    )


class QueryService:
    def __init__(self, root: Path, database_path, graph_source, runtime_factory=None):
        self.root = root
        self.store = QueryStore(root / "sessions.sqlite")
        self.database_path, self.graph_source = database_path, graph_source
        self.runtime_factory = runtime_factory or default_runtime
        self.jobs: dict[str, asyncio.Task] = {}
        self.session_locks: dict[str, asyncio.Lock] = {}

    def recover(self):
        for task in self.store.list("task"):
            if task["status"] in {"queued", "running"}:
                self.store.update(
                    task["id"],
                    status="interrupted",
                    event="task.interrupted",
                    error="Service restarted. Resume this task to continue from its saved step.",
                )

    async def close(self):
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    async def create_session(self, database_id, source, request_id):
        async with self.session_locks.setdefault(
            "create:" + request_id, asyncio.Lock()
        ):
            return await self._create_session(database_id, source, request_id)

    async def _create_session(self, database_id, source, request_id):
        for old in self.store.list("session"):
            if old["request_id"] == request_id:
                if (
                    old["database_id"] != database_id
                    or old["ontology_source"] != source
                ):
                    raise ValueError(
                        "Request ID already used with different session inputs"
                    )
                return self.public_session(old)
        original_path = self.database_path(database_id)
        envelope = self.graph_source(database_id, source)
        state = GraphState.model_validate(envelope["state"])
        key = uuid4().hex
        directory = self.root / key
        directory.mkdir(parents=True)

        # SQLite backup gives a coherent read-only session snapshot, including WAL state.
        def snapshot():
            with (
                closing(
                    sqlite3.connect(
                        original_path.resolve().as_uri() + "?mode=ro", uri=True
                    )
                ) as src,
                closing(sqlite3.connect(directory / "database.sqlite")) as dest,
            ):
                src.backup(dest)

        await asyncio.to_thread(snapshot)
        schema = await asyncio.to_thread(
            QueryDatabase(directory / "database.sqlite").schema
        )
        graph = state.model_dump(mode="json")
        issues = graph_schema_issues(graph["graph"], schema)
        if issues:
            import shutil

            shutil.rmtree(directory)
            raise ValueError(
                "Selected ontology does not match this database: "
                + "; ".join(issues[:5])
            )
        (directory / "graph.json").write_text(json.dumps(graph, ensure_ascii=False))
        session = {
            "id": key,
            "database_id": database_id,
            "dialect": "sqlite",
            "ontology_source": source,
            "graph_fingerprint": hashlib.sha256(
                json.dumps(graph, sort_keys=True).encode()
            ).hexdigest(),
            "created_at": now(),
            "request_id": request_id,
            "title": "New database conversation",
        }
        self.store.put(key, "session", session)
        return self.public_session(session)

    def public_session(self, session):
        tasks = [
            public_task(t)
            for t in reversed(self.store.list("task"))
            if t["session_id"] == session["id"]
        ]
        first_question = tasks[0]["question"] if tasks else ""
        return {
            **session,
            "title": conversation_title(first_question)
            if first_question
            else session["title"],
            "first_question": first_question,
            "tasks": tasks,
        }

    async def archive_session(self, session_id):
        async with self.session_locks.setdefault(session_id, asyncio.Lock()):
            session = self.store.get(session_id, "session")
            if any(
                t["session_id"] == session_id and t["status"] in ACTIVE
                for t in self.store.list("task")
            ):
                raise ValueError("Stop or complete the active task before archiving")
            session["archived_at"] = session.get("archived_at") or now()
            self.store.put(session_id, "session", session)
            return self.public_session(session)

    async def create_task(self, session_id, question, request_id, revision_of=None):
        async with self.session_locks.setdefault(session_id, asyncio.Lock()):
            session = self.store.get(session_id, "session")
            if session.get("archived_at"):
                raise ValueError("This conversation has been archived")
            existing = [
                t for t in self.store.list("task") if t["session_id"] == session_id
            ]
            for task in existing:
                if task["request_id"] == request_id:
                    if (
                        task["question"] != question
                        or task.get("revision_of") != revision_of
                    ):
                        raise ValueError(
                            "Request ID already used with different task inputs"
                        )
                    return public_task(task)
            if any(
                t["status"] in ACTIVE or t["status"] == "interrupted" for t in existing
            ):
                raise ValueError(
                    "Answer, resume or cancel the current task before starting another"
                )
            if revision_of and not any(t["id"] == revision_of for t in existing):
                raise ValueError("Revision must refer to a task in this session")
            history = [
                {
                    "question": t["question"],
                    "answer": t.get("answer"),
                    "confirmations": t.get("confirmations", []),
                }
                for t in reversed(existing[:6])
            ]
            key = uuid4().hex
            task = {
                "id": key,
                "session_id": session_id,
                "database_id": session["database_id"],
                "request_id": request_id,
                "question": question,
                "revision_of": revision_of,
                "status": "queued",
                "phase": "prepare",
                "step": "prepare",
                "state_version": 0,
                "last_event_seq": 0,
                "created_at": now(),
                "updated_at": now(),
                "online_context_run_id": uuid4().hex,
                "interaction": None,
                "confirmations": [],
                "graphs": {},
                "context": None,
                "sql_revisions": [],
                "result": None,
                "answer": None,
                "error": None,
                "observations": [],
                "history": history,
                "call_count": 0,
                "repair_count": 0,
                "answer_receipts": {},
                "agent_feedback": "",
            }
            self.store.put(key, "task", task)
            self.store.update(key, event="task.created")
            if not existing:
                session["title"] = conversation_title(question)
            self.store.put(session_id, "session", session)
            self.schedule(key)
            return public_task(self.store.get(key))

    def schedule(self, key):
        if key in self.jobs and not self.jobs[key].done():
            return
        job = asyncio.create_task(self.run(key))
        self.jobs[key] = job

        def done(completed):
            if self.jobs.get(key) is completed:
                self.jobs.pop(key, None)

        job.add_done_callback(done)

    async def answer(self, key, request):
        task = self.store.get(key, "task")
        async with self.session_locks.setdefault(task["session_id"], asyncio.Lock()):
            task = self.store.get(key, "task")
            receipt = task.get("answer_receipts", {}).get(request["request_id"])
            identity = {k: request[k] for k in ("interaction_id", "answers")}
            if receipt:
                if receipt != identity:
                    raise ValueError(
                        "Answer request ID already used with different answers"
                    )
                return public_task(task)
            interaction = task.get("interaction")
            if (
                task["status"] != "awaiting_user"
                or not interaction
                or interaction["id"] != request["interaction_id"]
            ):
                raise ValueError("This question is no longer awaiting an answer")
            answers = tuple(
                InteractionAnswer.model_validate(a) for a in request["answers"]
            )
            if interaction["source"] == "online_context":
                pending = OnlineContextPending.model_validate(task["pending"])
                _validate_answers(pending.questions, answers)
            else:
                questions = {q["question_id"]: q for q in interaction["questions"]}
                if len(answers) != len(questions) or {
                    a.question_id for a in answers
                } != set(questions):
                    raise TypeError("Answer every question exactly once")
                for a in answers:
                    if not set(a.selected_option_ids) <= {
                        o["option_id"] for o in questions[a.question_id]["options"]
                    }:
                        raise TypeError("Unknown answer option")
            encoded = [a.model_dump(mode="json") for a in answers]
            confirmations = list(task["confirmations"])
            for a in encoded:
                q = next(
                    q
                    for q in interaction["questions"]
                    if q["question_id"] == a["question_id"]
                )
                confirmations.append(
                    {
                        "question_id": a["question_id"],
                        "prompt": q["prompt"],
                        "selected_statements": [
                            o["statement"]
                            for o in q["options"]
                            if o["option_id"] in a["selected_option_ids"]
                        ],
                        "explanation": a["explanation"],
                        "source": interaction["source"],
                    }
                )
            receipts = {**task["answer_receipts"], request["request_id"]: identity}
            step = (
                "context_resume"
                if interaction["source"] == "online_context"
                else task["resume_step"]
            )
            task = self.store.update(
                key,
                expected_version=request["expected_state_version"],
                event="clarification.answered",
                payload={"interaction_id": interaction["id"], "answers": encoded},
                status="queued",
                phase=step,
                step=step,
                pending_answers=encoded,
                interaction=None,
                confirmations=confirmations,
                answer_receipts=receipts,
            )
            self.schedule(key)
            return public_task(task)

    async def retry(self, key):
        task = self.store.get(key, "task")
        async with self.session_locks.setdefault(task["session_id"], asyncio.Lock()):
            task = self.store.get(key, "task")
            if task["status"] not in {"failed", "interrupted"}:
                raise ValueError("Only a failed or interrupted task can be retried")
            self.store.update(key, status="queued", error=None, event="task.resumed")
            self.schedule(key)
            return public_task(self.store.get(key))

    async def cancel(self, key):
        task = self.store.get(key, "task")
        async with self.session_locks.setdefault(task["session_id"], asyncio.Lock()):
            task = self.store.get(key, "task")
            if task["status"] in {"completed", "cancelled"}:
                return public_task(task)
            self.store.update(
                key, status="cancelled", interaction=None, event="task.cancelled"
            )
            job = self.jobs.get(key)
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            return public_task(self.store.get(key))

    def wait_for_agent_answer(self, task, action, step):
        options = action.get("options", [])
        if len(options) < 2 or len({o["option_id"] for o in options}) != len(options):
            raise ValueError(
                "Clarification requires at least two uniquely identified options"
            )
        q = {
            "question_id": uuid4().hex,
            "prompt": str(action["prompt"]),
            "reason": str(action.get("reason", "")),
            "selection_mode": "multiple",
            "requires_explanation_when_no_option": True,
            "options": [
                {
                    "option_id": str(o["option_id"]),
                    "statement": str(o["statement"]),
                    "graph_refs": [],
                }
                for o in options
            ],
        }
        interaction = {"id": uuid4().hex, "source": "agent", "questions": [q]}
        self.store.update(
            task["id"],
            status="awaiting_user",
            phase="clarification",
            interaction=interaction,
            resume_step=step,
            event="clarification.requested",
            payload=interaction,
        )

    def accept_context(self, task, result):
        key = task["id"]
        raw = result.model_dump(mode="json")
        graphs = dict(task["graphs"])
        if result.retrieved_graph_state is not None:
            graphs["retrieved"] = result.retrieved_graph_state.graph.model_dump(
                mode="json"
            )
        if result.processed_graph_state is not None:
            graphs["processed"] = result.processed_graph_state.graph.model_dump(
                mode="json"
            )
        context = {
            "status": result.status,
            "guidance": result.downstream_guidance.model_dump(mode="json")
            if result.downstream_guidance
            else None,
            "projection_trace": [
                p.model_dump(mode="json") for p in result.projection_trace
            ],
            "unresolved_choice_point_ids": list(result.unresolved_choice_point_ids),
            "diagnostics": result.diagnostics.model_dump(mode="json"),
        }
        task = self.store.update(
            key,
            graphs=graphs,
            context=context,
            context_result=raw,
            retrieval_selection=retrieval_display_selection(raw),
            event="context.updated",
            payload={
                "graph_snapshot": "retrieved",
                "node_count": len(graphs.get("retrieved", {}).get("nodes", [])),
            },
        )
        if result.pending is not None:
            interaction = {
                "id": uuid4().hex,
                "source": "online_context",
                "questions": [
                    q.model_dump(mode="json") for q in result.interaction_questions
                ],
            }
            self.store.update(
                key,
                pending=result.pending.model_dump(mode="json"),
                interaction=interaction,
                status="awaiting_user",
                phase="clarification",
                event="clarification.requested",
                payload=interaction,
            )
            return False
        if result.status == "unavailable":
            raise RuntimeError("Online context is unavailable; retry to continue")
        if result.status == "completed_with_unresolved":
            self.store.update(
                key,
                status="needs_review",
                phase="review",
                error="Some ontology decisions remain unresolved. Start a revision with a more specific question.",
                event="context.needs_review",
            )
            return False
        self.store.update(
            key,
            step="generate",
            phase="generate_sql",
            pending=None,
            event="context.ready",
            payload={
                "status": result.status,
                "graph_snapshot": "processed" if "processed" in graphs else "retrieved",
            },
        )
        return True

    async def run(self, key):
        try:
            task = self.store.get(key)
            if task["status"] not in {"queued", "running"}:
                return
            session = self.store.get(task["session_id"], "session")
            directory = self.root / session["id"]
            database = QueryDatabase(directory / "database.sqlite")

            def on_retrieved(result):
                current = self.store.get(key)
                if (
                    current["status"] != "running"
                    or result.retrieved_graph_state is None
                ):
                    return
                graph = result.retrieved_graph_state.graph.model_dump(mode="json")
                self.store.update(
                    key,
                    graphs={**current["graphs"], "retrieved": graph},
                    retrieval_selection=retrieval_display_selection(
                        result.model_dump(mode="json")
                    ),
                    event="context.retrieved",
                    payload={
                        "node_count": len(graph["nodes"]),
                        "graph_snapshot": "retrieved",
                    },
                )

            agent, online = await asyncio.to_thread(
                self.runtime_factory, directory, on_retrieved=on_retrieved
            )
            state = GraphState.model_validate_json(
                (directory / "graph.json").read_text()
            )
            self.store.update(
                key,
                status="running",
                error=None,
                retrieval_mode=getattr(agent, "retrieval_mode", "configured"),
                event="task.started",
            )
            while True:
                task = self.store.get(key)
                if task["status"] != "running":
                    return
                step = task["step"]
                if step == "prepare":
                    schema = await asyncio.to_thread(database.schema)
                    self.store.update(
                        key,
                        schema=schema,
                        step="context_start",
                        phase="online_context",
                        initial_sql=None,
                        event="tool.completed",
                        payload={"tool": "inspect_schema", "tables": len(schema)},
                    )
                elif step in {"draft", "generate"}:
                    if task["call_count"] >= 24:
                        raise RuntimeError(
                            "Agent tool-call budget exhausted; revise the question"
                        )
                    self.store.update(key, call_count=task["call_count"] + 1)
                    handoff = task_context(task)
                    context = {
                        "context": handoff,
                        "relevant_graph": agent_graph(task, handoff),
                        "user_confirmations": task["confirmations"],
                    }
                    action = await agent.act(
                        question=task["question"],
                        schema=task["schema"],
                        history=task["history"],
                        context=context,
                        stage="draft" if step == "draft" else "final",
                        feedback=task.get("agent_feedback", ""),
                        observations=task["observations"],
                    )
                    kind = action.get("action")
                    if kind in {"inspect_schema", "inspect_values"}:
                        self.store.update(
                            key, event="tool.started", payload={"tool": kind}
                        )
                        try:
                            data = (
                                await asyncio.to_thread(
                                    database.schema, action.get("tables")
                                )
                                if kind == "inspect_schema"
                                else await asyncio.to_thread(
                                    database.values, action["table"], action["column"]
                                )
                            )
                        except (ValueError, sqlite3.Error, KeyError) as exc:
                            data = {"error": str(exc)}
                        self.store.update(
                            key,
                            observations=[
                                *task["observations"],
                                {"tool": kind, "input": action, "result": data},
                            ],
                            event="tool.completed",
                            payload={"tool": kind, "input": action},
                        )
                    elif kind == "clarify":
                        # User-facing ontology questions belong to online_context.
                        # Legacy drafts may still emit this action; route them to
                        # retrieval instead of presenting an ungrounded question.
                        if step == "draft":
                            self.store.update(
                                key,
                                initial_sql=None,
                                step="context_start",
                                phase="online_context",
                                event="context.requested",
                            )
                        else:
                            self.store.update(
                                key,
                                status="needs_review",
                                phase="review",
                                error="当前图谱上下文不足以生成确定的 SQL。请补充业务问题或检查图谱依据。",
                                event="context.needs_review",
                            )
                            return
                    elif kind == "sql":
                        sql = str(action["sql"]).strip()
                        if not sql or len(sql) > 50000:
                            raise ValueError("Agent returned empty or oversized SQL")
                        if step == "draft":
                            self.store.update(
                                key,
                                initial_sql=sql,
                                step="context_start",
                                phase="online_context",
                                event="sql.drafted",
                            )
                        else:
                            revisions = [
                                *task["sql_revisions"],
                                {
                                    "id": uuid4().hex,
                                    "sql": sql,
                                    "explanation": str(action.get("explanation", "")),
                                    "status": "generated",
                                    "created_at": now(),
                                },
                            ]
                            self.store.update(
                                key,
                                sql_revisions=revisions,
                                declarations=action.get("grounding", []),
                                step="execute",
                                phase="validate_sql",
                                event="sql.generated",
                                payload={"sql_revision_id": revisions[-1]["id"]},
                            )
                    else:
                        self.store.update(
                            key,
                            agent_feedback="Invalid action. Choose inspect_schema, inspect_values, clarify or sql.",
                        )
                elif step in {"context_start", "context_resume"}:
                    self.store.update(
                        key,
                        phase="online_context",
                        event="tool.started",
                        payload={
                            "tool": "online_context.resume"
                            if step == "context_resume"
                            else "online_context.run"
                        },
                    )
                    if step == "context_start":
                        context_question = (
                            task["question"]
                            + (
                                "\nPrevious conversation (for resolving references only): "
                                + json.dumps(task["history"], ensure_ascii=False)
                                if task["history"]
                                else ""
                            )
                            + (
                                "\nUser-confirmed task details: "
                                + json.dumps(task["confirmations"], ensure_ascii=False)
                                if task["confirmations"]
                                else ""
                            )
                        )
                        result = await online.run(
                            question=context_question,
                            graph_state=state,
                            dialect="sqlite",
                            initial_sql=task["initial_sql"],
                            run_id=task["online_context_run_id"],
                        )
                    else:
                        result = await online.resume(
                            graph_state=state,
                            pending=OnlineContextPending.model_validate(
                                task["pending"]
                            ),
                            answers=tuple(
                                InteractionAnswer.model_validate(a)
                                for a in task["pending_answers"]
                            ),
                        )
                    if not self.accept_context(task, result):
                        return
                elif step == "execute":
                    revisions = task["sql_revisions"]
                    revision = dict(revisions[-1])
                    try:
                        validated = await asyncio.to_thread(
                            database.validate, revision["sql"]
                        )
                        revision.update(validated, status="validated")
                        graph = task["graphs"].get(
                            "processed",
                            task["graphs"].get("retrieved", {"nodes": [], "edges": []}),
                        )
                        revision["grounding"] = ground_sql(
                            revision["sql"], graph, task.get("declarations")
                        )
                        revision["graph_snapshot"] = (
                            "processed"
                            if "processed" in task["graphs"]
                            else "retrieved"
                        )
                        revisions = [*revisions[:-1], revision]
                        self.store.update(
                            key,
                            sql_revisions=revisions,
                            phase="execute_sql",
                            event="sql.validated",
                            payload={"sql_revision_id": revision["id"]},
                        )
                        if not revision.get("meaning_review", {}).get("consistent"):
                            self.store.update(
                                key, phase="review_sql", event="sql.review_started"
                            )
                            review = await agent.review(
                                question=task["question"],
                                sql=revision["sql"],
                                confirmations=task["confirmations"],
                                context=task_context(task),
                            )
                            revision["meaning_review"] = review
                            self.store.update(
                                key,
                                sql_revisions=[*revisions[:-1], revision],
                                event="sql.review_completed",
                                payload=review,
                            )
                            if not review["consistent"]:
                                raise ValueError(
                                    "SQL contradicts confirmed meaning: "
                                    + str(review.get("issues", ""))
                                )
                        self.store.update(key, phase="execute_sql")
                        self.store.update(
                            key,
                            event="sql.execution_started",
                            payload={"sql_revision_id": revision["id"]},
                        )
                        result = await asyncio.to_thread(
                            database.execute, revision["sql"]
                        )
                    except (ValueError, sqlite3.Error) as exc:
                        # Retain each failed revision and feed the real failure back to the agent.
                        revision.update(status="failed", error=str(exc)[:1200])
                        repairs = task["repair_count"] + 1
                        self.store.update(
                            key,
                            sql_revisions=[*revisions[:-1], revision],
                            repair_count=repairs,
                            agent_feedback=f"SQL failed: {revision['sql']}\nError: {exc}",
                            step="generate",
                            phase="repair_sql",
                            event="sql.execution_failed",
                            payload={
                                "sql_revision_id": revision["id"],
                                "error": str(exc)[:1200],
                            },
                        )
                        if repairs >= 3:
                            raise RuntimeError(
                                "SQL still failed after three attempts; see revision errors"
                            ) from exc
                        continue
                    revision.update(status="executed")
                    self.store.update(
                        key,
                        sql_revisions=[*revisions[:-1], revision],
                        result=result,
                        step="summarize",
                        phase="summarize",
                        event="sql.execution_completed",
                        payload={
                            "sql_revision_id": revision["id"],
                            "row_count": result["row_count"],
                            "truncated": result["truncated"],
                        },
                    )
                elif step == "summarize":
                    answer = await agent.summarize(
                        question=task["question"],
                        sql=task["sql_revisions"][-1]["sql"],
                        result=task["result"],
                        confirmations=task["confirmations"],
                    )
                    self.store.update(
                        key,
                        answer=answer,
                        status="completed",
                        phase="completed",
                        step="done",
                        event="answer.completed",
                    )
                    return
                else:
                    raise ValueError("Unknown saved agent step")
        except asyncio.CancelledError:
            task = self.store.get(key)
            if task["status"] != "cancelled":
                self.store.update(
                    key,
                    status="interrupted",
                    error="Execution interrupted; resume to continue",
                    event="task.interrupted",
                )
            raise
        except Exception as exc:  # noqa: BLE001 - task failure must be durable and visible
            task = self.store.get(key)
            if task["status"] != "cancelled":
                self.store.update(
                    key, status="failed", error=str(exc)[:1500], event="task.failed"
                )
