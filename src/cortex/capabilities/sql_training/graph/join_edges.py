from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

import sqlglot
from sqlglot import expressions

from cortex.schema import GraphEdge, SourceReference

_JOIN_EDGE_PROPERTY_KEYS = frozenset({"default_join_type", "cardinality", "conditions"})


def join_edge_key(edge: GraphEdge) -> tuple[tuple[str, str], tuple[tuple[str, str, str], ...]]:
    """Identify one physical relation independently of alias, direction, or case."""

    if edge.edge_type != "joins_with":
        raise ValueError("join edge identity requires joins_with")
    conditions = edge.properties.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise ValueError("join edge requires complete conditions")
    normalized: list[tuple[str, str, str]] = []
    for condition in conditions:
        if not isinstance(condition, dict):
            raise ValueError("join condition must be an object")
        left = condition.get("left_column_ref")
        right = condition.get("right_column_ref")
        operator = condition.get("operator", "=")
        if not isinstance(left, str) or not isinstance(right, str) or operator != "=":
            raise ValueError("join condition must be a column equality")
        normalized.append((*sorted((left, right)), "="))
    return tuple(sorted((edge.from_node_id, edge.to_node_id))), tuple(sorted(normalized))


def merge_join_edges(
    existing: Iterable[GraphEdge], incoming: Iterable[GraphEdge]
) -> list[GraphEdge]:
    """Keep catalog IDs for equivalent Gold joins and union their provenance."""

    by_key: dict[tuple, GraphEdge] = {}
    used_ids: set[str] = set()
    for edge in (_public_join_edge(edge) for edge in (*tuple(existing), *tuple(incoming))):
        key = join_edge_key(edge)
        current = by_key.get(key)
        if current is not None:
            sources = tuple(
                {
                    (source.source_id, source.locator, source.case_id, source.chunk_id): source
                    for source in (*current.sources, *edge.sources)
                }.values()
            )
            by_key[key] = current.model_copy(update={"sources": sources})
            continue
        if edge.id in used_ids:
            digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
            edge = edge.model_copy(update={"id": f"edge_{digest}"})
        by_key[key] = edge
        used_ids.add(edge.id)
    return list(by_key.values())


def gold_join_edge(
    *,
    from_table: str,
    to_table: str,
    column_pairs: Iterable[tuple[str, str]],
    source_id: str,
    join_type: str = "INNER",
    sql_construct_type: str | None = None,
    evidence_fragment: str | None = None,
) -> GraphEdge:
    conditions = [
        {
            "left_column_ref": f"column.{from_table}.{left}",
            "right_column_ref": f"column.{to_table}.{right}",
            "operator": "=",
        }
        for left, right in column_pairs
    ]
    properties: dict[str, Any] = {
        "default_join_type": join_type,
        "conditions": conditions,
    }
    edge = GraphEdge(
        id="pending",
        edge_type="joins_with",
        from_node_id=f"table.{from_table}",
        to_node_id=f"table.{to_table}",
        properties=properties,
        sources=(SourceReference(source_id=source_id, locator="qa", case_id=source_id),),
    )
    digest = hashlib.sha256(json.dumps(join_edge_key(edge)).encode()).hexdigest()[:24]
    return edge.model_copy(update={"id": f"edge_{digest}"})


def _public_join_edge(edge: GraphEdge) -> GraphEdge:
    """Return the schema-public physical edge shape used in GraphSpace.

    Gold extraction may produce training evidence such as SQL construct kind or
    SQL fragments. That evidence is trace data; the public `joins_with` edge is
    the reusable physical relation and must stay inside the root schema.
    """

    if edge.edge_type != "joins_with":
        return edge
    properties = {
        key: value
        for key, value in edge.properties.items()
        if key in _JOIN_EDGE_PROPERTY_KEYS
    }
    if properties == edge.properties:
        return edge
    return edge.model_copy(update={"properties": properties})


def build_gold_join_edges_from_llm_proposals(
    *,
    proposals: Iterable[dict[str, Any]],
    source_id: str,
    table_columns: dict[str, set[str]],
) -> list[GraphEdge]:
    """Project LLM-proposed Gold relationships after catalog-level safety checks."""

    edges: list[GraphEdge] = []
    canonical = _canonical_table_columns(table_columns)
    for proposal in proposals:
        from_table = canonical.get(str(proposal.get("from_table") or "").lower())
        to_table = canonical.get(str(proposal.get("to_table") or "").lower())
        raw_pairs = proposal.get("column_pairs")
        if (
            from_table is None
            or to_table is None
            or from_table == to_table
            or not isinstance(raw_pairs, list)
            or not raw_pairs
        ):
            continue
        pairs: list[tuple[str, str]] = []
        valid = True
        for pair in raw_pairs:
            if not isinstance(pair, dict):
                valid = False
                break
            left = canonical.get(f"{from_table}.{str(pair.get('from_column') or '').lower()}")
            right = canonical.get(f"{to_table}.{str(pair.get('to_column') or '').lower()}")
            if left is None or right is None:
                valid = False
                break
            pairs.append((left, right))
        join_type = str(proposal.get("join_type") or "INNER").upper()
        if not valid or join_type not in {"INNER", "LEFT", "RIGHT", "FULL"}:
            continue
        edges.append(
            gold_join_edge(
                from_table=from_table,
                to_table=to_table,
                column_pairs=pairs,
                source_id=source_id,
                join_type=join_type,
                sql_construct_type=_optional_text(proposal.get("sql_construct_type")),
                evidence_fragment=_optional_text(proposal.get("evidence_fragment")),
            )
        )
    return edges


def _canonical_table_columns(table_columns: dict[str, set[str]]) -> dict[str, str]:
    canonical: dict[str, str] = {}
    for table, columns in table_columns.items():
        table_name = str(table)
        canonical[table_name.lower()] = table_name
        for column in columns:
            column_name = str(column)
            canonical[f"{table_name}.{column_name.lower()}"] = column_name
    return canonical


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _and_terms(expression: expressions.Expression) -> list[expressions.Expression]:
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
        star.find_ancestor(expressions.Count) is None for star in tree.find_all(expressions.Star)
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
                "edge_ids": [edge.id for edge in sorted(edges, key=lambda item: item.id)],
            }
        )
    return result
