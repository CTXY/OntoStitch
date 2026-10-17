from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nl2sql_baseline.models import QARecord


class AmemRetrievalAdapter:
    """Adapt persisted A-MEM retrieval rows to baseline reference memory text.

    The adapter deliberately reads only the ``memories`` field.  A-MEM rows also
    contain the current dev case and its gold SQL for audit/evaluation, but those
    fields must never enter the generation prompt.
    """

    def __init__(self, source: Path, tests: list[QARecord], *, top_k: int = 5):
        self.source = source.resolve()
        self.top_k = top_k
        self._by_case = self._load(tests)

    def memory_for(self, record: QARecord) -> str:
        key = (record.database_id, record.source_id)
        try:
            return self._by_case[key]
        except KeyError as exc:
            raise ValueError(
                f"A-MEM retrieval is missing database={record.database_id!r} "
                f"question_id={record.source_id!r}"
            ) from exc

    def _load(self, tests: list[QARecord]) -> dict[tuple[str, str], str]:
        expected = {(row.database_id, row.source_id): row for row in tests}
        paths = self._paths()
        if not paths:
            raise ValueError(f"A-MEM retrieval source contains no JSONL files: {self.source}")
        loaded: dict[tuple[str, str], str] = {}
        for path in paths:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"{path}:{line_number}: retrieval row must be an object")
                database_value = row.get("db_id")
                if database_value is None:
                    database_value = row.get("database_id")
                source_value = row.get("question_id")
                if source_value is None:
                    source_value = row.get("source_id")
                database_id = str(database_value or "").strip()
                source_id = str(source_value if source_value is not None else "").strip()
                key = (database_id, source_id)
                if key not in expected:
                    raise ValueError(
                        f"{path}:{line_number}: unknown A-MEM case database={database_id!r} "
                        f"question_id={source_id!r}"
                    )
                if key in loaded:
                    raise ValueError(f"duplicate A-MEM retrieval for {database_id}/{source_id}")
                memories = row.get("memories")
                if not isinstance(memories, list) or len(memories) != self.top_k:
                    raise ValueError(
                        f"{path}:{line_number}: expected exactly {self.top_k} memories for "
                        f"{database_id}/{source_id}"
                    )
                loaded[key] = self._render(memories)
        missing = sorted(set(expected) - set(loaded))
        if missing:
            raise ValueError(f"A-MEM retrieval is missing {len(missing)} dev cases: {missing[:5]}")
        return loaded

    def _paths(self) -> list[Path]:
        if self.source.is_file():
            return [self.source]
        if not self.source.is_dir():
            raise ValueError(f"A-MEM retrieval source does not exist: {self.source}")
        merged = self.source / "dev_retrieved_memories.jsonl"
        if merged.is_file():
            return [merged]
        return sorted(self.source.glob("*/dev_retrieved_memories.jsonl"))

    @staticmethod
    def _render(memories: list[Any]) -> str:
        rendered: list[str] = ["Retrieved A-MEM notes:"]
        for index, memory in enumerate(memories, 1):
            if not isinstance(memory, dict):
                raise ValueError("A-MEM memory entries must be objects")
            content = str(memory.get("content") or "").strip()
            if not content:
                raise ValueError("A-MEM memory entry has empty content")
            rendered.append(f"\n[{index}]\n{content}")
            context = str(memory.get("context") or "").strip()
            keywords = memory.get("keywords")
            tags = memory.get("tags")
            if context:
                rendered.append(f"Context: {context}")
            if isinstance(keywords, list) and keywords:
                rendered.append("Keywords: " + ", ".join(str(item) for item in keywords))
            if isinstance(tags, list) and tags:
                rendered.append("Tags: " + ", ".join(str(item) for item in tags))
        return "\n".join(rendered)
