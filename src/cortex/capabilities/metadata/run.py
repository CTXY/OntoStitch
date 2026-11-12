"""Per-run results, including diagnostics retained when an Agent fails."""

from dataclasses import dataclass, field
from typing import Literal

from .models import MetadataSnapshot


@dataclass
class AgentResult:
    snapshot: MetadataSnapshot
    status: Literal["completed", "incomplete", "failed"] = "completed"
    trace: list[dict] = field(default_factory=list)
    error: str | None = None


class AgentExecutionError(RuntimeError):
    def __init__(self, result: AgentResult):
        super().__init__(result.error)
        self.result = result
