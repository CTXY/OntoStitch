"""Lazy revision storage: construction and reads never create or modify files."""

import sqlite3
from contextlib import closing
from pathlib import Path

from cortex.capabilities.metadata.models import MetadataSnapshot, utc_now


class MetadataRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def check_source(self, source: str | Path):
        source = Path(source).expanduser().resolve()
        if source == self.path or (
            self.path.exists() and source.exists() and self.path.samefile(source)
        ):
            raise ValueError(
                "Source database and metadata repository must be different files"
            )

    def load(self, revision: int | None = None) -> MetadataSnapshot:
        if not self.path.exists():
            raise LookupError("metadata revision not found")
        with closing(
            sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='revisions'"
            ).fetchone():
                raise LookupError("not a metadata repository")
            row = connection.execute(
                "SELECT payload FROM revisions ORDER BY revision DESC LIMIT 1"
                if revision is None
                else "SELECT payload FROM revisions WHERE revision=?",
                () if revision is None else (revision,),
            ).fetchone()
        if row is None:
            raise LookupError("metadata revision not found")
        return MetadataSnapshot.model_validate_json(row[0])

    def save(
        self, snapshot: MetadataSnapshot, *, expected_revision: int
    ) -> MetadataSnapshot:
        self.check_source(snapshot.database.source)
        snapshot = MetadataSnapshot.model_validate(snapshot.model_dump(mode="json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
                )
            }
            if tables - {"revisions"}:
                raise ValueError("target file is not a standalone metadata repository")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS revisions (revision INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            latest = connection.execute(
                "SELECT revision,payload FROM revisions ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            revision = latest[0] if latest else 0
            if revision != expected_revision:
                raise ValueError(
                    f"STALE_REVISION: expected {expected_revision}, current {revision}"
                )
            if (
                latest
                and MetadataSnapshot.model_validate_json(latest[1]).database.source
                != snapshot.database.source
            ):
                raise ValueError("repository belongs to another database")
            saved = snapshot.model_copy(
                update={"revision": revision + 1, "updated_at": utc_now()}
            )
            connection.execute(
                "INSERT INTO revisions VALUES (?,?)",
                (saved.revision, saved.model_dump_json()),
            )
        return saved
