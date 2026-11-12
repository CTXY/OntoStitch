"""AST-based links between a SQL revision and actual ontology node identifiers."""

from __future__ import annotations

import hashlib

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


def ground_sql(sql: str, graph: dict, declarations: list[dict] | None = None) -> dict:
    tree = sqlglot.parse_one(sql, read="sqlite")
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    tables = {
        key: n.get("properties", {}).get("table_name")
        for key, n in nodes.items()
        if n["node_type"] == "table"
    }
    columns = {
        key: (
            tables.get(n.get("properties", {}).get("table_ref")),
            n.get("properties", {}).get("column_name"),
        )
        for key, n in nodes.items()
        if n["node_type"] == "column"
    }
    references = {}
    for key, node in nodes.items():
        refs = set()

        def visit(value, key=key, refs=refs):
            if isinstance(value, str) and value in nodes and value != key:
                refs.add(value)
            elif isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    visit(child)

        visit(node.get("properties", {}))
        references[key] = refs
    bindings = []
    seen = set()
    # Scope resolution prevents assigning identically named fields in unrelated subqueries.
    for scope in traverse_scope(tree):
        sources = {
            alias.lower(): source.name.lower()
            for alias, source in scope.sources.items()
            if isinstance(source, exp.Table)
        }
        for col in scope.columns:
            table = sources.get(col.table.lower()) if col.table else None
            if col.table and table is None:
                continue  # Derived tables require lineage analysis; don't invent a link.
            matches = [
                key
                for key, (tbl, name) in columns.items()
                if name
                and name.lower() == col.name.lower()
                and (
                    str(tbl).lower() == table
                    if table
                    else str(tbl).lower() in sources.values()
                )
            ]
            if len(matches) != 1:
                continue
            column_id = matches[0]
            position = col.this.meta
            if "start" not in position or "end" not in position:
                continue
            # sqlglot indexes are Unicode code points; browsers use UTF-16 offsets.
            start, end = position["start"], position["end"] + 1
            a = len(sql[:start].encode("utf-16-le")) // 2
            b = len(sql[:end].encode("utf-16-le")) // 2
            key = (column_id, a, b)
            if key in seen:
                continue
            seen.add(key)
            linked = [column_id] + [
                key for key, refs in references.items() if column_id in refs
            ]
            ancestor = col
            while (
                ancestor.parent is not None
                and not isinstance(ancestor, (exp.Predicate, exp.AggFunc))
                and not isinstance(
                    ancestor.parent,
                    (exp.Select, exp.Where, exp.Join, exp.Having, exp.Group, exp.Order),
                )
            ):
                ancestor = ancestor.parent
            container = ancestor.find_ancestor(
                exp.Where, exp.Join, exp.Having, exp.Group, exp.Order, exp.Select
            )
            clause = container.key if container is not None else "expression"
            bindings.append(
                {
                    "binding_id": f"binding_{len(bindings) + 1}",
                    "node_ids": linked,
                    "column_node_id": column_id,
                    "start": a,
                    "end": b,
                    "clause": clause,
                    "expression": ancestor.sql(dialect="sqlite"),
                    "match_status": "field_reference",
                    "explanation": "AST-resolved field reference; related ontology rules are evidence, not proof of semantic correctness.",
                }
            )
    declared = []
    for item in declarations or []:
        valid_ids = [key for key in item.get("node_ids", []) if key in nodes]
        if valid_ids:
            declared.append(
                {
                    "node_ids": valid_ids,
                    "explanation": str(item.get("explanation", ""))[:2000],
                    "match_status": "agent_declared",
                }
            )
    return {
        "version": 3,
        "sql_hash": hashlib.sha256(sql.encode()).hexdigest(),
        "bindings": bindings,
        "declarations": declared,
    }


def graph_schema_issues(graph: dict, schema: list[dict]) -> list[str]:
    actual = {t["name"]: {c["name"] for c in t["columns"]} for t in schema}
    tables = {
        n["id"]: n.get("properties", {}).get("table_name")
        for n in graph.get("nodes", [])
        if n["node_type"] == "table"
    }
    issues = []
    for name in tables.values():
        if name not in actual:
            issues.append(f"Ontology table does not exist in this database: {name}")
    for node in graph.get("nodes", []):
        if node["node_type"] == "column":
            props = node.get("properties", {})
            table, col = tables.get(props.get("table_ref")), props.get("column_name")
            if table not in actual or col not in actual.get(table, set()):
                issues.append(f"Ontology column does not exist: {table}.{col}")
    return issues
