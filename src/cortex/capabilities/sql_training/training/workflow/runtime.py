"""Synchronous execution/resume boundary with a persistent case checkpointer."""

import fcntl
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langsmith import tracing_context

from cortex.capabilities.sql_training.graph.snapshots import fingerprint
from cortex.capabilities.sql_training.training.workflow.graph import build_case_graph
from cortex.capabilities.sql_training.training.workflow.nodes import CaseRuntime
from cortex.capabilities.sql_training.training.workflow.state import (
    WORKFLOW_VERSION,
    CaseState,
)


class CaseResumeError(ValueError):
    """Refuse incompatible or missing checkpoints without changing a run."""


@contextmanager
def case_writer(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CaseResumeError("case already has an active writer") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class CaseWorkflow:
    def __init__(self, *, skip_direct=False, initialize=None, **kwargs):
        self.runtime = CaseRuntime(**kwargs)
        self.skip_direct = skip_direct
        self.initialize = initialize
        self.has_checkpoint = False
        self.root = self.runtime.root
        self.checkpoint_path = self.root / "checkpoints.sqlite"
        self.thread_id = "case-" + fingerprint(self.runtime.record["source_id"])
        self.config = {
            "configurable": {"thread_id": self.thread_id},
            "recursion_limit": 20
            + self.runtime.max_rounds * (8 * self.runtime.max_iterations + 6),
        }
        self.state = None

    def _compatible(self, values):
        # Check the raw header before parsing the current schema: retired layouts
        # may contain fields (such as bundle_ref) that this schema rejects.
        if (
            values.get("context_id") != self.runtime.services.context_id
            or values.get("workflow_version") != WORKFLOW_VERSION
            or values.get("case_id") != str(self.runtime.record["source_id"])
        ):
            raise CaseResumeError(
                "cannot resume a case with changed source/data/config/prompt/workflow"
            )
        return CaseState.model_validate(values)

    def run(self, *, resume=False, interrupt_after=None):
        if resume and not self.checkpoint_path.is_file():
            raise CaseResumeError(
                "case resume requires a persistent LangGraph checkpoint"
            )
        with (
            case_writer(self.root),
            SqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver,
        ):
            graph = build_case_graph(self.runtime, saver, skip_direct=self.skip_direct)
            snapshot = graph.get_state(self.config)
            if resume:
                if not snapshot.values:
                    raise CaseResumeError("case resume requires a complete checkpoint")
                self.has_checkpoint = True
                self.state = self._compatible(snapshot.values)
                self.runtime.graph.restore(
                    self.state.working_revision, self.state.accepted_revision
                )
                if not snapshot.next:
                    return self.state
                # A manual resume opens a fresh bounded infrastructure retry window;
                # semantic correct/incorrect gates remain immutable and cached.
                self.runtime.services.reset_unavailable_validation()
                inputs = None
            else:
                if snapshot.values:
                    raise CaseResumeError("case already exists; use resume_case")
                if self.initialize is not None:
                    self.initialize()
                self.state = CaseState(
                    case_id=str(self.runtime.record["source_id"]),
                    context_id=self.runtime.services.context_id,
                    working_revision=self.runtime.graph.working_revision,
                    accepted_revision=self.runtime.graph.accepted_revision,
                    direct_completed=self.skip_direct,
                )
                inputs = self.state.model_dump(mode="json")
            try:
                with tracing_context(enabled=False):
                    graph.invoke(
                        inputs,
                        self.config,
                        durability="sync",
                        interrupt_after=interrupt_after,
                    )
            finally:
                current = graph.get_state(self.config)
                self.has_checkpoint = bool(current.values)
                if current.values:
                    self.state = self._compatible(current.values)
                    self.runtime.graph.restore(
                        self.state.working_revision, self.state.accepted_revision
                    )
            return self.state

    def inspect(self):
        if not self.checkpoint_path.is_file():
            raise CaseResumeError("no case checkpoint")
        with SqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver:
            graph = build_case_graph(self.runtime, saver, skip_direct=self.skip_direct)
            snapshot = graph.get_state(self.config)
            self.state = self._compatible(snapshot.values)
            return snapshot
