"""Read-only views of grouped physical matches; never a stored legacy format."""

from __future__ import annotations

from typing import Any


def target_groups(properties: dict[str, Any]) -> list[list[str]]:
    """Return the exact grouped-match structure, without interpreting union or JOIN."""
    groups = properties.get("targets", [])
    if not isinstance(groups, list) or any(
        not isinstance(group, list)
        or not group
        or any(not isinstance(ref, str) or not ref.strip() for ref in group)
        for group in groups
    ):
        raise ValueError("targets must contain nonempty lists of physical node IDs")
    return [list(group) for group in groups]


def target_views(properties: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose stable positional references for discovery and retrieval only."""
    expressions = properties.get("target_expressions") or {}
    return [
        {
            "id": str(index),
            "physical_ids": group,
            "expression": expressions.get(str(index), ""),
        }
        for index, group in enumerate(target_groups(properties))
    ]


def select_target_groups(
    properties: dict[str, Any], indices: list[int]
) -> dict[str, Any]:
    """Select complete match groups and reindex their associated expressions."""
    groups = target_groups(properties)
    expressions = properties.get("target_expressions") or {}
    return {
        **properties,
        "targets": [groups[index] for index in indices],
        "target_expressions": {
            str(new): expressions[str(old)]
            for new, old in enumerate(indices)
            if str(old) in expressions
        },
    }


def validate_target_expression_columns(properties: dict[str, Any], nodes: dict) -> None:
    """Check catalog-resolved qualified columns, without SQL execution or alias inference."""
    import sqlglot
    from sqlglot import expressions as exp

    expressions = properties.get("target_expressions") or {}
    if not expressions:
        return
    groups = target_groups(properties)
    table_names = {
        node.id: str(node.properties.get("table_name", "")).casefold()
        for node in nodes.values()
        if node.node_type == "table"
    }
    columns_by_name: dict[tuple[str, str], set[str]] = {}
    for node in nodes.values():
        if node.node_type != "column":
            continue
        table_name = table_names.get(node.properties.get("table_ref"))
        column_name = str(node.properties.get("column_name", "")).casefold()
        if table_name and column_name:
            columns_by_name.setdefault((table_name, column_name), set()).add(node.id)
    for key, expression in expressions.items():
        allowed = set()
        for reference in groups[int(key)]:
            target = nodes.get(reference)
            if target is None:
                continue
            if target.node_type == "column":
                allowed.add(target.id)
            elif target.node_type == "dimension_value":
                allowed.add(target.properties.get("column_ref"))
            elif target.node_type == "table":
                allowed.update(
                    node.id
                    for node in nodes.values()
                    if node.node_type == "column"
                    and node.properties.get("table_ref") == target.id
                )
        try:
            tree = sqlglot.parse_one(expression, dialect="sqlite")
        except sqlglot.errors.SqlglotError:
            continue
        if tree is None:
            continue
        aliases = {
            table.alias.casefold() for table in tree.find_all(exp.Table) if table.alias
        }
        for column in tree.find_all(exp.Column):
            qualifier = column.table.casefold()
            if not qualifier or qualifier in aliases:
                continue
            matches = columns_by_name.get((qualifier, column.name.casefold()), set())
            if len(matches) == 1 and not matches <= allowed:
                raise ValueError(
                    f"Matching expression {key} uses {column.sql()} outside its target group"
                )
