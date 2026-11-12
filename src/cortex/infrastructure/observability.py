"""Native tracing with credential redaction at the SDK egress boundary."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from functools import lru_cache

from langsmith import Client, tracing_context
from langsmith.run_helpers import get_tracing_context

_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|password|secret|authorization|access_token|refresh_token|^token$)",
    re.IGNORECASE,
)
_CREDENTIAL = re.compile(
    r"(?:lsv2_[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{12,}|Bearer\s+[^\s\"']+|(?<=://)[^\s/@:]+:[^\s/@]+@)",
    re.IGNORECASE,
)


def redact_credentials(value):
    """Keep business payloads and usage intact; remove credential fields/strings."""
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if _SECRET_KEY.search(str(k)) else redact_credentials(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_credentials(v) for v in value]
    if isinstance(value, str):
        value = _CREDENTIAL.sub("[REDACTED]", value)
        for name, secret in os.environ.items():
            if _SECRET_KEY.search(name) and len(secret) >= 8:
                value = value.replace(secret, "[REDACTED]")
    return value


@lru_cache(maxsize=1)
def tracing_client():
    return Client(anonymizer=redact_credentials)


@contextmanager
def native_tracing(*, client=None, enabled=None):
    context = get_tracing_context()
    if enabled is None:
        enabled = context.get("enabled")
        if enabled is None:
            enabled = (
                os.getenv(
                    "LANGSMITH_TRACING", os.getenv("LANGCHAIN_TRACING_V2", "")
                ).lower()
                == "true"
            )
    with tracing_context(
        enabled=enabled,
        client=client
        or context.get("client")
        or (tracing_client() if enabled else None),
    ):
        yield
