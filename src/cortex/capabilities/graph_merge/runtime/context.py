"""Independent execution frames over shared, read-only source graph indexes."""

from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass, field
from typing import Any

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.models import LocalTask
from cortex.capabilities.graph_merge.runtime.state import TaskState
from cortex.schema import GraphState


def fork_task_context(inputs, *, before, task, read_ids=None):
    """Bind IntegrationContext methods to a fresh frame, never to the inputs.

    Source node/edge/catalog indexes are shared for read access. Read journals,
    grants, candidates, feedback, model runtimes and transaction callbacks belong
    only to this task. A nested task must fork the inputs, not its parent frame.
    """
    context = copy(inputs)
    context.read_ids = set(inputs.read_ids if read_ids is None else read_ids)
    context.tool_log = []
    context.phase = task.phase
    context.task_before = before
    context.active_task = task
    context.candidate = before
    context.identity = None
    context.preview = None
    context.task_visible_ids = set()
    context.task_identity_groups = []
    context.active_draft = None
    context.task_draft_id = None
    context.task_draft_errors = ()
    context.task_draft_errors_draft_id = None
    context.task_candidate_draft_id = None
    context.task_failure_stage = "patch_compile"
    context.task_semantic_issues = ()
    context.task_semantic_issue_basis = ()
    context.task_required_context = ()
    context.task_relevant_facts = ()
    context.task_partial_semantic_findings = ()
    context.task_assessment_premises = []
    context.task_assessment_cache_reused = False
    context.canonical_assessment_facts = None
    context.node_local_compile = False
    context.node_local_raw = None
    context.node_writer_followups = ()
    context.node_assessment_followups = ()
    context.node_structural_diagnostics = ()
    context.active_batch = None
    context.last_compiled_patch = None
    context.reconciliation_runtime = None
    context.output_validator = None
    context.compile_task_patch = None
    context.record_task_draft = None
    context.record_task_error = None
    context.load_task_recovery = None
    context.record_task_recovery = None
    context.preview_task_draft = None
    context.accept_compiled_draft = None
    context.record_complete_draft_action = None
    context.has_pending_draft_action = lambda _draft: False
    context.get_task_compilation = None
    return context


@dataclass
class TaskExecution:
    task: LocalTask
    before: GraphState
    context: IntegrationContext
    state: TaskState | dict
    compiled: Any = None
    payload: dict = field(default_factory=dict)

    def sync_draft_context(self):
        """Expose the exact immutable TaskState head to the task's model frame."""
        state = self.state
        context = self.context
        context.active_draft = deepcopy(state.draft)
        context.task_draft_id = state.draft_id
        context.task_draft_errors = state.issues
        context.task_draft_errors_draft_id = state.issues_draft_id
        context.task_candidate_draft_id = state.candidate_draft_id
