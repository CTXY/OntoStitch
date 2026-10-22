"""Case-local immutable evidence. Remote tracing only receives its references."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex.capabilities.sql_training.artifacts.jsonl import (
    read_json,
    write_json_atomic,
)
from cortex.capabilities.sql_training.training.trace import sanitize_trace_payload


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def put(self, kind: str, payload: Any) -> str:
        evidence_id = uuid4().hex
        write_json_atomic(
            self.root / f"{evidence_id}.json",
            {
                "evidence_id": evidence_id,
                "kind": kind,
                "payload": sanitize_trace_payload(payload),
            },
        )
        return evidence_id

    def read(self, evidence_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", evidence_id):
            raise ValueError("invalid evidence ID")
        payload = read_json(self.root / f"{evidence_id}.json")
        if payload is None:
            raise ValueError("evidence does not exist in this case")
        return payload
