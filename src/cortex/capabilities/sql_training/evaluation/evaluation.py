from __future__ import annotations

from typing import Any, TypeAlias

QueryResult: TypeAlias = dict[str, list[str] | list[list[Any]]]


def evaluate(
    ground_truth_sql: str,
    predicted_res: QueryResult,
    ground_truth_res: QueryResult,
) -> dict[str, int | str | None]:
    """Compare complete row sets; ignore row order, duplicates and column aliases."""
    try:
        if not isinstance(ground_truth_sql, str):
            return {"exec": -1, "reason": "ground_truth_sql must be a string."}
        for value, name in (
            (predicted_res, "predicted_res"),
            (ground_truth_res, "ground_truth_res"),
        ):
            error = _validate_query_result(value, name)
            if error:
                return {"exec": -1, "reason": error}
        equal = set(tuple(row) for row in predicted_res["rows"]) == set(
            tuple(row) for row in ground_truth_res["rows"]
        )
        return {"exec": int(equal), "reason": None if equal else "Result row sets do not match."}
    except Exception as exc:  # noqa: BLE001 - invalid evaluation input remains reportable.
        return {"exec": -1, "reason": f"Unexpected evaluation error: {exc}"}


def _validate_query_result(value: object, name: str) -> str | None:
    if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
        return f"{name} must contain a rows list."
    if "columns" in value and not isinstance(value["columns"], list):
        return f"{name}.columns must be a list when provided."
    for index, row in enumerate(value["rows"], start=1):
        if not isinstance(row, list | tuple):
            return f"{name}.rows[{index}] must be a list or tuple."
    return None
