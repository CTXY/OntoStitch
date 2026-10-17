from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import BaseModel

from cortex.capabilities.sql_training.artifacts import write_jsonl
from cortex.capabilities.sql_training.artifacts.workspace import serialize_asset_envelope
from cortex.capabilities.sql_training.models import NodeAsset

_SECRET_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "password",
        "secret",
        "secret_key",
        "token",
    }
)


class TrainTraceLogger:
    def __init__(self, workspace_root: str | Path, run_id: str, *, level: str = ""):
        self.workspace_root = Path(workspace_root)
        self.run_id = run_id
        self.path = self.workspace_root / "logs" / "train" / f"{run_id}.trace.jsonl"
        self.level = level.strip().lower()
        self._sequence = 0
        self._lock = Lock()

    def log(
        self,
        *,
        source_id: str | None,
        step: str,
        case_index: int | None = None,
        phase: str | None = None,
        input: Any = None,
        output: Any = None,
        prompt: Any = None,
        response: Any = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        sanitized_output = sanitize_trace_payload(output)
        with self._lock:
            self._sequence += 1
            row = {
                "run_id": self.run_id,
                "sequence": self._sequence,
                "source_id": source_id,
                "case_index": case_index,
                "step": step,
                "phase": phase,
                "created_at": datetime.now(UTC).isoformat(),
                "input": sanitize_trace_payload(input),
                "output": sanitized_output,
                "prompt": sanitize_trace_payload(prompt),
                "response": sanitize_trace_payload(response),
                "summary": sanitize_trace_payload(summary or {}),
                "status": _trace_status(sanitized_output),
            }
            write_jsonl(self.path, [row], append=True)
        self._print(row)

    def _print(self, row: dict[str, Any]) -> None:
        if self.level not in {"info", "debug"}:
            return
        prefix = f"[train-trace] {row.get('source_id') or '-'} {row['step']}"
        if row.get("phase"):
            prefix = f"{prefix} ({row['phase']})"
        print(f"{prefix} {json.dumps(row['summary'], ensure_ascii=False, default=str)}")
        if self.level == "debug":
            detail = {
                "input": row.get("input"),
                "output": row.get("output"),
                "prompt": row.get("prompt"),
                "response": row.get("response"),
            }
            print(json.dumps(detail, ensure_ascii=False, default=str))


class TracingLLMProvider:
    """Provider decorator that audits every LLM call at one common boundary."""

    def __init__(self, provider: Any, trace_logger: TrainTraceLogger):
        self.provider = provider
        self.trace_logger = trace_logger
        self.source_id: str | None = None
        self.case_index: int | None = None
        self.phase: str | None = None

    def set_context(
        self,
        *,
        source_id: str | None,
        case_index: int | None,
        phase: str | None,
    ) -> None:
        self.source_id = source_id
        self.case_index = case_index
        self.phase = phase

    def extract_assets(
        self,
        record: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[Any]:
        call_context = dict(context or {})
        trace_capture = call_context.get("trace_capture")
        if not isinstance(trace_capture, dict):
            trace_capture = {}
            call_context["trace_capture"] = trace_capture
        prompt_name = str(call_context.get("prompt_name") or "unknown")
        source_id = str(
            record.get("source_id") or call_context.get("source_id") or self.source_id or ""
        ) or None
        case_index = call_context.get("case_index", self.case_index)
        phase = call_context.get("phase", self.phase)
        try:
            result = self.provider.extract_assets(record, context=call_context)
        except Exception as exc:
            summary = {"prompt_name": prompt_name, "error": str(exc)}
            _add_llm_attempt_trace(summary, trace_capture)
            self.trace_logger.log(
                source_id=source_id,
                case_index=case_index,
                step="llm_call",
                phase=phase,
                input={"prompt_name": prompt_name, "record": record},
                output={"error_type": type(exc).__name__, "error": str(exc)},
                prompt=trace_capture.get("prompt") or call_context.get("prompt"),
                response=trace_capture.get("response"),
                summary=summary,
            )
            raise
        summary = {"prompt_name": prompt_name, "result_count": len(result)}
        _add_llm_attempt_trace(summary, trace_capture)
        self.trace_logger.log(
            source_id=source_id,
            case_index=case_index,
            step="llm_call",
            phase=phase,
            input={"prompt_name": prompt_name, "record": record},
            output=result,
            prompt=trace_capture.get("prompt") or call_context.get("prompt"),
            response=trace_capture.get("response"),
            summary=summary,
        )
        return result


def _add_llm_attempt_trace(summary: dict[str, Any], trace_capture: dict[str, Any]) -> None:
    attempts = trace_capture.get("metric_generation_attempts")
    if isinstance(attempts, list):
        summary["metric_generation_attempts"] = attempts


def _trace_status(output: Any) -> str:
    if isinstance(output, dict) and (
        output.get("error")
        or output.get("error_type")
        or output.get("fatal") is True
        or output.get("success") is False
    ):
        return "error"
    return "ok"


def sanitize_trace_payload(value: Any) -> Any:
    if isinstance(value, NodeAsset):
        return _sanitize_mapping(serialize_asset_envelope(value))
    if isinstance(value, BaseModel):
        return sanitize_trace_payload(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return sanitize_trace_payload(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        asset = _canonical_asset_payload(value)
        if asset is not None:
            return _sanitize_mapping(serialize_asset_envelope(asset))
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [sanitize_trace_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_trace_payload(item) for item in value]
    return value


def _sanitize_mapping(value: dict[Any, Any]) -> dict[Any, Any]:
    """Sanitize a mapping already recognized as canonical without re-detecting it."""

    sanitized = {}
    for key, item in value.items():
        if str(key).lower() in _SECRET_KEYS:
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = sanitize_trace_payload(item)
    return sanitized


def _canonical_asset_payload(value: dict[Any, Any]) -> NodeAsset | None:
    """Recognize an embedded canonical asset without rewriting unrelated objects."""
    if not {"id", "node_type", "properties"}.issubset(value):
        return None
    try:
        asset = NodeAsset.model_validate(value)
    except (TypeError, ValueError):
        return None
    return (
        asset
        if asset.node_type.value
        in {
            "table",
            "column",
            "dimension_value",
            "metric",
            "concept",
            "generation_rule",
        }
        else None
    )
