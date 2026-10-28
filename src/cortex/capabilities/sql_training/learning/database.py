"""Bounded investigation, restricted to the frozen database catalog."""

from __future__ import annotations

import sqlglot
from sqlglot import expressions as exp
from sqlglot.optimizer.scope import traverse_scope

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.learning.session import LearningSession
from cortex.infrastructure.database.execution import execute_query


class GroundingDatabase:
    """Reuse investigation scope, timeout and budget for edit-internal Value checks."""

    def __init__(self, session: LearningSession):
        self.session = session

    def get_dialect(self) -> str:
        return self.session.dialect

    def find_exact_column_values(self, table_name, column_name, value):
        assets = self.session.graph.working.physical_assets
        target = next(
            (a for a in assets if a.id == f"column.{table_name}.{column_name}"), None
        )
        if target is not None:
            owner = next(
                (
                    ref
                    for ref in target.properties.references
                    if ref.startswith("table.")
                ),
                None,
            )
            physical_table = next((a for a in assets if a.id == owner), None)
            if physical_table is not None:
                table_name = physical_table.properties.table_name
                column_name = target.properties.column_name
        result = investigate_database(
            self.session,
            mode="exact",
            table=table_name,
            column=column_name,
            value=str(value),
            sql="",
            limit=2,
        )
        if "rows" not in result:
            result = self.session.evidence.read(result["evidence_ref"])["payload"]
        return [row[0] for row in result["rows"]]


def investigate_database(
    session: LearningSession,
    *,
    mode: str,
    table: str,
    column: str,
    value: str,
    sql: str,
    limit: int,
) -> dict:
    if not 1 <= limit <= 100:
        raise ValueError("database result limit must be between 1 and 100")
    assets = session.graph.working.physical_assets
    tables = {a.properties.table_name for a in assets if a.node_type == NodeType.TABLE}
    table_names = {
        a.id: a.properties.table_name for a in assets if a.node_type == NodeType.TABLE
    }
    columns = {
        (table_names.get(ref), a.properties.column_name)
        for a in assets
        if a.node_type == NodeType.COLUMN
        for ref in a.properties.references
        if ref.startswith("table.")
    }
    if mode != "query" and table not in tables:
        raise ValueError("table is outside the configured catalog")
    if column and (table, column) not in columns:
        raise ValueError("column does not exist in the configured catalog")
    if mode in {"exact", "statistics"} and not column:
        raise ValueError("column is required")
    if mode == "metadata":
        owner_ids = {key for key, name in table_names.items() if name == table}
        result = [
            a.model_dump(mode="json")
            for a in assets
            if a.id in owner_ids
            or (
                a.node_type == NodeType.COLUMN
                and owner_ids.intersection(a.properties.references)
            )
        ]
        ref = session.evidence.put("database_metadata", result)
        return session.summarize(
            {"status": "ok", "evidence_ref": ref, "result": result}, evidence_id=ref
        )
    dialect = session.dialect
    qtable = exp.Table(this=exp.to_identifier(table, quoted=True)).sql(dialect=dialect)
    qcolumn = exp.Column(this=exp.to_identifier(column, quoted=True)).sql(
        dialect=dialect
    )
    if mode == "sample":
        sql = f"SELECT * FROM {qtable} LIMIT {limit}"
    elif mode == "exact":
        literal = exp.Literal.string(value).sql(dialect=dialect)
        sql = f"SELECT DISTINCT {qcolumn} AS value FROM {qtable} WHERE {qcolumn} = {literal} LIMIT 2"
    elif mode == "statistics":
        sql = f"SELECT COUNT({qcolumn}) AS non_null, COUNT(DISTINCT {qcolumn}) AS distinct_count FROM {qtable}"
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError(f"invalid investigation SQL: {exc}") from exc
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Query):
        raise ValueError("only one read-only SELECT/CTE query is allowed")
    tree = parsed[0]
    forbidden = {
        "Insert",
        "Update",
        "Delete",
        "Create",
        "Drop",
        "Command",
        "Into",
        "Lock",
        "Copy",
    }
    if any(type(node).__name__ in forbidden for node in tree.walk()):
        raise ValueError("query contains a forbidden operation")
    # CTE names are scoped, not a global allowlist: a nested CTE cannot authorize
    # an unrelated outer physical table with the same name.
    try:
        for scope in traverse_scope(tree):
            for _, referenced in scope.selected_sources.values():
                if isinstance(referenced, exp.Table) and (
                    referenced.db or referenced.catalog or referenced.name not in tables
                ):
                    raise ValueError(
                        "query references a table outside the configured catalog"
                    )
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError(f"invalid investigation SQL: {exc}") from exc
    for function in tree.find_all(exp.Func):
        name = (
            function.name
            if isinstance(function, exp.Anonymous)
            else function.sql_name()
        )
        if str(name).casefold() in {
            "load_extension",
            "load_file",
            "readfile",
            "writefile",
            "sleep",
            "benchmark",
            "get_lock",
        }:
            raise ValueError("function is not allowed in read-only investigation")
    with session.lock:
        if session.database_calls >= session.config.agent_database_calls:
            raise ValueError("database investigation budget exhausted")
        session.database_calls += 1
    # Server/driver read-only mode is a second boundary beyond SQL shape checks.
    bounded = f"SELECT * FROM ({tree.sql(dialect=dialect)}) AS investigation LIMIT {limit + 1}"
    execution = execute_query(
        bounded,
        database_url=session.database_url,
        timeout=session.config.sql_timeout,
        read_only=True,
        row_limit=limit + 1,
    )
    if not execution.success:
        raise ValueError(f"database investigation failed: {execution.error}")
    result = execution.result
    rows = result.get("rows", [])
    payload = {
        "status": "ok",
        "columns": result.get("columns", []),
        "rows": rows[:limit],
        "truncated": len(rows) > limit,
    }
    ref = session.evidence.put("database_investigation", {"sql": sql, **payload})
    return session.summarize({**payload, "evidence_ref": ref}, evidence_id=ref)
