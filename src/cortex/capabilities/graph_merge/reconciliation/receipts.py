"""Accepted per-check receipts, their integrity and dependency-bound reuse."""

import json
import re
from collections import Counter
from copy import deepcopy

from cortex.capabilities.graph_merge.runtime.state import canonical_hash


def policy_hash(session):
    versions = session.contract_versions
    return canonical_hash({"receipt_protocol": 1, "contracts": versions.to_dict()})


def read_dependencies(state):
    rows = {}
    for record in state.agent_recovery.values():
        for row in record.get("runtime", {}).get("journal", []):
            if row.get("name") in {
                "read_nodes",
                "read_relations",
                "read_evidence",
                "read_predicates",
                "counterexample",
            }:
                # Arguments contain complete-resource queries, not historical
                # tool responses; recompute them against accepted current.
                row = deepcopy(row)
                for key in ("ids", "node_ids"):
                    if key in row["args"]:
                        row["args"][key] = sorted(set(row["args"][key]))
                rows[canonical_hash(row)] = row
    return [rows[key] for key in sorted(rows)]


def dependency_facts(context, current, queries):
    from cortex.capabilities.graph_merge.reconciliation.scope import premise_object
    from cortex.schema.relation_contracts import authoring_schema, effective_schema

    nodes = {n.id: n for n in current.graph.nodes}
    edges = {e.id: e for e in current.graph.edges}
    evidence = {e.id: e.model_dump(mode="json") for e in context.evidence}
    contracts = effective_schema(context.schema, current.graph).edge_type_index()
    facts = []
    for query in queries:
        name, args = query["name"], query["args"]
        row = {"query": query}
        if name == "read_nodes":
            row["nodes"] = {
                i: premise_object(nodes[i]) if i in nodes else None
                for i in sorted(args["ids"])
            }
        elif name == "read_relations":
            if "checks" in args:
                from cortex.capabilities.graph_merge.reconciliation.scope import (
                    signal_scope,
                )

                scope = signal_scope(context, current, args["checks"])
                ids = set(scope.node_ids)
                chosen = [e for e in edges.values() if e.id in scope.edge_ids]
                row["relation_sets"] = scope.queries
            else:
                # Historical receipts retain their original conservative query.
                ids = set(args["node_ids"])
                chosen = [
                    e
                    for e in edges.values()
                    if e.from_node_id in ids or e.to_node_id in ids
                ]
            row["edges"] = [
                premise_object(e) for e in sorted(chosen, key=lambda e: e.id)
            ]
            ids |= {i for e in chosen for i in (e.from_node_id, e.to_node_id)}
            row["nodes"] = {
                i: premise_object(nodes[i]) if i in nodes else None for i in sorted(ids)
            }
        elif name == "read_evidence":
            row["evidence"] = {i: evidence.get(i) for i in sorted(args["ids"])}
        elif name == "read_predicates":
            schema = authoring_schema(context.schema, current.graph)
            row["contracts"] = [
                c.model_dump(mode="json")
                for c in sorted(schema.edge_types, key=lambda c: c.id)
                if any(
                    schema.is_node_type_compatible(args["from_node_type"], k)
                    for k in c.from_node_types
                )
                and any(
                    schema.is_node_type_compatible(args["to_node_type"], k)
                    for k in c.to_node_types
                )
            ]
        elif name == "counterexample":
            row["edges"] = [
                premise_object(e)
                for e in sorted(edges.values(), key=lambda e: e.id)
                if (
                    not args.get("from_node_id")
                    or e.from_node_id == args["from_node_id"]
                )
                and e.to_node_id == args["to_node_id"]
                and e.edge_type == args["edge_type"]
                and (
                    args.get("kind") != "concept_owner_absent"
                    or nodes[e.from_node_id].node_type == "concept"
                )
                and (
                    not args.get("alternative_id")
                    or not e.properties.get("alternative_ids")
                    or args["alternative_id"] in e.properties["alternative_ids"]
                )
            ]
        else:
            raise ValueError("Unsupported receipt dependency query")
        # Predicate meaning and supporting evidence are part of explicit reads.
        used_edges = row.get("edges", [])
        row["used_contracts"] = {
            e["edge_type"]: contracts[e["edge_type"]].model_dump(mode="json")
            for e in used_edges
            if e["edge_type"] in contracts
        }
        objects = [
            *used_edges,
            *(v for v in row.get("nodes", {}).values() if v is not None),
        ]
        refs = {canonical_hash(s) for obj in objects for s in obj.get("sources", [])}
        refs |= {
            canonical_hash(s) for obj in objects for s in obj.get("common_sources", [])
        }
        refs |= {
            canonical_hash(s)
            for obj in objects
            for alt in obj.get("alternatives", [])
            for s in alt.get("sources", [])
        }
        row["support"] = [
            e.model_dump(mode="json")
            for e in sorted(context.evidence, key=lambda e: e.id)
            if canonical_hash(e.source.model_dump(mode="json")) in refs
        ]
        facts.append(row)
    return facts


def premise_hash(session, check, dependencies=()):
    from cortex.capabilities.graph_merge.reconciliation.scope import signal_premise

    return canonical_hash(
        {
            "check": signal_premise(session.inputs, session.current, check),
            "supplementary": dependency_facts(
                session.inputs, session.current, dependencies
            ),
        }
    )


def is_accepted(session, check):
    policy = policy_hash(session)
    ledger = getattr(session, "signal_receipts", [])
    record = latest_receipt(ledger, check, policy_hash=policy)
    if record is None:
        return False
    return has_receipt(
        ledger,
        check,
        premise_hash=premise_hash(session, check, record["dependencies"]),
        policy_hash=policy,
    )


def record_acceptance(session, task, state, history_entry):
    if task.phase != "reconcile":
        return
    dependencies = read_dependencies(state)
    policy = policy_hash(session)
    for check in task.checks:
        record = make_receipt(
            check,
            premise_hash=premise_hash(session, check, dependencies),
            policy_hash=policy,
            history_entry=history_entry,
            candidate_hash=history_entry["candidate_hash"],
            dependencies=dependencies,
        )
        session.signal_receipts = [*getattr(session, "signal_receipts", []), record]


def stable_task_id(session, checks):
    from cortex.capabilities.graph_merge.reconciliation.scope import signal_key

    rows = []
    for c in checks:
        previous = latest_receipt(
            getattr(session, "signal_receipts", []), c, policy_hash=policy_hash(session)
        )
        rows.append(
            (
                signal_key(c),
                premise_hash(session, c, previous["dependencies"] if previous else ()),
            )
        )
    rows.sort()
    return (
        "structure-"
        + canonical_hash({"checks": rows, "policy": policy_hash(session)})[:24]
    )


class ReceiptValidationError(ValueError):
    """An accepted receipt is malformed or does not match its commit."""


RECEIPT_VERSION = 1


_HASH = re.compile(r"[0-9a-f]{64}\Z")


_FIELDS = frozenset(
    {
        "version",
        "check_key",
        "check",
        "check_hash",
        "premise_hash",
        "policy_hash",
        "dependencies",
        "task_id",
        "accepted_revision",
        "candidate_hash",
        "history_hash",
        "record_hash",
    }
)


def _json(value):
    """Detach JSON while rejecting NaN, non-string keys and custom objects."""

    def keys(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ReceiptValidationError("Receipt JSON object keys must be strings")
            for child in item.values():
                keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                keys(child)

    keys(value)
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ReceiptValidationError("Receipt must contain finite JSON data") from exc


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ReceiptValidationError(f"Receipt {label} must be a nonempty string")
    return value


def _hash(value, label):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ReceiptValidationError(f"Receipt {label} must be a SHA256 hex digest")
    return value


def _check(value):
    if not isinstance(value, dict):
        raise ReceiptValidationError("Receipt check must be an object")
    value = _json(value)
    _text(value.get("kind"), "check kind")
    if "id" in value:
        _text(value["id"], "check ID")
    if "signal_id" in value:
        _text(value["signal_id"], "signal ID")
    from cortex.capabilities.graph_merge.reconciliation.scope import (
        normalize_signal,
    )

    return normalize_signal(value)


def check_key(check):
    """Semantic check identity, independent of task ID, batching and cycles.

    Named signals retain a stable key when their requirements change; check_hash
    then invalidates the old receipt. Anonymous obligations are content keyed.
    """
    check = _check(check)
    ident = check.get("id") or check.get("signal_id")
    identity = {"kind": check["kind"], "id": ident} if ident is not None else check
    return canonical_hash(identity)


def _dependencies(value):
    if not isinstance(value, (list, tuple)):
        raise ReceiptValidationError(
            "Receipt dependencies must be an array of query objects"
        )
    rows = _json(list(value))
    if any(not isinstance(row, dict) or not row for row in rows):
        raise ReceiptValidationError(
            "Receipt dependencies must contain nonempty query objects"
        )
    hashes = [canonical_hash(row) for row in rows]
    if len(set(hashes)) != len(hashes):
        raise ReceiptValidationError("Receipt dependencies must not repeat a query")
    return rows


def _history_binding(entry, policy_hash, candidate_hash):
    if not isinstance(entry, dict):
        raise ReceiptValidationError("Receipt requires an accepted history object")
    if entry.get("phase") != "reconcile" or entry.get("assessment") != "graph_assess":
        raise ReceiptValidationError(
            "Receipt requires independently accepted reconciliation history"
        )
    _text(entry.get("task_id"), "history task ID")
    revision = entry.get("revision")
    if type(revision) is not int or revision < 1:
        raise ReceiptValidationError("Receipt requires a positive accepted revision")
    if entry.get("candidate_hash") != candidate_hash:
        raise ReceiptValidationError(
            "Receipt candidate does not match accepted history"
        )
    if entry.get("reconciliation_receipt_policy_hash") != policy_hash:
        raise ReceiptValidationError("Receipt policy does not match accepted history")
    if "reconciliation_check_hashes" in entry:
        if not isinstance(entry["reconciliation_check_hashes"], list):
            raise ReceiptValidationError(
                "Accepted history check manifest must be an array"
            )
        for item in entry["reconciliation_check_hashes"]:
            _hash(item, "history check manifest hash")


def make_receipt(
    check, *, premise_hash, policy_hash, history_entry, candidate_hash, dependencies=()
):
    """Create only after independent accept, inside the graph commit transaction."""
    check = _check(check)
    for value, name in (
        (premise_hash, "premise hash"),
        (policy_hash, "policy hash"),
        (candidate_hash, "candidate hash"),
    ):
        _hash(value, name)
    history_entry = _json(history_entry)
    _history_binding(history_entry, policy_hash, candidate_hash)
    if (
        "reconciliation_check_hashes" in history_entry
        and canonical_hash(check) not in history_entry["reconciliation_check_hashes"]
    ):
        raise ReceiptValidationError(
            "Receipt check is absent from accepted history manifest"
        )
    record = {
        "version": RECEIPT_VERSION,
        "check_key": check_key(check),
        "check": check,
        "check_hash": canonical_hash(check),
        "premise_hash": premise_hash,
        "policy_hash": policy_hash,
        "dependencies": _dependencies(dependencies),
        "task_id": history_entry["task_id"],
        "accepted_revision": history_entry["revision"],
        "candidate_hash": candidate_hash,
        "history_hash": canonical_hash(history_entry),
    }
    record["record_hash"] = canonical_hash(record)
    return record


def validate_receipt_ledger(rows, *, history, current=None):
    """Validate persisted records and their exact accepted-commit bindings.

    Missing legacy fields must be handled explicitly by the caller; None is not
    an empty ledger. Old receipts may describe older accepted graphs. Only a
    receipt at the latest history revision can be compared with current.
    """
    if not isinstance(rows, list):
        raise ReceiptValidationError("Reconciliation receipt ledger must be an array")
    if not isinstance(history, (list, tuple)):
        raise ReceiptValidationError("Receipt history must be an array")
    rows = _json(rows)
    by_revision = {}
    for entry in history:
        if not isinstance(entry, dict):
            raise ReceiptValidationError("Receipt history entries must be objects")
        revision = entry.get("revision")
        if type(revision) is int:
            by_revision.setdefault(revision, []).append(entry)
    latest_revision = max(by_revision, default=0)
    if hasattr(current, "model_dump"):
        current = current.model_dump(mode="json")
    current_hash = canonical_hash(current) if current is not None else None
    seen = set()
    seen_checks = set()
    actual_checks = {}
    last_revision = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ReceiptValidationError("Receipt fields are incomplete or unknown")
        if type(row["version"]) is not int or row["version"] != RECEIPT_VERSION:
            raise ReceiptValidationError("Unsupported reconciliation receipt version")
        for name in (
            "check_key",
            "check_hash",
            "premise_hash",
            "policy_hash",
            "candidate_hash",
            "history_hash",
            "record_hash",
        ):
            _hash(row[name], name)
        check = _check(row["check"])
        if row["check_key"] != check_key(check) or row["check_hash"] != canonical_hash(
            check
        ):
            raise ReceiptValidationError(
                "Receipt check content or identity hash mismatch"
            )
        _dependencies(row["dependencies"])
        if row["record_hash"] != canonical_hash(
            {k: v for k, v in row.items() if k != "record_hash"}
        ):
            raise ReceiptValidationError("Receipt record hash mismatch")
        if row["record_hash"] in seen:
            raise ReceiptValidationError("Duplicate reconciliation receipt record")
        seen.add(row["record_hash"])
        revision = row["accepted_revision"]
        if type(revision) is not int or revision < 1 or revision < last_revision:
            raise ReceiptValidationError(
                "Receipt revisions must be positive and append ordered"
            )
        last_revision = revision
        identity = (revision, row["check_key"])
        if identity in seen_checks:
            raise ReceiptValidationError(
                "Duplicate receipt check in one accepted revision"
            )
        seen_checks.add(identity)
        actual_checks.setdefault(revision, []).append(row["check_hash"])
        matches = by_revision.get(revision, [])
        if len(matches) != 1:
            raise ReceiptValidationError(
                "Receipt has no unique accepted history revision"
            )
        entry = matches[0]
        _history_binding(entry, row["policy_hash"], row["candidate_hash"])
        if row["task_id"] != entry["task_id"] or row["history_hash"] != canonical_hash(
            entry
        ):
            raise ReceiptValidationError("Receipt accepted history binding mismatch")
        if (
            revision == latest_revision
            and current_hash is not None
            and row["candidate_hash"] != current_hash
        ):
            raise ReceiptValidationError(
                "Latest receipt does not describe the accepted current snapshot"
            )
    for revision, entries in by_revision.items():
        for entry in entries:
            if "reconciliation_check_hashes" not in entry:
                continue
            expected = entry["reconciliation_check_hashes"]
            if not isinstance(expected, list):
                raise ReceiptValidationError(
                    "Accepted history check manifest must be an array"
                )
            for item in expected:
                _hash(item, "history check manifest hash")
            if len(entries) != 1 or Counter(actual_checks.get(revision, [])) != Counter(
                expected
            ):
                raise ReceiptValidationError(
                    "Receipt ledger does not match accepted history check manifest"
                )
    return rows


def latest_receipt(rows, check, *, policy_hash):
    """Return the latest record for this semantic key under the exact policy."""
    _hash(policy_hash, "policy hash")
    key = check_key(check)
    for row in reversed(rows):
        if row["check_key"] == key and row["policy_hash"] == policy_hash:
            return deepcopy(row)
    return None


def has_receipt(rows, check, *, premise_hash, policy_hash):
    """Reuse only the latest accepted decision with unchanged relevant facts."""
    _hash(premise_hash, "premise hash")
    row = latest_receipt(rows, check, policy_hash=policy_hash)
    return (
        row is not None
        and row["check_hash"] == canonical_hash(_check(check))
        and row["premise_hash"] == premise_hash
    )
