"""Completed node results bridge immutable domain writes and graph checkpoints."""

from collections.abc import Callable
from pathlib import Path

from cortex.capabilities.sql_training.artifacts.jsonl import (
    read_json,
    write_json_atomic,
)
from cortex.capabilities.sql_training.graph.snapshots import fingerprint
from cortex.capabilities.sql_training.training.workflow.state import CaseState


class OperationStore:
    def __init__(self, root: Path):
        self.root = root

    def execute(
        self, node: str, state: CaseState, action: Callable[[], CaseState]
    ) -> CaseState:
        inputs = state.model_dump(mode="json")
        key = fingerprint({"node": node, "input": inputs})
        path = self.root / f"{key}.json"
        previous = read_json(path)
        if previous is not None:
            if previous["input_fingerprint"] != key or previous["node"] != node:
                raise ValueError("operation result does not match node input")
            return CaseState.model_validate({**inputs, **previous["updates"]})
        output = action()
        updates = {
            key: value
            for key, value in output.model_dump(mode="json").items()
            if value != inputs[key]
        }
        write_json_atomic(
            path,
            {
                "node": node,
                "input_fingerprint": key,
                "updates": updates,
            },
        )
        return output
