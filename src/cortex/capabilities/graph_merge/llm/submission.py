"""Precise retry diagnostics; never repair or interpret model-generated edits."""

import json


def submission_error(message):
    errors = []
    for call in getattr(message, "invalid_tool_calls", ()):
        try:
            json.loads(call.get("args", ""))
        except (ValueError, TypeError) as exc:
            detail = (
                f"{exc.msg} at line {exc.lineno}, column {exc.colno}"
                if isinstance(exc, json.JSONDecodeError)
                else str(exc)
            )
            errors.append(f"{call.get('name') or 'tool'}: invalid JSON ({detail}).")
    return " ".join(errors)
