from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nl2sql_baseline.models import QARecord


class TkBoostRepairRuleAdapter:
    """Adapt TK-Boost per-case repair rules to baseline reference memory text.

    The source rows may contain dev-case SQL drafts, refined SQL, verdicts, and gold SQL for
    TK-Boost's own repair process.  This adapter deliberately renders only retrieved repair
    rules and retrieval metadata so the baseline does not receive dev answers.
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
                f"TK-Boost repair rules are missing database={record.database_id!r} "
                f"question_id={record.source_id!r}"
            ) from exc

    def _load(self, tests: list[QARecord]) -> dict[tuple[str, str], str]:
        expected = {(row.database_id, row.source_id): row for row in tests}
        if not self.source.is_file():
            raise ValueError(f"TK-Boost repair rule source is not a file: {self.source}")
        loaded: dict[tuple[str, str], str] = {}
        for line_number, line in enumerate(self.source.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{self.source}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}:{line_number}: retrieval row must be an object")
            database_id = str(row.get("db_id") or row.get("database_id") or "").strip()
            source_value = row.get("question_id")
            if source_value is None:
                source_value = row.get("source_id")
            source_id = str(source_value if source_value is not None else "").strip()
            key = (database_id, source_id)
            if key not in expected:
                continue
            if key in loaded:
                raise ValueError(f"duplicate TK-Boost retrieval for {database_id}/{source_id}")
            rules = row.get("retrieved_repair_rules")
            if not isinstance(rules, list):
                raise ValueError(
                    f"{self.source}:{line_number}: retrieved_repair_rules must be a list"
                )
            loaded[key] = self._render(rules, top_k=self.top_k)
        missing = sorted(set(expected) - set(loaded))
        if missing:
            raise ValueError(
                f"TK-Boost repair rules are missing {len(missing)} dev cases: {missing[:5]}"
            )
        return loaded

    @staticmethod
    def _render(rules: list[Any], *, top_k: int) -> str:
        rendered: list[str] = ["Retrieved TK-Boost repair rules:"]
        selected = 0
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("TK-Boost repair rule entries must be objects")
            content = str(rule.get("rule") or "").strip()
            if not content:
                continue
            selected += 1
            if selected > top_k:
                break
            index = selected
            rendered.append(f"\n[{index}]\n{content}")
            scope = str(rule.get("scope") or "").strip()
            table = str(rule.get("table") or "").strip()
            column = str(rule.get("column") or "").strip()
            operations = rule.get("sql_operations")
            matches = rule.get("matches")
            metadata: list[str] = []
            if scope:
                metadata.append(f"scope={scope}")
            if table:
                metadata.append(f"table={table}")
            if column:
                metadata.append(f"column={column}")
            if isinstance(operations, list) and operations:
                metadata.append("operations=" + ", ".join(str(item) for item in operations))
            if isinstance(matches, list) and matches:
                metadata.append("matches=" + ", ".join(str(item) for item in matches))
            if metadata:
                rendered.append("Metadata: " + "; ".join(metadata))
        if selected == 0:
            rendered.append("\nNo nonempty TK-Boost repair rules were retrieved for this case.")
        return "\n".join(rendered)
