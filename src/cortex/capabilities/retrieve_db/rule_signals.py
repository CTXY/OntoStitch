"""Read canonical Generation Rule recall signals without deriving applicability."""

from __future__ import annotations

from typing import Any

from cortex.schema.semantic_validation import validate_rule_triggers


def generation_rule_signals(
    value: Any,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    # Non-rule nodes have no triggers.
    if value is None:
        return (), (), ()
    validate_rule_triggers(value)
    question = value.get("question", {})
    sql = value.get("sql", {})
    return tuple(
        tuple(dict.fromkeys(items))
        for items in (
            question.get("phrases", []),
            sql.get("columns", []),
            sql.get("operations", []),
        )
    )
