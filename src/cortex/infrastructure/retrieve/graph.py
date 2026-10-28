from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from cortex.schema import GraphSpace, GraphState


class RetrieveGraphSourceError(ValueError):
    """Raised when a DB retrieval source cannot be loaded as GraphSpace."""


class FileSystemGraphSpaceLoader:
    """Load GraphSpace from a SQL Training run directory or JSON file."""

    def load(self, source: str | Path) -> GraphSpace:
        """Compatibility loader for callers that explicitly request a plain GraphSpace."""
        return self.load_state(source).graph

    def load_state(self, source: str | Path) -> GraphState:
        source_path = Path(source).expanduser().resolve()
        if source_path.is_dir():
            # Prefer the complete snapshot when both artifact forms exist.
            graph_path = source_path / "graph_state.json"
            if not graph_path.is_file():
                graph_path = source_path / "graph.json"
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
        try:
            return GraphState.model_validate(payload)
        except ValidationError as exc:
            raise RetrieveGraphSourceError(
                f"retrieve DB graph source is not a valid GraphSpace: {graph_path}: {exc}"
            ) from exc


__all__ = ["FileSystemGraphSpaceLoader", "RetrieveGraphSourceError"]
