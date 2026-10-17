from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    NodeMergeConflict,
    SchemaDefinition,
    calculate_schema_hash,
    validate_graph_state,
)

_PAYLOAD_FILES = ("nodes.jsonl", "edges.jsonl", "conflicts.jsonl")


class GraphStateBundleError(ValueError):
    """Raised when a persisted GraphState generation is incomplete or corrupt."""


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BundleFileEntry(_ManifestModel):
    path: str
    count: int
    sha256: str


class GraphStateManifest(_ManifestModel):
    format_version: Literal["graph-state-v1"] = "graph-state-v1"
    schema_hash: str
    nodes: BundleFileEntry
    edges: BundleFileEntry
    conflicts: BundleFileEntry


class FileSystemGraphStateRepository:
    def __init__(self, root: str | Path, schema: SchemaDefinition) -> None:
        self.root = Path(root)
        self.schema = schema

    def load(self) -> GraphState:
        current = self.root / "CURRENT"
        if not current.exists():
            return GraphState(graph=GraphSpace(), node_conflicts=())
        generation_id = current.read_text(encoding="utf-8").strip()
        if not generation_id or "/" in generation_id or generation_id.startswith("."):
            raise GraphStateBundleError("CURRENT contains an invalid generation ID")
        generation = self.root / "generations" / generation_id
        manifest_path = generation / "manifest.json"
        if not manifest_path.is_file():
            raise GraphStateBundleError("active generation has no manifest.json")
        try:
            manifest = GraphStateManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise GraphStateBundleError(f"invalid graph state manifest: {exc}") from exc
        if manifest.schema_hash != calculate_schema_hash(self.schema):
            raise GraphStateBundleError("graph state schema hash does not match active Schema")
        payloads = {}
        for label in ("nodes", "edges", "conflicts"):
            entry = getattr(manifest, label)
            expected_path = f"{label}.jsonl"
            if entry.path != expected_path:
                raise GraphStateBundleError(f"manifest {label} path is not allowed")
            path = generation / entry.path
            if not path.is_file():
                raise GraphStateBundleError(f"active generation is missing {entry.path}")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry.sha256:
                raise GraphStateBundleError(f"{entry.path} hash mismatch")
            rows = [json.loads(line) for line in raw.decode().splitlines() if line]
            if len(rows) != entry.count:
                raise GraphStateBundleError(f"{entry.path} count mismatch")
            payloads[label] = rows
        state = GraphState(
            graph=GraphSpace(
                nodes=tuple(GraphNode.model_validate(item) for item in payloads["nodes"]),
                edges=tuple(GraphEdge.model_validate(item) for item in payloads["edges"]),
            ),
            node_conflicts=tuple(
                NodeMergeConflict.model_validate(item) for item in payloads["conflicts"]
            ),
        )
        validate_graph_state(state, self.schema)
        return state

    def save(self, state: GraphState) -> None:
        validate_graph_state(state, self.schema)
        generations = self.root / "generations"
        generations.mkdir(parents=True, exist_ok=True)
        generation_id = uuid4().hex
        temporary = generations / f".tmp-{generation_id}"
        final = generations / generation_id
        temporary.mkdir()
        payloads = {
            "nodes": _jsonl(state.graph.nodes),
            "edges": _jsonl(state.graph.edges),
            "conflicts": _jsonl(state.node_conflicts),
        }
        entries = {}
        try:
            for label, raw in payloads.items():
                path = temporary / f"{label}.jsonl"
                _write_fsynced(path, raw)
                entries[label] = BundleFileEntry(
                    path=path.name,
                    count=len(
                        getattr(state.graph, label)
                        if label != "conflicts"
                        else state.node_conflicts
                    ),
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
            manifest = GraphStateManifest(
                schema_hash=calculate_schema_hash(self.schema),
                **entries,
            )
            _write_fsynced(
                temporary / "manifest.json",
                (manifest.model_dump_json(indent=2) + "\n").encode(),
            )
            _fsync_directory(temporary)
            os.rename(temporary, final)
            _fsync_directory(generations)
            # Reload the immutable generation before publishing its pointer.
            _publish_current(self.root, generation_id)
            self.load()
        except BaseException:
            if temporary.exists():
                for path in temporary.iterdir():
                    path.unlink()
                temporary.rmdir()
            raise


def _jsonl(items) -> bytes:
    rows = [
        json.dumps(
            item.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for item in items
    ]
    return (("\n".join(rows) + "\n") if rows else "").encode()


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_current(root: Path, generation_id: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "CURRENT.tmp"
    _write_fsynced(temporary, f"{generation_id}\n".encode())
    os.replace(temporary, root / "CURRENT")
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
