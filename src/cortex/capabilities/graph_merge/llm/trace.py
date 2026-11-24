from typing import Any, Protocol

"""Record each model request/response even when its enclosing agent fails."""

from time import monotonic
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler


class ModelBudgetExceeded(RuntimeError):
    pass


class EpisodeTrace(BaseCallbackHandler):
    raise_error = True

    def __init__(self, sink, phase, *, max_calls=None, budget=None):
        self.sink, self.phase = sink, phase
        self.budget = budget
        self.episode_id = uuid4().hex
        self.calls, self.responses = 0, []
        self.started, self.inputs = {}, []

    def record(self, event, **data):
        if self.sink:
            self.sink.record(
                {
                    "event_type": event,
                    "component": f"integration_{self.phase}",
                    "episode_id": self.episode_id,
                    **data,
                }
            )

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        if self.budget is not None:
            if self.budget["used"] >= self.budget["limit"]:
                raise ModelBudgetExceeded("Whole merge model-call budget exhausted")
            self.budget["used"] += 1
        self.calls += 1
        self.started[str(run_id)] = monotonic()
        self.inputs = list(messages[0])
        params = kwargs.get("invocation_params", {})
        self.record(
            "model_start",
            call_id=str(run_id),
            messages=[m.model_dump(mode="json") for m in self.inputs],
            tool_names=[
                (
                    t.get("function", t).get("name")
                    if isinstance(t, dict)
                    else getattr(t, "name", type(t).__name__)
                )
                for t in params.get("tools", [])
            ],
            tool_choice=params.get("tool_choice"),
        )

    def on_llm_end(self, response, *, run_id, **kwargs):
        for generation in response.generations[0]:
            message = generation.message
            self.responses.append(message)
            self.record(
                "model_end",
                call_id=str(run_id),
                elapsed_seconds=monotonic()
                - self.started.get(str(run_id), monotonic()),
                message=message.model_dump(mode="json"),
            )

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.record(
            "model_error", call_id=str(run_id), error=f"{type(error).__name__}: {error}"
        )


class GraphMergeAgentTraceSink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...
