"""Explicit, payload-free LangSmith spans with local full-evidence references."""

from __future__ import annotations

import os
from contextlib import contextmanager
from time import perf_counter

from langsmith import trace, tracing_context

_ALLOWED = {
    "case_id",
    "round",
    "iteration",
    "revision",
    "evidence_ref",
    "tool",
    "node_count",
    "model_calls",
    "tool_calls",
    "status",
    "error_type",
    "duration_seconds",
    "context_id",
    "prompt_fingerprint",
    "schema_version",
    "mode",
    "model",
}


class SummaryTracer:
    def __init__(self, *, client=None, enabled: bool | None = None):
        self.client = client
        self.enabled = (
            os.getenv(
                "LANGSMITH_TRACING", os.getenv("LANGCHAIN_TRACING_V2", "")
            ).lower()
            == "true"
            if enabled is None
            else enabled
        )

    @contextmanager
    def span(self, name: str, **metadata):
        started = perf_counter()
        summary = {key: value for key, value in metadata.items() if key in _ALLOWED}
        if not self.enabled:
            with tracing_context(enabled=False):
                yield summary
            return
        error = None
        traceback = None
        with tracing_context(enabled=True, client=self.client):  # noqa: SIM117 - keep tracing scope separate from the safe operation scope
            with trace(
                name, inputs={}, metadata=summary, run_type="chain", client=self.client
            ) as run:
                # Keep this explicit parent, but disable automatic child payloads.
                with tracing_context(enabled=False):
                    try:
                        yield summary
                    except BaseException as exc:  # noqa: BLE001 - rethrow after preventing raw exception tracing
                        error, traceback = exc, exc.__traceback__
                        summary.update(status="error", error_type=type(exc).__name__)
                summary.setdefault("status", "ok")
                summary["duration_seconds"] = perf_counter() - started
                safe = {key: value for key, value in summary.items() if key in _ALLOWED}
                run.metadata.update(safe)
                run.end(
                    outputs={"status": summary["status"]},
                    error=summary.get("error_type") if error else None,
                )
        # The trace context never sees the original exception text (SQL/parameters).
        if error is not None:
            raise error.with_traceback(traceback)
