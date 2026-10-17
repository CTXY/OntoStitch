from __future__ import annotations

from nl2sql_baseline.models import QueryResult


def evaluate_results(predicted: QueryResult, gold: QueryResult) -> dict[str, int | str | None]:
    """Compare complete result-row sets, ignoring row order and duplicates."""
    equal = set(predicted.rows) == set(gold.rows)
    return {"exec": int(equal), "reason": None if equal else "Result row sets do not match."}
