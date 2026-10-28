"""Atomic task snapshots and replayable events in a dedicated SQLite database."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


class QueryStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS query_records(id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS query_events(task_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(task_id,seq));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, key, kind=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT body,kind FROM query_records WHERE id=?", (key,)
            ).fetchone()
        if row is None or (kind and row["kind"] != kind):
            raise KeyError(key)
        return json.loads(row["body"])

    def list(self, kind):
        with self.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body FROM query_records WHERE kind=? ORDER BY rowid DESC",
                    (kind,),
                )
            ]

    def put(self, key, kind, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO query_records VALUES(?,?,?)",
                (key, kind, json.dumps(value, ensure_ascii=False)),
            )

    def update(
        self,
        key,
        *,
        event="phase.changed",
        payload=None,
        expected_version=None,
        **fields,
    ):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM query_records WHERE id=? AND kind='task'", (key,)
            ).fetchone()
            if row is None:
                raise KeyError(key)
            body = json.loads(row[0])
            if (
                expected_version is not None
                and body["state_version"] != expected_version
            ):
                raise ValueError("Task state changed; reload the latest question")
            body.update(fields)
            body["state_version"] += 1
            body["updated_at"] = now()
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM query_events WHERE task_id=?",
                (key,),
            ).fetchone()[0]
            entry = {
                "task_id": key,
                "seq": seq,
                "state_version": body["state_version"],
                "type": event,
                "created_at": body["updated_at"],
                "payload": payload
                or {"status": body["status"], "phase": body["phase"]},
            }
            body["last_event_seq"] = seq
            db.execute(
                "UPDATE query_records SET body=? WHERE id=?",
                (json.dumps(body, ensure_ascii=False), key),
            )
            db.execute(
                "INSERT INTO query_events VALUES(?,?,?)",
                (key, seq, json.dumps(entry, ensure_ascii=False)),
            )
        return body

    def events(self, key, after=0):
        with self.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body FROM query_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 500",
                    (key, after),
                )
            ]
