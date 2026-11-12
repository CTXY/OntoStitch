"""Validate and store uploaded SQLite files separately from bundled databases."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile

DATABASE_UPLOAD_LIMIT = 200 * 1024 * 1024


def validate_database(path: Path) -> None:
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise ValueError("Choose a SQLite database file, not SQL text or JSON")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        deadline = time.monotonic() + 15
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("SQLite integrity check failed")
        names = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        if not names:
            raise ValueError("Database must contain at least one user table")
        for name in names:
            db.execute(
                'PRAGMA table_info("' + name.replace('"', '""') + '")'
            ).fetchall()
    finally:
        db.close()


async def receive_database(file: UploadFile, storage: Path) -> dict:
    name = Path((file.filename or "").replace("\\", "/")).name
    if Path(name).suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
        await file.close()
        raise HTTPException(422, "Choose a .sqlite, .sqlite3 or .db SQLite file")
    db_id = "uploaded_" + uuid4().hex
    directory = storage / "databases" / db_id
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "database.sqlite"
    accepted = False
    try:
        size = 0
        with path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > DATABASE_UPLOAD_LIMIT:
                    raise HTTPException(413, "Database file exceeds 200 MiB")
                await asyncio.to_thread(target.write, chunk)
        try:
            await asyncio.to_thread(validate_database, path)
        except (sqlite3.Error, ValueError):
            raise HTTPException(
                422,
                "Invalid SQLite database: file must be intact and contain a user table",
            ) from None
        accepted = True
        return {
            "id": db_id,
            "name": Path(name).stem[:160],
            "filename": name[:200],
            "description": "Uploaded SQLite database.",
            "domain": "Uploaded",
            "source": "uploaded",
        }
    finally:
        await file.close()
        if not accepted:
            shutil.rmtree(directory)
