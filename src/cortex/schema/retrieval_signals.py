"""Read-only compatibility for declared Generation Rule recall signals."""

from __future__ import annotations

from typing import Any


def generation_rule_signals(value: Any) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Read nested or array signals without inferring rule applicability."""
    phrases: list[str] = []
    columns: list[str] = []
    operations: list[str] = []
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        if isinstance(entry, str):
            phrases.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        question = entry.get("question") or []
        if isinstance(question, dict):
            question = question.get("phrases") or []
        sql = entry.get("sql") or {}
        if not isinstance(sql, dict):
            sql = {}
        for supplied, output in (
            (question, phrases),
            (sql.get("columns") or [], columns),
            (sql.get("operations") or [], operations),
        ):
            if isinstance(supplied, list):
                output.extend(item for item in supplied if isinstance(item, str) and item.strip())
    return (
        tuple(dict.fromkeys(phrases)),
        tuple(dict.fromkeys(columns)),
        tuple(dict.fromkeys(operations)),
    )
