from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Any

import sqlglot
from sqlglot import expressions

from cortex.schema.graph import GraphEdge, GraphSpace
from cortex.schema.relationships import is_dependency_edge

JOINABILITY_INTENT = "joinability"


def semantic_tables_from_sql(
    sql: str | None, graph: GraphSpace, *, dialect: str | None = None
) -> set[str]:
    """Identify table sources used outside join predicates, retaining terminal sources."""
    if not sql:
        return set()
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.ParseError, ValueError):
        return set()
    table_ids = {
        str(node.properties.get("table_name", "")).casefold(): node.id
        for node in graph.nodes
        if node.node_type == "table"
    }
    aliases = {
        table.alias_or_name: table.name for table in tree.find_all(expressions.Table)
    }
    used = {
        table_ids[aliases[column.table].casefold()]
        for column in tree.find_all(expressions.Column)
        if column.find_ancestor(expressions.Join) is None
        and column.table in aliases
        and aliases[column.table].casefold() in table_ids
    }
    if any(
        star.find_ancestor(expressions.Count) is None
        for star in tree.find_all(expressions.Star)
    ):
        return {
            table_ids[name.casefold()]
            for name in aliases.values()
            if name.casefold() in table_ids
        }
    degree: dict[str, int] = defaultdict(int)
    for join in tree.find_all(expressions.Join):
        if not isinstance(join.this, expressions.Table) or not join.args.get("on"):
            continue
        right = join.this.alias_or_name
        neighbors = {
            column.table
            for column in join.args["on"].find_all(expressions.Column)
            if column.table != right and column.table in aliases
        }
        if len(neighbors) == 1:
            neighbor = next(iter(neighbors))
            degree[right] += 1
            degree[neighbor] += 1
    used.update(
        table_ids[aliases[alias].casefold()]
        for alias, count in degree.items()
        if count == 1 and aliases[alias].casefold() in table_ids
    )
    return used


def join_path_candidates(
    graph: GraphSpace, semantic_tables: set[str]
) -> list[dict[str, Any]]:
    """Enumerate complete simple paths of at most two join edges."""
    if len(semantic_tables) < 2:
        return []
    node_by_id = {node.id: node for node in graph.nodes}
    joins = [
        edge
        for edge in graph.edges
        if edge.edge_type == "joins_with" and _complete_join_edge(edge, node_by_id)
    ]
    adjacency: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in joins:
        adjacency[edge.from_node_id].append(edge)
        adjacency[edge.to_node_id].append(edge)
    rules = [
        node
        for node in graph.nodes
        if node.node_type == "generation_rule"
        and node.merge_status != "conflict"
        and JOINABILITY_INTENT in node.properties.get("operation_intents", [])
    ]
    column_ids = {node.id for node in graph.nodes if node.node_type == "column"}
    rule_column_refs = {
        rule.id: {
            edge.to_node_id
            for edge in graph.edges
            if edge.from_node_id == rule.id
            and is_dependency_edge(edge)
            and edge.to_node_id in column_ids
        }
        for rule in rules
    }
    candidate_paths: list[tuple[str, str, tuple[GraphEdge, ...]]] = []

    def collect_path(left: str, right: str, edges: tuple[GraphEdge, ...]) -> None:
        candidate_paths.append((left, right, edges))

    candidates: list[dict[str, Any]] = []
    for left, right in combinations(sorted(semantic_tables), 2):

        def walk(
            current: str,
            path: tuple[GraphEdge, ...],
            visited: frozenset[str],
            left: str = left,
            right: str = right,
        ) -> None:
            if len(path) >= 2:
                return
            for edge in adjacency[current]:
                next_table = _other(edge, current)
                if next_table in visited:
                    continue
                next_path = (*path, edge)
                if next_table == right:
                    collect_path(left, right, next_path)
                    continue
                walk(next_table, next_path, visited | {next_table})

        walk(left, (), frozenset({left}))
    path_rule_ids = {
        (left, right, tuple(edge.id for edge in edges)): _supporting_rule_ids(
            edges, rule_column_refs
        )
        for left, right, edges in candidate_paths
    }
    rule_path_counts: dict[str, int] = defaultdict(int)
    for rule_ids in path_rule_ids.values():
        for rule_id in rule_ids:
            rule_path_counts[rule_id] += 1
    for left, right, edges in candidate_paths:
        rule_ids = [
            rule_id
            for rule_id in path_rule_ids[
                (left, right, tuple(edge.id for edge in edges))
            ]
            if rule_path_counts[rule_id] == 1
        ]
        if rule_ids:
            candidates.extend(
                _path(left, right, edges, rule_id) for rule_id in rule_ids
            )
        else:
            candidates.append(_path(left, right, edges, None))
    unique: dict[tuple, dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            tuple(candidate["semantic_tables"]),
            tuple(candidate["edge_ids"]),
            candidate["joinability_rule_id"],
        )
        unique.setdefault(key, candidate)
    return list(unique.values())


def _supporting_rule_ids(
    edges: tuple[GraphEdge, ...], rule_column_refs: dict[str, set[str]]
) -> list[str]:
    """Match a rule to the full path or any contiguous routed subpath."""
    supported = set()
    for start in range(len(edges)):
        for stop in range(start + 1, len(edges) + 1):
            if len(edges) > 1 and stop - start == 1:
                continue
            columns = _join_columns(*edges[start:stop])
            supported.update(
                rule_id
                for rule_id, references in rule_column_refs.items()
                if references == columns
            )
    return sorted(supported)


def _other(edge: GraphEdge, endpoint: str) -> str:
    return edge.to_node_id if edge.from_node_id == endpoint else edge.from_node_id


def _complete_join_edge(edge: GraphEdge, nodes: dict) -> bool:
    pairs = edge.properties.get("column_pairs")
    if not isinstance(pairs, list) or not pairs:
        return False
    for pair in pairs:
        if not isinstance(pair, dict) or set(pair) != {
            "from_column_ref",
            "to_column_ref",
        }:
            return False
        for key, owner in (
            ("from_column_ref", edge.from_node_id),
            ("to_column_ref", edge.to_node_id),
        ):
            reference = pair[key]
            column = nodes.get(reference) if isinstance(reference, str) else None
            if (
                column is None
                or column.node_type != "column"
                or column.properties.get("table_ref") != owner
            ):
                return False
    return True


def _join_columns(*edges: GraphEdge) -> set[str]:
    return {
        str(condition[key])
        for edge in edges
        for condition in edge.properties.get("column_pairs", [])
        for key in ("from_column_ref", "to_column_ref")
    }


def _path(
    left: str, right: str, edges: tuple[GraphEdge, ...], rule_id: str | None
) -> dict[str, Any]:
    current = left
    joins = []
    for edge in edges:
        reversed_edge = edge.to_node_id == current
        next_table = edge.from_node_id if reversed_edge else edge.to_node_id
        cardinality = edge.properties.get("cardinality")
        if cardinality and reversed_edge:
            cardinality = {"1:1": "1:1", "1:N": "N:1", "N:1": "1:N"}[cardinality]
        joins.append(
            {
                "from_table": current,
                "to_table": next_table,
                "column_pairs": [
                    {
                        "from_column_ref": pair[
                            "to_column_ref" if reversed_edge else "from_column_ref"
                        ],
                        "to_column_ref": pair[
                            "from_column_ref" if reversed_edge else "to_column_ref"
                        ],
                    }
                    for pair in edge.properties["column_pairs"]
                ],
                **({"cardinality": cardinality} if cardinality else {}),
            }
        )
        current = next_table
    return {
        "semantic_tables": [left, right],
        "kind": "routed" if len(edges) > 1 else "direct",
        "edge_ids": [edge.id for edge in edges],
        "joinability_rule_id": rule_id,
        "joins": joins,
    }
