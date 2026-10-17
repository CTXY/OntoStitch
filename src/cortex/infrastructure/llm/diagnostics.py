"""Bounded, credential-redacted exception details for persisted LLM traces."""

from __future__ import annotations

import re
from typing import Any


def _safe_message(message: str) -> str:
    # URLs can carry credentials in userinfo, query parameters, or path segments.
    message = re.sub(r"https?://[^\s\"'<>]+", "[redacted URL]", message)
    message = re.sub(r"(?i)\bBearer\s+[^\s\"',;}]+", "Bearer [redacted]", message)
    message = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", message)
    message = re.sub(
        r"(?i)([\"']?(?:authorization|api[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|token|password|secret|cookie|set-cookie)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1[redacted]",
        message,
    )
    return message[:2000]


def exception_details(error: BaseException) -> list[dict[str, Any]]:
    """Follow visible causes without dumping request objects, headers, or bodies."""
    details: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(details) < 8:
        seen.add(id(current))
        item: dict[str, Any] = {
            "type": type(current).__name__,
            "message": _safe_message(str(current)),
        }
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            item["status_code"] = status
        details.append(item)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return details
