"""Case-local evidence, completed-node results and the exact validation index.

Only LangGraph checkpoints own execution position. These records are immutable
facts or derived lookup data; they never select the current ontology head.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex.capabilities.sql_training.artifacts.jsonl import (
    read_json,
    write_json_atomic,
)
from cortex.capabilities.sql_training.graph.snapshots import fingerprint
from cortex.capabilities.sql_training.training.trace import sanitize_trace_payload


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def put(self, kind: str, payload: Any) -> str:
        evidence_id = uuid4().hex
        self._save(evidence_id, kind, sanitize_trace_payload(payload))
        return evidence_id

    def _save(self, evidence_id: str, kind: str, payload: Any) -> None:
        write_json_atomic(
            self.root / f"{evidence_id}.json",
            {
                "evidence_id": evidence_id,
                "kind": kind,
                "payload": payload,
            },
        )

    def read(self, evidence_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{64})", evidence_id):
            raise ValueError("invalid evidence ID")
        payload = read_json(self.root / f"{evidence_id}.json")
        if payload is None:
            raise ValueError("evidence does not exist in this case")
        return payload

    def read_index(self) -> dict[str, Any]:
        return read_json(self.root / "index.json", default={})

    def write_index(self, index: dict[str, Any]) -> None:
        write_json_atomic(self.root / "index.json", index)

    def completed(self, node: str, inputs: dict, action: Callable[[], dict]) -> dict:
        """Replay a completed step, including the write-before-checkpoint window."""
        key = fingerprint({"kind": "node_result", "node": node, "input": inputs})
        previous = read_json(self.root / f"{key}.json")
        if previous is not None:
            payload = previous["payload"]
            if (
                previous["kind"] != "node_result"
                or payload["node"] != node
                or payload["input_fingerprint"] != key
            ):
                raise ValueError("completed evidence does not match node input")
            return {**inputs, **payload["updates"]}
        output = action()
        updates = {
            name: value for name, value in output.items() if value != inputs[name]
        }
        # Execution facts must round-trip exactly, without trace sanitization.
        self._save(
            key,
            "node_result",
            {"node": node, "input_fingerprint": key, "updates": updates},
        )
        return output
