"""Scope-aware physical column lineage for extraction (never execute rewritten SQL)."""

from __future__ import annotations

from collections import defaultdict

import sqlglot
from sqlglot import expressions as exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_node_id


def physical_column_sources(
    tree: exp.Expression,
    table_columns: dict[str, set[str]],
) -> dict[int, tuple[str, str]]:
    """Map original AST columns to physical names without crossing alias scopes.

    Only direct/transparent projections carry lineage. Computed and ambiguous
    outputs remain unresolved. Correlated subqueries may resolve in their parent.
    """
    scopes = list(traverse_scope(tree))
    by_select = {id(scope.expression): scope for scope in scopes}
    catalog = {
        t.casefold(): (t, {c.casefold(): c for c in cols})
        for t, cols in (table_columns or {}).items()
    }

    def source_column(source, name, active):
        if isinstance(source, exp.Table):
            entry = catalog.get(source.name.casefold())
            if entry is None or name.casefold() not in entry[1]:
                return None
            return entry[0], entry[1][name.casefold()]
        if not isinstance(source, Scope) or not isinstance(
            source.expression, exp.Select
        ):
            return None
        outputs = source.expression.expressions
        names = source.outer_columns or [item.alias_or_name for item in outputs]
        matching = [
            item
            for output, item in zip(names, outputs)
            if output.casefold() == name.casefold()
        ]
        if len(matching) != 1:
            return None
        item = matching[0]
        if isinstance(item, exp.Alias):
            item = item.this
        return resolve(item, source, active) if isinstance(item, exp.Column) else None

    def source_exposes(source, name):
        # Name visibility and physical lineage are different: a derived local
        # output shadows outer columns even when its lineage is intentionally None.
        if isinstance(source, exp.Table):
            if not catalog:
                return True
            entry = catalog.get(source.name.casefold())
            return entry is None or name.casefold() in entry[1]
        if isinstance(source, Scope):
            names = source.outer_columns or source.expression.named_selects
            return any(
                output == "*" or output.casefold() == name.casefold()
                for output in names
            )
        return True

    def resolve(column, scope, active=frozenset()):
        key = (id(column), id(scope))
        if key in active or column.is_star:
            return None
        active = active | {key}
        if not column.table and isinstance(scope.expression, exp.Select):
            clause = column.find_ancestor(exp.Order, exp.Group, exp.Having)
            if (
                clause is not None
                and clause.find_ancestor(exp.Select) is scope.expression
            ):
                aliases = [
                    item
                    for item in scope.expression.expressions
                    if isinstance(item, exp.Alias)
                    and item.alias.casefold() == column.name.casefold()
                ]
                if aliases:
                    return source_column(scope, column.name, active)
        sources = {
            name.casefold(): source
            for name, (_, source) in scope.selected_sources.items()
        }
        if column.table:
            source = sources.get(column.table.casefold())
            if source is not None:
                return source_column(source, column.name, active)
        else:
            candidates = [
                source
                for source in sources.values()
                if source_exposes(source, column.name)
            ]
            if len(candidates) == 1:
                return source_column(candidates[0], column.name, active)
            if candidates:
                return None
        if scope.can_be_correlated and scope.parent is not None:
            return resolve(column, scope.parent, active)
        return None

    result = {}
    for column in tree.find_all(exp.Column):
        select = column.find_ancestor(exp.Select)
        scope = by_select.get(id(select))
        if scope is not None and (resolved := resolve(column, scope)) is not None:
            result[id(column)] = resolved
    return result


def collect_physical_column_references(
    sql: str,
    *,
    available_column_refs: set[str],
    dialect: str = "sqlite",
    table_columns: dict[str, set[str]] | None = None,
) -> set[str]:
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as exc:
        raise ValueError(f"Failed to parse Gold SQL: {exc}") from exc
    if tree is None:
        raise ValueError("Failed to parse Gold SQL")
    if table_columns is None:
        table_columns = defaultdict(set)
        for reference in available_column_refs:
            _, _, suffix = reference.partition(".")
            table, _, column = suffix.rpartition(".")
            table_columns[table].add(column)
    sources = physical_column_sources(tree, table_columns=table_columns)
    return {
        reference
        for table, column in sources.values()
        if (reference := make_node_id(NodeType.COLUMN, table, column))
        in available_column_refs
    }
