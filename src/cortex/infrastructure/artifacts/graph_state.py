from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from cortex.schema import (
    GraphSpace,
    GraphState,
    SchemaDefinition,
    calculate_schema_hash,
)

_PAYLOAD_FILES = ("nodes.jsonl", "edges.jsonl")


class GraphStateBundleError(ValueError):
    """Raised when a persisted GraphState generation is incomplete or corrupt."""


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BundleFileEntry(_ManifestModel):
    path: str
    count: int
    sha256: str


class GraphStateManifest(_ManifestModel):
    format_version: Literal["graph-state-v2", "graph-state-v3-inline"] = (
        "graph-state-v3-inline"
    )
    schema_hash: str
    nodes: BundleFileEntry
    edges: BundleFileEntry
    conflicts: BundleFileEntry | None = None
    relation_contracts: BundleFileEntry | None = None


class FileSystemGraphStateRepository:
    def __init__(self, root: str | Path, schema: SchemaDefinition) -> None:
        self.root = Path(root)
        self.schema = schema

    def load(self) -> GraphState:
        current = self.root / "CURRENT"
        if not current.exists():
            return GraphState(graph=GraphSpace())
        generation_id = current.read_text(encoding="utf-8").strip()
        if not generation_id or "/" in generation_id or generation_id.startswith("."):
            raise GraphStateBundleError("CURRENT contains an invalid generation ID")
        return self._load_generation(generation_id)

    @property
    def revision(self) -> str:
        current = self.root / "CURRENT"
        return (
            current.read_text(encoding="utf-8").strip() if current.exists() else "empty"
        )

    def load_snapshot(self) -> tuple[GraphState, str]:
        revision = self.revision
        state = (
            GraphState(graph=GraphSpace())
            if revision == "empty"
            else self._load_generation(revision)
        )
        return state, revision

    def _load_generation(self, generation_id: str) -> GraphState:
        if not generation_id or "/" in generation_id or generation_id.startswith("."):
            raise GraphStateBundleError("invalid generation ID")
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
            raise GraphStateBundleError(
                "graph state schema hash does not match active Schema"
            )
        payloads = {}
        if manifest.format_version == "graph-state-v2" and manifest.conflicts is None:
            raise GraphStateBundleError("legacy generation requires conflicts entry")
        if (
            manifest.format_version == "graph-state-v3-inline"
            and manifest.conflicts is not None
        ):
            raise GraphStateBundleError(
                "inline generation cannot contain a conflict sidecar"
            )
        labels = (
            ("nodes", "edges", "conflicts")
            if manifest.format_version == "graph-state-v2"
            else ("nodes", "edges")
        )
        if manifest.relation_contracts is not None:
            labels = (*labels, "relation_contracts")
        for label in labels:
            entry = getattr(manifest, label)
            expected_path = f"{label}.jsonl"
            if entry.path != expected_path:
                raise GraphStateBundleError(f"manifest {label} path is not allowed")
            path = generation / entry.path
            if not path.is_file():
                raise GraphStateBundleError(
                    f"active generation is missing {entry.path}"
                )
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry.sha256:
                raise GraphStateBundleError(f"{entry.path} hash mismatch")
            rows = [json.loads(line) for line in raw.decode().splitlines() if line]
            if len(rows) != entry.count:
                raise GraphStateBundleError(f"{entry.path} count mismatch")
            payloads[label] = rows
        payload = {"graph": {"nodes": payloads["nodes"], "edges": payloads["edges"]}}
        if "relation_contracts" in payloads:
            payload["graph"]["relation_contracts"] = payloads["relation_contracts"]
        if manifest.format_version == "graph-state-v2":
            payload["node_conflicts"] = payloads["conflicts"]
        state = GraphState.model_validate(payload)
        from cortex.application.ontology_graph import GraphManager

        GraphManager(self.schema).validate_state(state)
        return state

    def save(self, state: GraphState) -> None:
        self.commit(state)

    def commit(self, state: GraphState, expected_revision: str | None = None) -> str:
        # One process-wide writer per local graph, including independent managers.
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".write.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if expected_revision is not None and expected_revision != self.revision:
                    from cortex.application.ontology_graph import GraphRejected, issue

                    raise GraphRejected(
                        [
                            issue(
                                "REVISION_MISMATCH",
                                "The stored graph revision changed.",
                                "Reload the graph before retrying.",
                                details={"current_revision": self.revision},
                            )
                        ],
                        self.revision,
                    )
                return self._save(state)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _save(self, state: GraphState) -> str:
        from cortex.application.ontology_graph import GraphManager

        GraphManager(self.schema).validate_state(state)
        generations = self.root / "generations"
        generations.mkdir(parents=True, exist_ok=True)
        generation_id = uuid4().hex
        temporary = generations / f".tmp-{generation_id}"
        final = generations / generation_id
        temporary.mkdir()
        payloads = {
            "nodes": _jsonl(state.graph.nodes),
            "edges": _jsonl(state.graph.edges),
        }
        if state.graph.relation_contracts:
            payloads["relation_contracts"] = _jsonl(state.graph.relation_contracts)
        entries = {}
        try:
            for label, raw in payloads.items():
                path = temporary / f"{label}.jsonl"
                _write_fsynced(path, raw)
                entries[label] = BundleFileEntry(
                    path=path.name,
                    count=len(getattr(state.graph, label)),
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
            manifest = GraphStateManifest(
                schema_hash=calculate_schema_hash(self.schema),
                **entries,
            )
            _write_fsynced(
                temporary / "manifest.json",
                (manifest.model_dump_json(indent=2, exclude_none=True) + "\n").encode(),
            )
            _fsync_directory(temporary)
            os.rename(temporary, final)
            _fsync_directory(generations)
            # Validate the staged generation before publishing its pointer.
            self._load_generation(generation_id)
            _publish_current(self.root, generation_id)
            return generation_id
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
