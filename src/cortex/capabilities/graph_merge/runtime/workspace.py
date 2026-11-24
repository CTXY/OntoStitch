"""Atomic local checkpoints for an unfinished, serial merge workspace.

The store checks serialization/GraphState shape, not final ontology invariants.
It is deliberately separate from the published graph repository. Connections and
write transactions exist only during load/save, never during model execution.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from cortex.schema import GraphState


class WorkspaceRevisionMismatch(ValueError):
    def __init__(self, expected, actual):
        self.expected, self.actual = expected, actual
        super().__init__(
            f"Workspace revision mismatch: expected {expected!r}, actual {actual}"
        )


class WorkspaceCorruption(ValueError):
    pass


def _json_default(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Workspace value is not JSON serializable: {type(value).__name__}")


def _normalize(payload):
    if not isinstance(payload, dict):
        raise TypeError("Workspace payload must be an object")
    normalized = dict(payload)
    normalized["current"] = GraphState.model_validate(payload["current"]).model_dump(
        mode="json"
    )
    if not isinstance(payload.get("lineage"), dict):
        raise TypeError("Workspace requires a lineage snapshot object")
    for key in ("history", "open_tasks"):
        value = payload.get(key, [])
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"Workspace {key} must be a list")
        normalized[key] = list(value)
    if 'signal_receipts' in payload:
        from cortex.capabilities.graph_merge.reconciliation.ledger import validate_receipt_ledger
        normalized['signal_receipts'] = validate_receipt_ledger(
            payload['signal_receipts'], history=normalized['history'], current=normalized['current'])
    elif any('reconciliation_receipt_policy_hash' in h for h in normalized['history']):
        raise ValueError('Missing receipt ledger for a workspace with accepted reconciliation receipts')
    if 'signal_scope_migrations' in payload:
        from cortex.capabilities.graph_merge.reconciliation.scope_migration import validate_scope_migrations
        normalized['signal_scope_migrations'] = validate_scope_migrations(
            payload['signal_scope_migrations'], ledger=normalized.get('signal_receipts', []))
    if payload.get("task_state") is not None:
        from cortex.capabilities.graph_merge.runtime.state import TaskState, canonical_hash

        task = TaskState.from_dict(payload["task_state"])
        normalized["task_state"] = task.to_dict()
        if payload.get("contract_versions") != task.versions.to_dict():
            raise ValueError("Task and workspace contracts differ")
        expected_graph = task.candidate_hash if task.stage == "committed" else task.base_hash
        if canonical_hash(normalized["current"]) != expected_graph:
            raise ValueError("Task state does not belong to the accepted workspace graph")
    return normalized


class WorkspaceStore:
    """One atomic JSON row per run, with optimistic serial revision checks.

    save returns revision 1 for creation, then increments by one. None permits
    creation only; updating a stored run always requires expected_revision.
    load returns detached JSON values (current is a GraphState JSON object).
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS merge_workspaces "
                "(run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)"
            )

    def load(self, run_id):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            row = connection.execute(
                "SELECT revision, payload FROM merge_workspaces WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        revision, raw = row
        try:
            payload = json.loads(raw)
            if payload.get("revision") != revision:
                raise ValueError("Payload and row revisions disagree")
            _normalize(payload)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise WorkspaceCorruption(
                f"Invalid checkpoint for {run_id!r}: {exc}"
            ) from exc
        return payload

    def save(self, run_id, payload, *, expected_revision=None):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Workspace run_id must be a nonempty string")
        normalized = _normalize(payload)
        normalized.pop("revision", None)
        # Serialize the expensive graph outside the short write transaction.
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        )
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM merge_workspaces WHERE run_id = ?", (run_id,)
            ).fetchone()
            actual = row[0] if row else 0
            if (row is not None and expected_revision is None) or (
                expected_revision is not None and expected_revision != actual
            ):
                raise WorkspaceRevisionMismatch(expected_revision, actual)
            revision = actual + 1
            stored = encoded[:-1] + f',"revision":{revision}}}'
            connection.execute(
                "INSERT INTO merge_workspaces(run_id, revision, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET revision=excluded.revision, payload=excluded.payload",
                (run_id, revision, stored),
            )
            connection.execute("COMMIT")
            return revision
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
