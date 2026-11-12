"""Read-only SQLite adapter. DDL facts and bounded observations, never semantics."""

import math
import sqlite3
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

from cortex.capabilities.metadata.models import (
    BuildOptions,
    Column,
    ColumnProfile,
    Database,
    ForeignKey,
    MetadataSnapshot,
    SourceRecord,
    Table,
    TableProfile,
    TopValue,
    object_id,
    utc_now,
)


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def sqlite_name(name):
    # SQLite identifier folding is ASCII-only, not Unicode casefold().
    return name.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def json_value(value):
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return {"encoding": "nonfinite", "value": str(value)}
    return value


class SQLiteMetadataSource:
    def __init__(self, path: str | Path, options: BuildOptions):
        self.path = Path(path).expanduser().resolve(strict=True)
        self.options = options
        self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("BEGIN")
        self.connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        # Opaque identity for this pinned read transaction, not a content hash.
        # Do not load an entire database into memory merely to name a bounded scan.
        self.snapshot_id = f"sqlite-read:{uuid4().hex}"

    def close(self):
        self.connection.close()

    def _read(self, sql, limit=None, *, agent=False):
        deadline = time.monotonic() + self.options.query_timeout
        self.connection.set_progress_handler(
            lambda: int(time.monotonic() > deadline), 1000
        )
        if agent:
            allowed = {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
                sqlite3.SQLITE_RECURSIVE,
            }
            self.connection.set_authorizer(
                lambda action, a, b, db, trigger: (
                    sqlite3.SQLITE_OK
                    if action in allowed
                    and not (
                        action == sqlite3.SQLITE_FUNCTION
                        and b in {"load_extension", "writefile", "readfile"}
                    )
                    else sqlite3.SQLITE_DENY
                )
            )
        try:
            cursor = self.connection.execute(sql)
            if agent and cursor.description:
                labels = [column[0] for column in cursor.description]
                if len(labels) != len(set(labels)):
                    raise ValueError(
                        "DUPLICATE_RESULT_COLUMNS: use explicit unique AS aliases for every repeated column name"
                    )
            return cursor.fetchmany(limit) if limit is not None else cursor.fetchall()
        finally:
            self.connection.set_progress_handler(None, 0)
            self.connection.set_authorizer(None)

    def query(self, sql: str):
        rows = self._read(sql, 101, agent=True)
        return {
            "rows": [
                {k: json_value(v) for k, v in dict(r).items()} for r in rows[:100]
            ],
            "truncated": len(rows) > 100,
            "snapshot_id": self.snapshot_id,
        }

    def scan(self) -> MetadataSnapshot:
        schema_rows = self._read(
            "SELECT name,type,sql FROM sqlite_schema WHERE type IN ('table','view') AND name NOT GLOB 'sqlite_*' ORDER BY name"
        )
        tables = []
        issues = []
        for row in schema_rows:
            name = row["name"]
            columns = self._read(f"PRAGMA table_xinfo({identifier(name)})")
            tables.append(
                Table(
                    name=name,
                    object_type=row["type"],
                    columns=[
                        Column(
                            name=c["name"],
                            data_type=c["type"],
                            nullable=False
                            if c["notnull"]
                            else None
                            if c["pk"]
                            else True,
                            default_expression=c["dflt_value"],
                        )
                        for c in columns
                        if c["hidden"] != 1
                    ],
                    primary_keys=[
                        c["name"]
                        for c in sorted(columns, key=lambda c: c["pk"])
                        if c["pk"]
                    ],
                )
            )
        by_name = {sqlite_name(t.name): t for t in tables}
        for table in tables:
            for idx in self._read(f"PRAGMA index_list({identifier(table.name)})"):
                if not idx["unique"] or idx["partial"] or idx["origin"] == "pk":
                    continue
                cols = self._read(f"PRAGMA index_info({identifier(idx['name'])})")
                if all(c["name"] is not None for c in cols):
                    table.unique_keys.append([c["name"] for c in cols])
                else:
                    issues.append(
                        f"Expression unique index retained in schema source only: {idx['name']}"
                    )
            grouped = {}
            for fk in self._read(f"PRAGMA foreign_key_list({identifier(table.name)})"):
                grouped.setdefault(fk["id"], []).append(fk)
            for number, rows in grouped.items():
                rows.sort(key=lambda r: r["seq"])
                target = by_name.get(sqlite_name(rows[0]["table"]))
                target_names = (
                    {sqlite_name(c.name): c.name for c in target.columns}
                    if target
                    else {}
                )
                target_cols = (
                    [target_names.get(sqlite_name(r["to"]), "") for r in rows]
                    if target and all(r["to"] is not None for r in rows)
                    else target.primary_keys
                    if target
                    else []
                )
                local_names = {sqlite_name(c.name): c.name for c in table.columns}
                local = [local_names.get(sqlite_name(r["from"]), "") for r in rows]
                if (
                    not target
                    or "" in local
                    or len(target_cols) != len(local)
                    or not set(target_cols) <= {c.name for c in target.columns}
                ):
                    issues.append(f"Unresolved declared FK: {table.name}/{number}")
                    continue
                table.foreign_keys.append(
                    ForeignKey(
                        columns=local,
                        referenced_table=target.name,
                        referenced_columns=target_cols,
                    )
                )
            try:
                self.profile(table)
            except sqlite3.DatabaseError as exc:
                issues.append(f"Profiling unavailable for {table.name}: {exc}")
        return MetadataSnapshot(
            database=Database(
                id=object_id("database", self.path.stem),
                name=self.path.stem,
                source=str(self.path),
                snapshot_id=self.snapshot_id,
            ),
            tables=tables,
            issues=issues,
            sources=[
                SourceRecord(
                    id="schema",
                    kind="database_schema",
                    locator=str(self.path),
                    content={
                        "snapshot_id": self.snapshot_id,
                        "ddl": {row["name"]: row["sql"] for row in schema_rows},
                        "indexes": [
                            dict(row)
                            for row in self._read(
                                "SELECT name, tbl_name, sql FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL ORDER BY name"
                            )
                        ],
                    },
                )
            ],
        )

    def profile(self, table):
        rows = self._read(
            f"SELECT * FROM {identifier(table.name)} LIMIT {self.options.profile_rows + 1}",
            self.options.profile_rows + 1,
        )
        complete = len(rows) <= self.options.profile_rows
        rows = rows[: self.options.profile_rows]
        common = {
            "observed_at": utc_now(),
            "snapshot_id": self.snapshot_id,
            "scan_mode": "full" if complete else "sample",
            "sampling_method": "all_rows" if complete else "prefix",
            "rows_examined": len(rows),
        }
        table.profile = TableProfile(
            **common, row_count=len(rows) if complete else None
        )
        for column in table.columns:
            values = [r[column.name] for r in rows if r[column.name] is not None]
            # Physical value equality (numeric 1 and 1.0 coincide); not SQL
            # collation-aware equality such as NOCASE for text.
            frequencies = Counter(values)
            comparable = values and all(
                type(v) in {int, float} and math.isfinite(v) for v in values
            )
            column.profile = ColumnProfile(
                **common,
                null_fraction=(len(rows) - len(values)) / len(rows) if rows else None,
                distinct_count=len(frequencies),
                min=json_value(min(values)) if comparable else None,
                max=json_value(max(values)) if comparable else None,
                examples=[
                    json_value(v) for v in list(frequencies)[: self.options.examples]
                ],
                top_values=[
                    TopValue(value=json_value(v), count=n)
                    for v, n in frequencies.most_common(self.options.examples)
                ],
            )
