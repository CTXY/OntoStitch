from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from threading import Lock

from cortex.application.retrieve_db import RetrieveDbResult
from cortex.infrastructure.artifacts import render_graph_html
from cortex.schema import GraphSpace, GraphState, SchemaDefinition, export_inline_graph

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


class FileSystemRetrieveDbArtifactRecorder:
    """Publish inspectable RetrieveDB result, GraphSpace and HTML artifacts."""

    def __init__(self, workspace: str | Path, schema: SchemaDefinition) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.schema = SchemaDefinition.model_validate(schema.model_dump(mode="python"))
        self._sequence = 0
        self._lock = Lock()

    def write(self, result: RetrieveDbResult, *, trace_id: str) -> Path:
        validated = RetrieveDbResult.model_validate(result.model_dump(mode="python"))
        graph = validated.subgraph or GraphSpace()
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        artifact_dir = (
            self.workspace / "retrieval" / _safe_component(trace_id) / f"{sequence:04d}"
        )
        artifact_dir.mkdir(parents=True, exist_ok=False)
        try:
            _atomic_write(
                artifact_dir / "result.json",
                json.dumps(validated.public_payload(), ensure_ascii=False, indent=2)
                + "\n",
            )
            _atomic_write(
                artifact_dir / "graph.json",
                json.dumps(
                    export_inline_graph(GraphState(graph=graph)),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            _atomic_write(
                artifact_dir / "graph.html",
                render_graph_html(
                    graph,
                    self.schema,
                    title=f"RetrieveDB · {validated.question}",
                ),
            )
            _atomic_write(
                artifact_dir / "summary.json",
                json.dumps(
                    {
                        "question": validated.question,
                        "status": validated.status,
                        "initial_sql": validated.initial_sql,
                        "raw_retrieved_node_ids": validated.raw_retrieved_node_ids,
                        "selected_node_ids": validated.selected_node_ids,
                        "dependency_node_ids": validated.dependency_node_ids,
                        "generation_rule_node_ids": validated.generation_rule_node_ids,
                        "node_count": len(graph.nodes),
                        "edge_count": len(graph.edges),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
        except BaseException:
            for path in artifact_dir.iterdir():
                path.unlink(missing_ok=True)
            artifact_dir.rmdir()
            raise
        return artifact_dir


def _safe_component(value: str) -> str:
    normalized = _SAFE_COMPONENT.sub("_", value.strip()).strip("._")
    return normalized[:120] or "unknown-trace"


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


__all__ = ["FileSystemRetrieveDbArtifactRecorder"]
