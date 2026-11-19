"""Stdlib-only SQLite execution in an explicitly selected Python runtime.

SQL travels over stdin, never shell interpolation. Both oracle and candidate
queries use this same configured backend; no SQL rewrites or schema changes.
"""
import base64
import json
import sqlite3
import sys
import time


def main():
    request = json.load(sys.stdin)
    deadline = time.monotonic() + request["timeout"]
    connection = None
    try:
        connection = sqlite3.connect(request["database"], uri=True)
        if request["read_only"]:
            connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        cursor = connection.execute(request["sql"])
        columns = [item[0] for item in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(request["row_limit"]) if request["row_limit"] is not None else cursor.fetchall()
        result = {"success": True, "result": {"columns": columns, "rows": rows}, "error": None}
    except Exception as error:
        result = {"success": False, "error": (
            f"query execution timed out after {request['timeout']} seconds"
            if time.monotonic() >= deadline else str(error))}
    finally:
        if connection is not None:
            connection.close()
    def encode(value):
        if isinstance(value, bytes):
            return {"__sqlite_blob__": base64.b64encode(value).decode()}
        raise TypeError(type(value).__name__)
    json.dump({"execution": result, "sqlite_version": sqlite3.sqlite_version}, sys.stdout, default=encode)


if __name__ == "__main__":
    main()
