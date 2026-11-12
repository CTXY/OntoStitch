"""Physical equality relationships, independent of a query's JOIN operator."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import sqlglot
from sqlglot import expressions

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.schema import GraphEdge, SourceReference
from cortex.schema.physical_joins import (
    _edge_id,
    _public_join_edge,
    join_edge_key,
    merge_join_edges,
)


def gold_join_edge(
    *,
    from_table: str,
    to_table: str,
    column_pairs: Iterable[tuple[str, str]],
    source_id: str,
) -> GraphEdge:
    edge = GraphEdge(
        id="pending",
        edge_type="joins_with",
        from_node_id=make_node_id(NodeType.TABLE, from_table),
        to_node_id=make_node_id(NodeType.TABLE, to_table),
        properties={
            "column_pairs": [
                {
                    "from_column_ref": make_node_id(NodeType.COLUMN, from_table, left),
                    "to_column_ref": make_node_id(NodeType.COLUMN, to_table, right),
                }
                for left, right in column_pairs
            ]
        },
        sources=(
            SourceReference(source_id=source_id, locator="qa", case_id=source_id),
        ),
    )
    edge = _public_join_edge(edge)
    return edge.model_copy(update={"id": _edge_id(join_edge_key(edge))})


def constraint_cardinality(
    from_columns: set[str],
    to_columns: set[str],
    from_keys: Iterable[Iterable[str]],
    to_keys: Iterable[Iterable[str]],
) -> str | None:
    """A complete declared unique key bounds matches; data samples are irrelevant."""
    left = any(key and set(key) <= from_columns for key in from_keys)
    right = any(key and set(key) <= to_columns for key in to_keys)
    return "1:1" if left and right else "1:N" if left else "N:1" if right else None


def extract_gold_join_edges(
    *,
    sql: str,
    dialect: str,
    source_id: str,
    table_columns: dict[str, set[str]],
    skipped: list[dict[str, str]] | None = None,
) -> list[GraphEdge]:
    """Extract complete equality keys from explicit JOIN ON/USING only.

    Each ON must consist entirely of direct column equalities to its joined
    table. Derived sources, self-relations, cross-source WHERE conditions and
    joins inside IN/EXISTS are conservatively skipped as whole relationships.
    """
    from sqlglot.optimizer.scope import traverse_scope

    tree = sqlglot.parse_one(sql, dialect=dialect)
    catalog = {
        name.casefold(): (name, {col.casefold(): col for col in columns})
        for name, columns in table_columns.items()
    }
    edges = []

    def skip(join, reason):
        if skipped is not None:
            skipped.append({"reason": reason, "sql": join.sql(dialect=dialect)})

    for scope in traverse_scope(tree):
        select = scope.expression
        if not isinstance(select, expressions.Select):
            continue
        joins = select.args.get("joins") or []
        if select.find_ancestor(expressions.In, expressions.Exists) is not None:
            for join in joins:
                skip(join, "existence_subquery")
            continue
        sources = {
            alias.casefold(): source
            for alias, (_, source) in scope.selected_sources.items()
        }
        physical_sources = {
            alias: source
            for alias, source in sources.items()
            if isinstance(source, expressions.Table)
            and source.name.casefold() in catalog
        }

        def resolve(column, sources=sources):
            if not isinstance(column, expressions.Column) or column.is_star:
                return None
            candidates = (
                [sources.get(column.table.casefold())]
                if column.table
                else list(sources.values())
            )
            matches = []
            for source in candidates:
                # Unknown/derived local outputs cannot establish a physical key.
                if not isinstance(source, expressions.Table):
                    if not column.table:
                        return None
                    continue
                entry = catalog.get(source.name.casefold())
                if entry and column.name.casefold() in entry[1]:
                    matches.append((source, entry[0], entry[1][column.name.casefold()]))
            return matches[0] if len(matches) == 1 else None

        source = select.args.get("from_")
        previous = [source.this] if source is not None else []
        where = select.args.get("where")
        for join in joins:
            right = physical_sources.get(join.this.alias_or_name.casefold())
            using, on = join.args.get("using"), join.args.get("on")
            terms = []
            if right is None:
                skip(join, "non_physical_source")
            elif using:
                left_candidates = [
                    table
                    for table in previous
                    if isinstance(table, expressions.Table)
                    and table.name.casefold() in catalog
                    and all(
                        name.name.casefold() in catalog[table.name.casefold()][1]
                        for name in using
                    )
                ]
                if len(left_candidates) != 1:
                    skip(join, "ambiguous_using_source")
                else:
                    left = left_candidates[0]
                    terms = [
                        expressions.EQ(
                            this=expressions.column(
                                name.name, table=left.alias_or_name
                            ),
                            expression=expressions.column(
                                name.name, table=right.alias_or_name
                            ),
                        )
                        for name in using
                    ]
            elif on is not None:
                terms = _and_terms(on)
            else:
                skip(join, "missing_explicit_equality_key")
            previous.append(join.this)
            if not terms:
                continue
            groups = defaultdict(list)
            valid = True
            for term in terms:
                if not isinstance(term, expressions.EQ):
                    valid = False
                    break
                left, other = resolve(term.left), resolve(term.right)
                if (
                    left is None
                    or other is None
                    or left[0] is other[0]
                    or left[1] == other[1]
                ):
                    valid = False
                    break
                if left[0] is right:
                    left, other = other, left
                if other[0] is not right or left[0] not in previous[:-1]:
                    valid = False
                    break
                groups[(id(left[0]), id(other[0]), left[1], other[1])].append(
                    (left[2], other[2])
                )
            # Never publish ON alone when another predicate supplies additional
            # constraints on that same source pair outside the explicit key.
            if valid and where is not None:
                candidate_sources = {frozenset(key[:2]) for key in groups}
                for predicate in _and_terms(where.this):
                    owners = {
                        id(resolved[0])
                        for col in predicate.find_all(expressions.Column)
                        if (resolved := resolve(col)) is not None
                    }
                    if any(pair <= owners for pair in candidate_sources):
                        valid = False
                        break
            if not valid:
                skip(join, "complex_or_incomplete_key")
                continue
            for (_, _, left_table, right_table), pairs in groups.items():
                edges.append(
                    gold_join_edge(
                        from_table=left_table,
                        to_table=right_table,
                        column_pairs=pairs,
                        source_id=source_id,
                    )
                )
    return merge_join_edges([], edges)


def _and_terms(expression: expressions.Expression) -> list[expressions.Expression]:
    if isinstance(expression, expressions.Paren):
        return _and_terms(expression.this)
    if isinstance(expression, expressions.And):
        return [*_and_terms(expression.left), *_and_terms(expression.right)]
    return [expression]


def pure_routing_paths(
    *,
    sql: str,
    dialect: str,
    case_edges: Iterable[GraphEdge],
) -> list[dict[str, Any]]:
    """Find one-table bridges with no independent value use in simple Gold SQL."""

    tree = sqlglot.parse_one(sql, dialect=dialect or None)
    if tree.find(expressions.CTE):
        return []
    if any(
        star.find_ancestor(expressions.Count) is None
        for star in tree.find_all(expressions.Star)
    ):
        return []
    source = tree.args.get("from_")
    scope_tables = [
        table
        for table in (
            source.this if source is not None else None,
            *(join.this for join in tree.args.get("joins") or []),
        )
        if isinstance(table, expressions.Table)
    ]
    aliases = {table.alias_or_name: table.name for table in scope_tables}
    if len(aliases) != len(scope_tables) or len(set(aliases.values())) != len(aliases):
        return []
    used_tables: set[str] = set()
    for column in tree.find_all(expressions.Column):
        if (
            column.find_ancestor(expressions.Select) is tree
            and column.find_ancestor(expressions.Join) is None
        ):
            table = aliases.get(column.table)
            if table:
                used_tables.add(table)
    by_table: dict[str, list[GraphEdge]] = {}
    for edge in case_edges:
        from_table = edge.from_node_id.removeprefix("table.")
        to_table = edge.to_node_id.removeprefix("table.")
        by_table.setdefault(from_table, []).append(edge)
        by_table.setdefault(to_table, []).append(edge)
    # A terminal joined table can constrain the population even when it only
    # contributes a join key (for example in COUNT(*)). It is not a bridge.
    used_tables.update(table for table, edges in by_table.items() if len(edges) == 1)
    result: list[dict[str, Any]] = []
    for route_table, edges in sorted(by_table.items()):
        if route_table in used_tables or len(edges) != 2:
            continue
        endpoints = sorted(
            {
                endpoint
                for edge in edges
                for endpoint in (
                    edge.from_node_id.removeprefix("table."),
                    edge.to_node_id.removeprefix("table."),
                )
                if endpoint != route_table
            }
        )
        if len(endpoints) != 2 or not set(endpoints) <= used_tables:
            continue
        result.append(
            {
                "semantic_tables": [f"table.{name}" for name in endpoints],
                "routing_table": f"table.{route_table}",
                "edge_ids": [
                    edge.id for edge in sorted(edges, key=lambda item: item.id)
                ],
            }
        )
    return result
