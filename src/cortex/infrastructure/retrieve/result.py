from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_OUTPUT_TIMEZONE = ZoneInfo("Asia/Shanghai")


class RetrieveResultWriteError(ValueError):
    """Raised when a unified retrieve result cannot be published."""


class FileSystemRetrieveResultRecorder:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()

    def write(self, result: dict[str, Any], *, started_at: datetime | None = None) -> Path:
        try:
            content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        except (TypeError, ValueError) as exc:
            raise RetrieveResultWriteError(
                f"retrieve result is not JSON serializable: {exc}"
            ) from exc
        candidate = (started_at or datetime.now(_OUTPUT_TIMEZONE)).astimezone(_OUTPUT_TIMEZONE)
        candidate = candidate.replace(microsecond=(candidate.microsecond // 1000) * 1000)
        while True:
            run_directory = self.workspace / "runs" / _dated_run_id(candidate)
            try:
                run_directory.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                candidate += timedelta(milliseconds=1)
            except OSError as exc:
                raise RetrieveResultWriteError(
                    f"unable to create retrieve result directory {run_directory}: {exc}"
                ) from exc
        result_path = run_directory / "result.json"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=run_directory,
                prefix=".result.json.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, result_path)
        except OSError as exc:
            result_path.unlink(missing_ok=True)
            try:
                run_directory.rmdir()
            except OSError:
                pass
            raise RetrieveResultWriteError(
                f"unable to save retrieve result under {run_directory}: {exc}"
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return result_path


def _dated_run_id(value: datetime) -> str:
    return f"{value:%Y%m%d-%H%M%S}-{value.microsecond // 1000:03d}"


__all__ = ["FileSystemRetrieveResultRecorder", "RetrieveResultWriteError"]
