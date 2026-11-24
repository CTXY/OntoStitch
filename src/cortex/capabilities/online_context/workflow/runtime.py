"""Persistent async execution, compatible resume, and one writer per session."""

import hashlib
import json
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from cortex.application.online_context import (
    OnlineContextError,
    OnlineCheckpointCompatibilityError,
    OnlineContextResult,
    calculate_graph_state_fingerprint,
)
from cortex.capabilities.online_context.workflow.graph import build_online_graph
from cortex.infrastructure.observability import native_tracing
from cortex.infrastructure.workflow.locking import workflow_writer

WORKFLOW_VERSION = "online-context-v4-clarification-filter"


class OnlineWorkflow:
    def __init__(self, engine, path, settings):
        self.engine, self.path = engine, path.expanduser().resolve()
        self.identity = hashlib.sha256(
            json.dumps(
                {"version": WORKFLOW_VERSION, **settings}, sort_keys=True, default=str
            ).encode()
        ).hexdigest()

    async def start(self, request, *, run_id=None):
        return await self._run(run_id or uuid4().hex, request=request)

    async def resume(self, graph_state, pending, answers):
        if not pending.run_id or pending.workflow_fingerprint != self.identity:
            raise OnlineCheckpointCompatibilityError(
                "pending workflow, prompts or configuration changed; start a new online-context run"
            )
        if (
            calculate_graph_state_fingerprint(graph_state)
            != pending.graph_state_fingerprint
        ):
            raise OnlineContextError("pending graph differs from the supplied graph")
        return await self._run(pending.run_id, pending=pending, answers=answers)

    async def _run(self, run_id, *, request=None, pending=None, answers=()):
        if not run_id.isalnum():
            raise OnlineContextError("invalid online context run ID")
        if request is None and not self.path.is_file():
            raise OnlineContextError("online context checkpoint is missing")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        config = {
            "configurable": {"thread_id": run_id},
            "recursion_limit": 10000,
            "metadata": {"session_id": run_id, "workflow_version": WORKFLOW_VERSION},
        }
        with (
            workflow_writer(self.path.with_name(f".{self.path.name}.{run_id}.lock")),
            native_tracing(),
        ):
            async with AsyncSqliteSaver.from_conn_string(str(self.path)) as saver:
                graph = build_online_graph(
                    self.engine, saver, run_id=run_id, identity=self.identity
                )
                snapshot = await graph.aget_state(config)
                if request is not None:
                    if snapshot.values:
                        if (
                            snapshot.values.get("request") != request
                            or snapshot.values.get("identity") != self.identity
                        ):
                            raise OnlineCheckpointCompatibilityError(
                                "cannot reuse run ID with changed graph, question, prompts or configuration"
                            )
                        if (not snapshot.next and not snapshot.tasks) or any(
                            task.interrupts for task in snapshot.tasks
                        ):
                            if "result" not in snapshot.values:
                                raise OnlineCheckpointCompatibilityError(
                                    "checkpoint has no result or resumable task"
                                )
                            return OnlineContextResult.model_validate(
                                snapshot.values["result"]
                            )
                        # Empty `next` can mean the tasks have committed pending
                        # writes, while their successor routing is not committed.
                        # Native replay applies those writes without rerunning
                        # the completed tasks. Do not manually reschedule nodes.
                        inputs = None
                    else:
                        inputs = {"request": request, "identity": self.identity}
                else:
                    state = snapshot.values
                    if not state or state.get("identity") != self.identity:
                        raise OnlineCheckpointCompatibilityError(
                            "checkpoint is missing or incompatible"
                        )
                    saved_pending = state.get("result", {}).get("pending")
                    supplied = [a.model_dump(mode="json") for a in answers]
                    history = {a["question_id"]: a for a in state.get("answers", ())}
                    waiting = any(task.interrupts for task in snapshot.tasks)
                    replay = bool(supplied) and all(
                        history.get(a["question_id"]) == a for a in supplied
                    )
                    if replay:
                        if (not snapshot.next and not snapshot.tasks) or waiting:
                            return OnlineContextResult.model_validate(state["result"])
                        # The answer was committed but a downstream node failed.
                        # Resume that node without consuming the answer a second time.
                        inputs = None
                    else:
                        if not saved_pending or saved_pending != pending.model_dump(
                            mode="json"
                        ):
                            raise OnlineContextError(
                                "stale pending questions; load the latest session result"
                            )
                        if not waiting:
                            raise OnlineContextError(
                                "session is not waiting for answers"
                            )
                        inputs = Command(resume=supplied)
                try:
                    await graph.ainvoke(inputs, config, durability="sync")
                except Exception as exc:
                    exc.add_note(
                        f"online-context run_id={run_id}; retry with the same run ID or pending state"
                    )
                    raise
                saved = await graph.aget_state(config)
                if "result" not in saved.values:
                    raise OnlineCheckpointCompatibilityError(
                        "native checkpoint replay did not reach a public result"
                    )
                return OnlineContextResult.model_validate(saved.values["result"])
