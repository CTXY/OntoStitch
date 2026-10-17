from __future__ import annotations

import re
from collections import deque
from typing import Any

from nl2sql_baseline.database import Database, SchemaSnapshot
from nl2sql_baseline.models import SchemaSelection


def select_schema(
    snapshot: SchemaSnapshot,
    database: Database,
    *,
    question: str,
    evidence: str,
    mode: str,
    table_top_k: int,
    column_top_k: int,
    value_top_k: int,
    values_per_column: int,
) -> SchemaSelection:
    if mode == "full_schema":
        return SchemaSelection(
            ddl=snapshot.ddl,
            tables={
                table["name"]: [column["name"] for column in table["columns"]]
                for table in snapshot.catalog["tables"]
            },
            value_matches=[],
            mode=mode,
        )
    if mode != "simple":
        raise ValueError(f"unsupported schema linking mode: {mode}")

    context = "\n".join(part for part in (question, evidence) if part)
    column_scores: list[tuple[float, str, str]] = []
    table_scores: dict[str, float] = {}
    for table in snapshot.catalog["tables"]:
        table_name = str(table["name"])
        table_score = _text_score(context, table_name)
        table_scores[table_name] = table_score
        for column in table["columns"]:
            column_name = str(column["name"])
            metadata = " ".join(
                str(column.get(key) or "")
                for key in ("expanded_name", "description", "value_description", "comment")
            )
            score = 2.0 * _text_score(context, column_name) + _text_score(context, metadata)
            score += 0.25 * table_score
            column_scores.append((score, table_name, column_name))

    scan_limit = max(column_top_k * 2, 40)
    value_matches = _find_value_matches(
        database,
        snapshot,
        context=context,
        candidates=sorted(column_scores, reverse=True)[:scan_limit],
        values_per_column=values_per_column,
        value_top_k=value_top_k,
    )
    value_bonus = {
        (str(match["table"]), str(match["column"])): float(match["score"])
        for match in value_matches
    }
    ranked_columns = sorted(
        (
            score + 3.0 * value_bonus.get((table, column), 0.0),
            table,
            column,
        )
        for score, table, column in column_scores
    )
    ranked_columns.reverse()

    positive = [item for item in ranked_columns if item[0] >= 0.35]
    chosen_columns = (positive or ranked_columns)[:column_top_k]
    chosen_table_scores: dict[str, float] = {}
    for score, table, _ in chosen_columns:
        chosen_table_scores[table] = max(chosen_table_scores.get(table, 0.0), score)
    chosen_tables = {
        table
        for table, _ in sorted(
            chosen_table_scores.items(), key=lambda item: (-item[1], item[0])
        )[:table_top_k]
    }
    selected: dict[str, set[str]] = {table: set() for table in chosen_tables}
    for _, table, column in chosen_columns:
        if table in selected:
            selected[table].add(column)

    _add_key_columns(snapshot.catalog, selected)
    _add_foreign_key_paths(snapshot.catalog, selected)
    ordered = {
        table["name"]: [
            column["name"]
            for column in table["columns"]
            if column["name"] in selected.get(table["name"], set())
        ]
        for table in snapshot.catalog["tables"]
        if table["name"] in selected
    }
    return SchemaSelection(
        ddl=_render_schema(snapshot.catalog, ordered, value_matches),
        tables=ordered,
        value_matches=value_matches,
        mode=mode,
    )


def _find_value_matches(
    database: Database,
    snapshot: SchemaSnapshot,
    *,
    context: str,
    candidates: list[tuple[float, str, str]],
    values_per_column: int,
    value_top_k: int,
) -> list[dict[str, Any]]:
    if value_top_k < 1 or values_per_column < 1:
        return []
    context_normalized = _compact(context)
    matches: list[dict[str, Any]] = []
    catalog_columns = {
        (table["name"], column["name"]): column
        for table in snapshot.catalog["tables"]
        for column in table["columns"]
    }
    for _, table, column in candidates:
        column_type = str(catalog_columns[(table, column)].get("type") or "").casefold()
        if any(marker in column_type for marker in ("blob", "binary")):
            continue
        try:
            values = database.sample_distinct_values(table, column, limit=values_per_column)
        except Exception:  # noqa: BLE001 - one unsupported column must not disable linking.
            continue
        for value in values:
            normalized = _compact(value)
            if len(normalized) < 2:
                continue
            exact = (
                normalized in context_normalized
                if len(normalized) > 3
                else normalized in _tokens(context)
            )
            score = 1.0 if exact else 0.0
            if score <= 0:
                continue
            matches.append(
                {"table": table, "column": column, "value": value, "score": round(score, 6)}
            )
    matches.sort(key=lambda item: (-float(item["score"]), str(item["table"]), str(item["column"])))
    return matches[:value_top_k]


def _add_key_columns(catalog: dict[str, Any], selected: dict[str, set[str]]) -> None:
    for table in catalog["tables"]:
        name = table["name"]
        if name not in selected:
            continue
        for column in table["columns"]:
            if column.get("primary_key"):
                selected[name].add(column["name"])
        for foreign_key in table.get("foreign_keys", []):
            if foreign_key["referred_table"] in selected:
                selected[name].add(foreign_key["column"])
                selected[foreign_key["referred_table"]].add(foreign_key["referred_column"])


def _add_foreign_key_paths(catalog: dict[str, Any], selected: dict[str, set[str]]) -> None:
    adjacency: dict[str, list[tuple[str, str, str]]] = {}
    for table in catalog["tables"]:
        source = table["name"]
        for foreign_key in table.get("foreign_keys", []):
            target = foreign_key["referred_table"]
            source_column = foreign_key["column"]
            target_column = foreign_key["referred_column"]
            adjacency.setdefault(source, []).append((target, source_column, target_column))
            adjacency.setdefault(target, []).append((source, target_column, source_column))

    anchors = sorted(selected)
    if len(anchors) < 2:
        return
    connected = {anchors[0]}
    for target in anchors[1:]:
        path = _shortest_path(adjacency, connected, target)
        if not path:
            connected.add(target)
            continue
        for source, source_column, destination, destination_column in path:
            selected.setdefault(source, set()).add(source_column)
            selected.setdefault(destination, set()).add(destination_column)
            connected.update((source, destination))


def _shortest_path(
    adjacency: dict[str, list[tuple[str, str, str]]], starts: set[str], target: str
) -> list[tuple[str, str, str, str]]:
    queue = deque((start, []) for start in sorted(starts))
    visited = set(starts)
    while queue:
        node, path = queue.popleft()
        if node == target:
            return path
        for neighbor, source_column, target_column in adjacency.get(node, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append(
                (
                    neighbor,
                    [*path, (node, source_column, neighbor, target_column)],
                )
            )
    return []


def _render_schema(
    catalog: dict[str, Any],
    selected: dict[str, list[str]],
    value_matches: list[dict[str, Any]],
) -> str:
    examples: dict[tuple[str, str], list[str]] = {}
    for match in value_matches:
        examples.setdefault((match["table"], match["column"]), []).append(str(match["value"]))
    blocks: list[str] = []
    for table in catalog["tables"]:
        table_name = table["name"]
        if table_name not in selected:
            continue
        lines = [f"CREATE TABLE {_quote(table_name)} ("]
        definitions: list[str] = []
        selected_columns = set(selected[table_name])
        for column in table["columns"]:
            if column["name"] not in selected_columns:
                continue
            definition = f"  {_quote(column['name'])} {column.get('type') or ''}".rstrip()
            annotations = [
                _annotation(str(column.get(key) or ""))
                for key in ("expanded_name", "description", "value_description")
                if str(column.get(key) or "").strip()
            ]
            column_examples = examples.get((table_name, column["name"]), [])
            if column_examples:
                annotations.append(f"matched values: {column_examples}")
            if annotations:
                definition += " /* " + " | ".join(annotations) + " */"
            definitions.append(definition)
        for foreign_key in table.get("foreign_keys", []):
            if (
                foreign_key["column"] in selected_columns
                and foreign_key["referred_table"] in selected
            ):
                definitions.append(
                    f"  FOREIGN KEY ({_quote(foreign_key['column'])}) REFERENCES "
                    f"{_quote(foreign_key['referred_table'])} "
                    f"({_quote(foreign_key['referred_column'])})"
                )
        lines.append(",\n".join(definitions))
        lines.append(");")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _text_score(query: str, candidate: str) -> float:
    query_tokens = _tokens(query)
    candidate_tokens = _tokens(candidate)
    if not query_tokens or not candidate_tokens:
        return 0.0
    overlap = query_tokens & candidate_tokens
    score = len(overlap) / len(candidate_tokens)
    compact_candidate = _compact(candidate)
    if len(compact_candidate) >= 2 and compact_candidate in _compact(query):
        score += 1.0
    return score


def _tokens(value: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value).casefold()
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", expanded.replace("_", " "))
    return {word for word in words if len(word) > 1 or "\u3400" <= word <= "\u9fff"}


def _compact(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9\u3400-\u9fff]+", value.casefold()))


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _annotation(value: str, limit: int = 240) -> str:
    compact = " ".join(value.split()).replace("/*", "/ *").replace("*/", "* /")
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"
