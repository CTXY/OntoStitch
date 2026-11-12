"""Native LangSmith model/usage tracing without uploading ontology or SQL payloads."""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache

from langsmith import Client, tracing_context


@lru_cache(maxsize=1)
def _summary_client():
    return Client(
        hide_inputs=True,
        hide_outputs=True,
        anonymizer=lambda value: {
            k: ("operation failed" if k in {"error", "events"} and v else v)
            for k, v in value.items()
        },
    )


@contextmanager
def merge_tracing():
    enabled = (
        os.getenv("LANGSMITH_TRACING", os.getenv("LANGCHAIN_TRACING_V2", "")).lower()
        == "true"
    )
    # Explicitly re-enable native child spans even inside training's payload-free tracer.
    # Token usage and model metadata remain available for native cost tracking.
    with tracing_context(
        enabled=enabled, client=_summary_client() if enabled else None
    ):
        yield
