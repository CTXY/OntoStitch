from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from cortex.schema import GraphSpace


class RetrieveGraphSourceError(ValueError):
    """Raised when a DB retrieval source cannot be loaded as GraphSpace."""


class FileSystemGraphSpaceLoader:
    """Load GraphSpace from a SQL Training run directory or JSON file."""

    def load(self, source: str | Path) -> GraphSpace:
        source_path = Path(source).expanduser().resolve()
        if source_path.is_dir():
            graph_path = source_path / "graph.json"
            if not graph_path.is_file():
                # Merge artifacts publish GraphState as graph_state.json. The
                # retrieval boundary only needs its public GraphSpace.
                graph_path = source_path / "graph_state.json"
        else:
            graph_path = source_path
        if not graph_path.is_file():
            raise RetrieveGraphSourceError(
                "retrieve DB graph source does not contain graph.json or graph_state.json: "
                f"{source_path}"
            )
        try:
            payload = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RetrieveGraphSourceError(
                f"unable to read retrieve DB graph source {graph_path}: {exc}"
            ) from exc
        if isinstance(payload, dict) and "graph" in payload:
            payload = payload["graph"]
        try:
            return GraphSpace.model_validate(payload)
        except ValidationError as exc:
            raise RetrieveGraphSourceError(
                f"retrieve DB graph source is not a valid GraphSpace: {graph_path}: {exc}"
            ) from exc


__all__ = ["FileSystemGraphSpaceLoader", "RetrieveGraphSourceError"]
