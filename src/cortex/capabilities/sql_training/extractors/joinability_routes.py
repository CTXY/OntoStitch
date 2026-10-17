from __future__ import annotations

from collections import defaultdict
from itertools import combinations

import sqlglot
from sqlglot import expressions


def exact_gold_join_fragments(sql: str, *, dialect: str | None = None) -> list[str]:
    """Return JOIN clauses that occur verbatim in Gold SQL."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except (sqlglot.errors.ParseError, ValueError):
        return []
    if tree is None:
        return []
    fragments: list[str] = []
    for select in tree.find_all(expressions.Select):
        for join in select.args.get("joins") or []:
            fragment = join.sql(dialect=dialect or None)
            if fragment in sql and fragment not in fragments:
                fragments.append(fragment)
    return fragments


def pure_routing_reference_sets(sql: str, *, dialect: str | None = None) -> set[frozenset[str]]:
    """Return Gold-proved two-hop routes whose middle table has no business use."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except (sqlglot.errors.ParseError, ValueError):
        return set()
    if tree is None or any(
        star.find_ancestor(expressions.Count) is None for star in tree.find_all(expressions.Star)
    ):
        return set()
    routes = set()
    for select in tree.find_all(expressions.Select):
        routes.update(_scope_routes(select))
    return routes


def gold_join_column_reference_sets(
    sql: str, *, dialect: str | None = None
) -> set[frozenset[str]]:
    """Return complete ON-column sets for direct joins and pure two-hop routes in Gold SQL."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except (sqlglot.errors.ParseError, ValueError):
        return set()
    if tree is None:
        return set()
    paths: set[frozenset[str]] = set()
    for select in tree.find_all(expressions.Select):
        paths.update(_scope_direct_join_column_sets(select))
        paths.update(
            frozenset(ref for ref in route if ref.startswith("column."))
            for route in _scope_routes(select)
        )
    return {path for path in paths if path}


def _scope_direct_join_column_sets(select: expressions.Select) -> set[frozenset[str]]:
    source = select.args.get("from_")
    scope_tables = [
        table
        for table in (
            source.this if source is not None else None,
            *(join.this for join in select.args.get("joins") or []),
        )
        if isinstance(table, expressions.Table)
    ]
    aliases = {table.alias_or_name: table.name for table in scope_tables}
    if len(aliases) != len(scope_tables):
        return set()
    paths: set[frozenset[str]] = set()
    for join in select.args.get("joins") or []:
        if not isinstance(join.this, expressions.Table) or not (on := join.args.get("on")):
            continue
        if on.find(expressions.Or):
            continue
        right = join.this.alias_or_name
        refs: set[str] = set()
        valid = True
        for equality in _and_terms(on):
            if not isinstance(equality, expressions.EQ):
                valid = False
                break
            left_col, right_col = equality.left, equality.right
            if not isinstance(left_col, expressions.Column) or not isinstance(
                right_col, expressions.Column
            ):
                valid = False
                break
            if right not in {left_col.table, right_col.table}:
                valid = False
                break
            if left_col.table not in aliases or right_col.table not in aliases:
                valid = False
                break
            refs.add(f"column.{aliases[left_col.table]}.{left_col.name}")
            refs.add(f"column.{aliases[right_col.table]}.{right_col.name}")
        if valid and refs:
            paths.add(frozenset(refs))
    return paths


def _scope_routes(select: expressions.Select) -> set[frozenset[str]]:
    source = select.args.get("from_")
    scope_tables = [
        table
        for table in (
            source.this if source is not None else None,
            *(join.this for join in select.args.get("joins") or []),
        )
        if isinstance(table, expressions.Table)
    ]
    aliases = {table.alias_or_name: table.name for table in scope_tables}
    if len(aliases) != len(scope_tables):
        return set()
    business_aliases = {
        column.table
        for column in select.find_all(expressions.Column)
        if column.find_ancestor(expressions.Select) is select
        and column.table in aliases
        and column.find_ancestor(expressions.Join) is None
    }
    incident: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
    for join in select.args.get("joins") or []:
        if not isinstance(join.this, expressions.Table) or not (on := join.args.get("on")):
            continue
        if on.find(expressions.Or):
            continue
        right = join.this.alias_or_name
        pairs = []
        for equality in _and_terms(on):
            if not isinstance(equality, expressions.EQ):
                pairs = []
                break
            left_col, right_col = equality.left, equality.right
            if not isinstance(left_col, expressions.Column) or not isinstance(
                right_col, expressions.Column
            ):
                pairs = []
                break
            if right not in {left_col.table, right_col.table}:
                pairs = []
                break
            pairs.append((left_col, right_col))
        if not pairs:
            continue
        others = {column.table for pair in pairs for column in pair if column.table != right}
        if len(others) != 1:
            continue
        left = next(iter(others))
        if left not in aliases or right not in aliases:
            continue
        refs = {
            f"column.{aliases[column.table]}.{column.name}" for pair in pairs for column in pair
        }
        incident[left].append((right, refs))
        incident[right].append((left, refs))
    routes = set()
    for middle, links in incident.items():
        if middle in business_aliases:
            continue
        for (left, first), (right, second) in combinations(links, 2):
            if left == right or left == middle or right == middle:
                continue
            routes.add(
                frozenset(
                    {
                        f"table.{aliases[left]}",
                        f"table.{aliases[middle]}",
                        f"table.{aliases[right]}",
                        *first,
                        *second,
                    }
                )
            )
    return routes


def _and_terms(expression: expressions.Expression) -> list[expressions.Expression]:
    if isinstance(expression, expressions.And):
        return [*_and_terms(expression.left), *_and_terms(expression.right)]
    return [expression]
