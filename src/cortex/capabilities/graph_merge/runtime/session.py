"""Accepted merge state, resumable progress and atomic workspace persistence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from cortex.capabilities.graph_merge.graph.batch import alias_view
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.inputs import IntegrationContext, input_fingerprint
from cortex.capabilities.graph_merge.runtime.state import (
    ContractVersions,
    TaskState,
    validate_checkpoint_contract,
)
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.schema import GraphState, SourceReference


@dataclass
class MergeSession:
    inputs: IntegrationContext | None = None
    current: GraphState | None = None
    lineage: LineageIndex | None = None
    history: list[dict] = field(default_factory=list)
    reviewed_sources: dict[str, set[SourceReference]] = field(default_factory=dict)
    revision: int = 0
    storage_revision: int | None = None
    identity_decision: dict | None = None
    identity_read_ids: list[str] | None = None
    content_plan: list | None = None
    contract_versions: ContractVersions | None = None
    task_state: TaskState | None = None
    open_tasks: list[dict] = field(default_factory=list)
    signal_receipts: list[dict] = field(default_factory=list)
    relation_coverage: set[str] = field(default_factory=set)
    relation_followups: list[dict] = field(default_factory=list)
    relation_merger_checkpoint: dict = field(default_factory=dict)
    relation_merger_history: list[dict] = field(default_factory=list)
    reconciliation_complete: bool = False
    pending_relation_work: dict | None = None
    pending_review: dict | None = None

    @classmethod
    def start(cls, inputs, *, versions=None, current=None, lineage=None):
        if current is None:
            current, edges = alias_view(inputs.union_state(), {})
        else:
            edges = {edge.id: edge.id for edge in current.graph.edges}
        return cls(
            inputs=inputs,
            current=current,
            lineage=lineage
            or LineageIndex(
                inputs, initial_node_map=inputs.physical_map, initial_edge_map=edges
            ),
            contract_versions=versions,
        )

    def restore(
        self, snapshot, *, versions, storage_revision=None, normalize_current=True
    ):
        validate_checkpoint_contract(snapshot, versions)
        inputs = self.inputs
        if snapshot.get("input_fingerprint") != input_fingerprint(inputs):
            raise ValueError("Workspace source/schema/evidence fingerprint mismatch")
        self.contract_versions = versions
        self.storage_revision = storage_revision
        self.current = GraphState.model_validate(snapshot["current"])
        self.lineage = LineageIndex.restore(inputs, snapshot["lineage"])
        self.history = list(snapshot["history"])
        self.revision = snapshot.get("accepted_revision", len(self.history))
        self.reviewed_sources = {
            key: {SourceReference.model_validate(row) for row in rows}
            for key, rows in snapshot.get("reviewed_sources", {}).items()
        }
        self.identity_decision = snapshot.get("identity")
        reads = snapshot.get("identity_read_ids")
        if reads is not None:
            if not set(reads) <= set(inputs.nodes):
                raise ValueError(
                    "Saved identity reads reference nodes outside the original input"
                )
            inputs.read_ids.update(reads)
            self.identity_read_ids = sorted(reads)
        self.content_plan = snapshot.get("content_plan")
        self.relation_coverage = set(snapshot.get("relation_coverage", ()))
        from cortex.capabilities.graph_merge.reconciliation.receipts import (
            validate_receipt_ledger,
        )

        if "signal_receipts" not in snapshot and any(
            "reconciliation_receipt_policy_hash" in row for row in self.history
        ):
            raise ValueError(
                "Missing accepted signal receipt ledger; checkpoint is incomplete"
            )
        self.signal_receipts = (
            validate_receipt_ledger(
                snapshot["signal_receipts"], history=self.history, current=self.current
            )
            if "signal_receipts" in snapshot
            else []
        )
        self.reconciliation_complete = snapshot.get("reconciliation_complete", False)
        self.pending_relation_work = snapshot.get("pending_relation_work")
        self.pending_review = snapshot.get("pending_review")
        from cortex.capabilities.graph_merge.reconciliation.followups import (
            validate_followup_queue,
        )

        self.relation_followups = validate_followup_queue(
            snapshot.get("relation_followups", [])
        )
        self.relation_merger_checkpoint = deepcopy(
            snapshot.get("relation_merger_checkpoint", {})
        )
        self.relation_merger_history = deepcopy(
            snapshot.get("relation_merger_history", [])
        )
        self.open_tasks = list(snapshot.get("open_tasks", []))
        if snapshot.get("task_state"):
            task = TaskState.from_dict(snapshot["task_state"])
            self.task_state = task if task.stage != "committed" else None
        normalized, edge_aliases = (
            alias_view(self.current, {}) if normalize_current else (self.current, {})
        )
        if normalized != self.current:
            self.lineage.observe(
                self.current,
                normalized,
                edge_aliases=edge_aliases,
                explanation="Canonicalize duplicate assertions using declared physical JOIN identity",
            )
            self.current = normalized

    def mark_reviewed(self, ids):
        nodes = {node.id: node for node in self.current.graph.nodes}
        for key in ids:
            if key not in nodes:
                continue
            node = nodes[key]
            refs = set(node.sources) | {
                source for alt in node.alternatives for source in alt.sources
            }
            refs |= {
                source
                for edge in self.current.graph.edges
                if edge.from_node_id == key
                and edge.edge_type in {"grounded_on", "depends_on", "applies_to"}
                for source in edge.sources
            }
            self.reviewed_sources[key] = refs

    def acceptance_snapshot(self):
        """Save the exact accepted boundary before a tentative task commit."""
        return {
            "current": self.current,
            "lineage": self.lineage,
            "revision": self.revision,
            "storage_revision": self.storage_revision,
            "open_tasks": self.open_tasks,
            "pending_relation_work": self.pending_relation_work,
            "pending_review": self.pending_review,
            "relation_followups": self.relation_followups,
            "signal_receipts": deepcopy(self.signal_receipts),
            "history": list(self.history),
            "reviewed_sources": deepcopy(self.reviewed_sources),
            "relation_merger_checkpoint": deepcopy(self.relation_merger_checkpoint),
        }

    def restore_acceptance(self, snapshot):
        for name, value in snapshot.items():
            setattr(self, name, value)

    def snapshot(self, *, final=False):
        return {
            "current": self.current.model_dump(mode="json"),
            "lineage": self.lineage.snapshot(),
            "history": self.history,
            "open_tasks": self.open_tasks,
            "final": final,
            "accepted_revision": self.revision,
            "input_fingerprint": input_fingerprint(self.inputs),
            "identity": self.identity_decision,
            "identity_read_ids": self.identity_read_ids,
            "content_plan": self.content_plan,
            "relation_coverage": sorted(self.relation_coverage),
            "signal_receipts": deepcopy(self.signal_receipts),
            "reconciliation_complete": self.reconciliation_complete,
            "pending_relation_work": self.pending_relation_work,
            "pending_review": self.pending_review,
            "relation_followups": deepcopy(self.relation_followups),
            "relation_merger_checkpoint": deepcopy(self.relation_merger_checkpoint),
            "relation_merger_history": deepcopy(self.relation_merger_history),
            "contract_versions": self.contract_versions.to_dict()
            if self.contract_versions
            else None,
            "task_state": self.task_state.to_dict() if self.task_state else None,
            "reviewed_sources": {
                key: [
                    ref.model_dump(mode="json")
                    for ref in sorted(refs, key=lambda ref: ref.model_dump_json())
                ]
                for key, refs in self.reviewed_sources.items()
            },
        }

    def save(self, path, run_id, *, final=False):
        if path:
            self.storage_revision = WorkspaceStore(path).save(
                run_id,
                self.snapshot(final=final),
                expected_revision=self.storage_revision,
            )
