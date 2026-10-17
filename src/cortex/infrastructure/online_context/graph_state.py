from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from cortex.schema import GraphState


class OnlineGraphSourceError(ValueError):
    """Raised when an online-context source is not a complete GraphState."""


class FileSystemGraphStateLoader:
    def load(self, source: str | Path) -> GraphState:
        path = Path(source).expanduser().resolve()
        if path.is_dir():
            path = path / "graph_state.json"
        if not path.is_file():
            raise OnlineGraphSourceError(f"online context source has no graph_state.json: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return GraphState.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise OnlineGraphSourceError(
                f"online context source is not a valid GraphState: {path}: {exc}"
            ) from exc


__all__ = ["FileSystemGraphStateLoader", "OnlineGraphSourceError"]
