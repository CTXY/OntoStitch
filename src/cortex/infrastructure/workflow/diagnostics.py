"""Safe, typed failure records; infrastructure failures are not SQL mismatches."""

import re


def safe_message(value, *, limit=2400):
    text = str(value)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|authorization)\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", text)
    return text[:limit]


def classify_workflow_error(exc, *, stage="online_context"):
    chain, seen, current = [], set(), exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        item = {"type": type(current).__name__, "sanitized_message": safe_message(current)}
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            item["http_status"] = status
        chain.append(item)
        current = current.__cause__
    statuses = {x.get("http_status") for x in chain}
    types = " ".join(x["type"].lower() for x in chain)
    code, retryable = "online_internal_error", False
    declared = next((e for e in _causes(exc) if getattr(e, "error_code", None)), None)
    if declared is not None:
        code = declared.error_code
        stage = getattr(declared, "error_stage", stage)
        retryable = bool(getattr(declared, "retryable", False))
    elif 402 in statuses:
        code, stage = "provider_quota_error", "provider_response"
    elif statuses & {401, 403}:
        code = "authentication_error"
    elif statuses & {408, 429, 500, 502, 503, 504} or any(
        t in types for t in ("timeout", "connection", "connecterror", "protocolerror", "admissionpaused")
    ):
        code, retryable = "transport_error", True
    elif any(t in types for t in ("llmstructuredoutputerror", "validationerror", "jsondecodeerror")):
        code = "output_contract_error"
        detail = " ".join(x["sanitized_message"] for x in chain)
        for schema, phase in (
            ("InitialSqlMemoryOutput", "discovery.initial_sql_memory"),
            ("IntraNodeOutput", "discovery.intra_node"),
            ("CrossNodeOutput", "discovery.cross_node"),
            ("RelevanceOutput", "retrieval.relevance"),
        ):
            if schema in detail:
                stage = phase
                break
    elif isinstance(exc, ValueError):
        code = "output_validation_error"
    return {
        "error_code": code, "error_stage": stage, "retryable": retryable,
        "error_type": type(exc).__name__, "safe_message": safe_message(exc),
        "error_chain": chain,
    }


def _causes(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__


def case_outcome(online, prediction):
    """Coverage is separate from EA: a blocked online run is not a SQL pair."""
    if prediction is None:
        return "online_blocked" if online.get("status") == "error" else "online_no_guidance"
    if prediction.get("error"):
        return "sql_error"
    return "correct" if prediction.get("evaluation", {}).get("exec") == 1 else "ea_mismatch"
