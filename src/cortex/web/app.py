"""Ontology workbench with isolated real CLI training and explicit archive browsing."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
import sqlglot
from sqlglot import exp

from cortex.web.database_files import receive_database
from cortex.web.graphs import load_graph
from cortex.web.training import CliTrainingRunner, check_training_configuration

ROOT = Path(os.environ.get("CORTEX_PROJECT_ROOT", Path(__file__).resolve().parents[3])).resolve()
API_PREFIXES = ("/ontomosaic-text2sql/api/", "/ontology/api/")
LIMIT = 20 * 1024 * 1024
DATABASES = {
    "financial": {"name": "Financial", "description": "Bank accounts, clients, cards and transactions.", "domain": "Finance"},
    "thrombosis_prediction": {"name": "Thrombosis prediction", "description": "Patient records, laboratory tests and examinations.", "domain": "Healthcare"},
}


class ImportRequest(BaseModel):
    file_id: str


class TrainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_id: str
    mode: Literal["live", "demo"] = "live"
    request_id: str
    case_limit: int | None = Field(default=2, ge=1, le=10_000)


def create_app(*, database_root: Path | None = None, storage: Path | None = None, examples: Path | None = None, frontend: Path | None = None, training_runner=None, query_runtime_factory=None) -> FastAPI:
    database_root = database_root or Path(os.getenv("CORTEX_WEB_DATABASE_ROOT", ROOT / "data/web_databases"))
    storage = storage or Path(os.getenv("CORTEX_WEB_STORAGE", ROOT / "workspace/web"))
    examples = examples or Path(os.getenv("CORTEX_WEB_EXAMPLES", ROOT / "examples/ontology"))
    frontend = frontend or (Path(os.environ["CORTEX_WEB_FRONTEND_DIST"]) if os.getenv("CORTEX_WEB_FRONTEND_DIST") else None)
    if frontend:
        frontend = frontend.resolve()
    storage.mkdir(parents=True, exist_ok=True)
    metadata = storage / "metadata.sqlite"
    tasks: set[asyncio.Task] = set()
    runner = training_runner or CliTrainingRunner(storage)

    @contextmanager
    def connection():
        db = sqlite3.connect(metadata)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL)")
            yield db
            db.commit()
        finally:
            db.close()

    def put(key, kind, body):
        with connection() as db:
            db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)", (key, kind, json.dumps(body, ensure_ascii=False)))

    def get(key, kind):
        with connection() as db:
            row = db.execute("SELECT body FROM records WHERE id=? AND kind=?", (key, kind)).fetchone()
        if row is None:
            raise HTTPException(404, "Record not found")
        return json.loads(row[0])

    def listing(kind):
        with connection() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT body FROM records WHERE kind=? ORDER BY rowid DESC", (kind,))]

    def database_path(db_id):
        if db_id in DATABASES:
            path = database_root / db_id / f"{db_id}.sqlite"
        elif re.fullmatch(r"uploaded_[0-9a-f]{32}", db_id):
            get(db_id, "database")
            path = storage / "databases" / db_id / "database.sqlite"
        else:
            raise HTTPException(404, "Unknown database")
        if not path.is_file():
            raise HTTPException(503, "Database file is not configured. Run scripts/prepare_ontology_demo.py.")
        return path

    @contextmanager
    def readonly(db_id):
        db = sqlite3.connect(database_path(db_id).resolve().as_uri() + "?mode=ro", uri=True)
        db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 3
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            yield db
        finally:
            db.close()

    def quote(name):
        return '"' + name.replace('"', '""') + '"'

    def catalog(db_id):
        with readonly(db_id) as db:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return [{"name": name, "columns": [{"name": r[1], "type": r[2], "nullable": not bool(r[3]) and not bool(r[5]), "primary_key": bool(r[5])} for r in db.execute(f"PRAGMA table_info({quote(name)})")], "foreign_keys": [{"column": r[3], "table": r[2], "target": r[4]} for r in db.execute(f"PRAGMA foreign_key_list({quote(name)})")]} for name in names]

    @asynccontextmanager
    async def lifespan(_app):
        for run in listing("run"):
            if run["status"] in {"queued", "running"}:
                run.update(status="interrupted", phase="interrupted", error="Training service restarted. Start a new run.", eta_seconds=None)
                put(run["id"], "run", run)
        query_service.recover()
        yield
        await query_service.close()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Ontology workbench", lifespan=lifespan)

    @app.get("/api/health")
    def health():
        try:
            check_training_configuration()
            ready, error = True, None
        except ValueError as exc:
            ready, error = False, str(exc)
        return {"status": "ok", "training_mode": "live", "live_sql_train": True, "training_ready": ready, "training_error": error}

    def database_summary(key, meta):
        flags = {"has_example_pairs": key in DATABASES and (examples / f"{key}.pairs.jsonl").is_file(), "has_archive": key in DATABASES and (examples / f"{key}.graph.json").is_file()}
        try:
            tables = catalog(key)
            return {"id": key, **meta, **flags, "available": True, "tables": len(tables), "columns": sum(len(t["columns"]) for t in tables), "size": database_path(key).stat().st_size}
        except HTTPException:
            return {"id": key, **meta, **flags, "available": False, "tables": 0, "columns": 0, "size": 0}

    @app.get("/api/databases")
    def databases():
        records = [database_summary(key, meta) for key, meta in DATABASES.items()]
        records.extend(database_summary(meta["id"], meta) for meta in listing("database"))
        return {"databases": records}

    @app.post("/api/databases/upload", status_code=201)
    async def upload_database(file: UploadFile = File(...)):
        record = await receive_database(file, storage)
        put(record["id"], "database", record)
        return database_summary(record["id"], record)

    @app.get("/api/databases/{db_id}/schema")
    def schema(db_id: str):
        return {"tables": catalog(db_id)}

    @app.get("/api/databases/{db_id}/rows")
    def rows(db_id: str, table: str, offset: int = Query(0, ge=0, le=10_000_000), limit: int = Query(20, ge=1, le=100)):
        if table not in {t["name"] for t in catalog(db_id)}:
            raise HTTPException(404, "Unknown table")
        with readonly(db_id) as db:
            cursor = db.execute(f"SELECT * FROM {quote(table)} LIMIT ? OFFSET ?", (limit + 1, offset))
            values = cursor.fetchall()
            def cell(value):
                if isinstance(value, bytes):
                    return f"[BLOB: {len(value)} bytes]"
                return value[:2000] if isinstance(value, str) else value
            return {"columns": [r[0] for r in cursor.description], "rows": [[cell(v) for v in row] for row in values[:limit]], "has_more": len(values) > limit}

    @app.get("/api/databases/{db_id}/example-pairs")
    def example_pairs(db_id: str):
        if db_id not in DATABASES:
            raise HTTPException(404, "Unknown database")
        return FileResponse(examples / f"{db_id}.pairs.jsonl", filename=f"{db_id}.pairs.jsonl", media_type="application/x-ndjson")

    @app.get("/api/databases/{db_id}/graph")
    def archived_graph(db_id: str):
        if db_id not in DATABASES:
            raise HTTPException(404, "Unknown database")
        path = examples / f"{db_id}.graph.json"
        if not path.is_file():
            raise HTTPException(404, "Archived graph not configured")
        return {**json.loads(path.read_text()), "origin": "archived_merge", "database_id": db_id}

    async def read_upload(file):
        content = await file.read(LIMIT + 1)
        if len(content) > LIMIT:
            raise HTTPException(413, "File exceeds 20 MiB")
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "Choose a UTF-8 file") from None

    @app.post("/api/training-files")
    async def upload_pairs(database_id: str, file: UploadFile = File(...)):
        database_path(database_id)
        text = await read_upload(file)
        pairs, errors, ids = [], [], set()
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            if len(pairs) + len(errors) >= 10_000:
                raise HTTPException(422, "Maximum 10,000 records")
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Each line must be an object")
                normalized = {}
                for key, aliases in (("question", ("question", "nlq", "NLQ")), ("sql", ("sql", "SQL"))):
                    values = [row[a] for a in aliases if a in row]
                    if not values or any(not isinstance(v, str) or not v.strip() for v in values):
                        raise ValueError(f"{key} must be nonempty text")
                    if len({v.strip() for v in values}) != 1:
                        raise ValueError(f"Conflicting {key} aliases")
                    normalized[key] = values[0].strip()
                for field in ("db_id", "database_id"):
                    if field in row and row[field] != database_id:
                        raise ValueError("Record belongs to a different database")
                source = str(row.get("source_id", row.get("id", f"case-{number:04d}"))).strip()
                if not source or source in ids:
                    raise ValueError("Empty or duplicate source_id")
                evidence = row.get("evidence")
                if evidence is not None and not isinstance(evidence, str):
                    raise ValueError("evidence must be text or null")
                ids.add(source)
                pairs.append({"source_id": source, **normalized, "evidence": evidence, "line": number})
            except (ValueError, TypeError) as exc:
                errors.append({"line": number, "message": str(exc)[:300]})
        if not pairs and not errors:
            errors.append({"line": 1, "message": "File has no records"})
        key = uuid4().hex
        body = {"id": key, "database_id": database_id, "filename": Path(file.filename or "pairs.jsonl").name, "count": len(pairs), "errors": errors, "pairs": pairs, "imported": False}
        put(key, "input", body)
        return {k: v for k, v in body.items() if k != "pairs"}

    @app.post("/api/imports")
    def import_pairs(request: ImportRequest):
        body = get(request.file_id, "input")
        if body["errors"] or not body["count"]:
            raise HTTPException(422, "Fix all file errors before importing")
        body["imported"] = True
        put(body["id"], "input", body)
        return {k: v for k, v in body.items() if k != "pairs"}

    @app.get("/api/imports/{key}/pairs")
    def pairs(key: str, offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=100)):
        body = get(key, "input")
        return {"pairs": body["pairs"][offset:offset + limit], "total": body["count"]}

    @app.get("/api/imports/{key}")
    def input_metadata(key: str):
        return {k: v for k, v in get(key, "input").items() if k != "pairs"}

    @app.post("/api/graphs/import")
    async def import_graph(file: UploadFile = File(...)):
        try:
            result = await asyncio.to_thread(load_graph, json.loads(await read_upload(file)))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(422, f"Invalid graph: {str(exc)[:800]}") from exc
        key = uuid4().hex
        put(key, "graph", {**result, "id": key, "origin": "imported", "filename": Path(file.filename or "graph.json").name})
        return {**result, "id": key, "origin": "imported", "filename": Path(file.filename or "graph.json").name}

    @app.get("/api/graphs/{key}")
    def imported_graph(key: str):
        return get(key, "graph")

    async def preview_run(run, body):
        started = time.monotonic()
        def update(**fields):
            run.update(fields, elapsed_seconds=round(time.monotonic() - started, 1), updated_at=datetime.now(timezone.utc).isoformat())
            put(run["id"], "run", run)
        try:
            update(status="running", phase="preparing", eta_seconds=4)
            await asyncio.sleep(.6)
            sample = body["pairs"][:2]
            results = []
            for pair in sample:
                update(phase="validating_sample", eta_seconds=3)
                def check():
                    try:
                        statements = sqlglot.parse(pair["sql"], read="sqlite")
                        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
                            raise ValueError("Preview only executes a single SELECT query")
                        with readonly(body["database_id"]) as db:
                            db.execute(pair["sql"]).fetchmany(1)
                        return {"source_id": pair["source_id"], "executable": True}
                    except (sqlite3.Error, ValueError, sqlglot.errors.ParseError) as exc:
                        return {"source_id": pair["source_id"], "executable": False, "error": str(exc)[:200]}
                results.append(await asyncio.to_thread(check))
                update(pairs_completed=len(results), validation=results)
                await asyncio.sleep(.6)
            update(phase="loading_archived_graph", eta_seconds=1)
            graph = archived_graph(body["database_id"])
            await asyncio.sleep(.8)
            update(status="completed", phase="completed", eta_seconds=0, result={"nodes": len(graph["state"]["graph"]["nodes"]), "edges": len(graph["state"]["graph"]["edges"]), "conflicts": sum(n.get("merge_status") == "conflict" for n in graph["state"]["graph"]["nodes"]), "origin": "archived_merge", "note": "Preview only: checked up to two SQL examples for executability; displayed graph is an existing merge result, not learned from this upload."})
        except Exception as exc:
            update(status="failed", phase="failed", eta_seconds=None, error=str(exc)[:400])

    @app.post("/api/sql-train", status_code=202)
    async def sql_train(request: TrainRequest):
        body = get(request.import_id, "input")
        if not body["imported"]:
            raise HTTPException(409, "Import the pairs first")
        for previous in listing("run"):
            if previous["request_id"] == request.request_id:
                if previous["import_id"] != request.import_id or previous["mode"] != request.mode or previous.get("case_limit", 2) != request.case_limit:
                    raise HTTPException(409, "Request ID already used with different input")
                return previous
            if previous["database_id"] == body["database_id"] and previous["status"] in {"queued", "running"}:
                raise HTTPException(409, "This database already has an active training run")
        if request.mode == "live" and training_runner is None:
            try:
                await asyncio.to_thread(check_training_configuration)
            except ValueError as exc:
                raise HTTPException(503, str(exc)) from exc
        selected_pairs = body["pairs"][:request.case_limit] if request.case_limit else body["pairs"]
        completed_history = [r for r in listing("run") if r["database_id"] == body["database_id"] and r.get("mode") == "live" and r["status"] in {"completed", "needs_review"} and r.get("pairs_completed", 0) and r.get("elapsed_seconds", 0)]
        from statistics import median
        case_seconds = median(r["elapsed_seconds"] / r["pairs_completed"] for r in completed_history) if completed_history else None
        run = {"id": uuid4().hex, "import_id": body["id"], "request_id": request.request_id, "database_id": body["database_id"], "mode": request.mode, "case_limit": request.case_limit, "status": "queued", "phase": "queued", "imported_total": body["count"], "pairs_total": len(selected_pairs), "sample_total": min(2, body["count"]), "pairs_completed": 0, "pairs_running": 0, "elapsed_seconds": 0, "eta_seconds": None, "eta_basis": "insufficient_history", "created_at": datetime.now(timezone.utc).isoformat(), "result": None, "error": None}
        run.update(eta_case_seconds=case_seconds, eta_sample_cases=sum(r["pairs_completed"] for r in completed_history))
        if request.mode == "live" and case_seconds:
            run.update(eta_seconds=round(case_seconds * len(selected_pairs)), eta_basis="historical_completed_runs")
        put(run["id"], "run", run)
        async def live_run():
            def update(**fields):
                run.update(fields)
                put(run["id"], "run", run)
            try:
                await runner.run(run, selected_pairs, database_path(body["database_id"]), update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                update(status="failed", phase="failed", eta_seconds=None, error=str(exc)[:500], pairs_running=0)
        task = asyncio.create_task(live_run() if request.mode == "live" else preview_run(run, body))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return run

    @app.get("/api/runs")
    def runs(database_id: str | None = None):
        return {"runs": [r for r in listing("run") if database_id is None or r["database_id"] == database_id][:50]}

    @app.get("/api/runs/{key}")
    def run(key: str):
        return get(key, "run")

    @app.get("/api/runs/{key}/graph")
    def run_graph(key: str):
        record = get(key, "run")
        if record["status"] not in {"completed", "needs_review"} or not record.get("result"):
            raise HTTPException(409, "This run has no completed graph")
        if record["mode"] == "demo":
            return archived_graph(record["database_id"])
        try:
            return runner.graph(key)
        except FileNotFoundError:
            raise HTTPException(404, "This run's graph artifact is missing") from None

    # The interactive query agent has its own durable store and leaves baseline/ untouched.
    from cortex.capabilities.nl2sql.service import QueryService
    from cortex.web.nl2sql import query_router

    def query_graph_source(db_id, source):
        if source["kind"] == "archive":
            return archived_graph(db_id)
        if source["kind"] == "run":
            record = get(source["id"], "run")
            if record["database_id"] != db_id:
                raise HTTPException(409, "Ontology run belongs to another database")
            return run_graph(source["id"])
        return imported_graph(source["id"])

    def query_sources(db_id):
        result = []
        if db_id in DATABASES and (examples / f"{db_id}.graph.json").is_file():
            result.append({"kind": "archive", "id": "", "label": "Prebuilt ontology"})
        result.extend({"kind": "run", "id": r["id"], "label": f"Build {r['id'][:8]} · {r['created_at'][:16]}"} for r in listing("run") if r["database_id"] == db_id and r["status"] in {"completed", "needs_review"} and r.get("result") and r.get("mode") == "live")
        result.extend({"kind": "import", "id": g["id"], "label": "Imported: " + g.get("filename", g["id"][:8])} for g in listing("graph"))
        return result

    query_service = QueryService(storage / "nl2sql", database_path, query_graph_source, query_runtime_factory)
    app.state.query_service = query_service
    app.include_router(query_router(query_service, query_sources))

    # Direct local preview of the actual portal build, no separate HTML viewer.
    if frontend:
        @app.get("/{path:path}")
        def serve_frontend(path: str):
            if path == "api" or path.startswith("api/") or any(("/" + path).startswith(prefix.rstrip("/")) for prefix in API_PREFIXES):
                raise HTTPException(404, "Unknown API route")
            candidate = (frontend / path).resolve()
            if candidate.is_relative_to(frontend.resolve()) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend / "index.html")

    @app.middleware("http")
    async def same_origin_prefix(request, call_next):
        for prefix in API_PREFIXES:
            if request.scope["path"].startswith(prefix):
                request.scope["path"] = "/api/" + request.scope["path"][len(prefix):]
                request.scope["raw_path"] = request.scope["path"].encode("utf-8")
                break
        return await call_next(request)
    return app


app = create_app()
