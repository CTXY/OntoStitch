"""Run-scoped credential scheduling, shared by all SQL Training chat profiles."""

from __future__ import annotations

import time
from collections import deque
from threading import Condition
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult
from pydantic import PrivateAttr

from cortex.infrastructure.llm.providers import LLMInvocationError


class KeyScheduler:
    """One in-flight request per key, across models, threads and caller event loops."""

    def __init__(self, size: int, *, wait_timeout: float = 300.0) -> None:
        if size < 1 or wait_timeout <= 0:
            raise ValueError("key count and wait timeout must be positive")
        self._condition = Condition()
        self._available = deque(range(size))
        self._disabled: set[int] = set()
        self._cooldown = [0.0] * size
        self._size = size
        self.wait_timeout = wait_timeout

    def acquire(self, *, wait_for_case: bool = False) -> int:
        deadline = None if wait_for_case else time.monotonic() + self.wait_timeout
        with self._condition:
            while True:
                now = time.monotonic()
                for index in self._available:
                    if self._cooldown[index] <= now:
                        self._available.remove(index)
                        return index
                if len(self._disabled) == self._size:
                    raise LLMInvocationError("All LLM credential slots are disabled")
                remaining = deadline - now if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("Timed out waiting for an available LLM credential")
                cooldowns = [self._cooldown[index] - now for index in self._available]
                waits = cooldowns + ([remaining] if remaining is not None else [])
                self._condition.wait(min(waits) if waits else None)

    def slot_state(self, index: int) -> tuple[bool, float]:
        with self._condition:
            return index in self._disabled, max(0.0, self._cooldown[index] - time.monotonic())

    def disable(self, index: int) -> None:
        with self._condition:
            try:
                self._available.remove(index)
            except ValueError:
                pass
            self._disabled.add(index)
            self._condition.notify_all()

    def release(self, index: int, *, disabled: bool = False, cooldown: float = 0) -> None:
        with self._condition:
            if disabled:
                self._disabled.add(index)
            else:
                self._cooldown[index] = time.monotonic() + cooldown
                self._available.append(index)
            self._condition.notify_all()


class ThreadedChatModel(BaseChatModel):
    """Use a synchronous HTTP client even when callers use separate async event loops."""

    _model: BaseChatModel = PrivateAttr()

    def __init__(self, model: BaseChatModel) -> None:
        super().__init__(cache=False)
        self._model = model

    @property
    def _llm_type(self) -> str:
        return "cortex-threaded-chat"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        return self._model._generate(messages, stop=stop, **kwargs)

    def _get_ls_params(self, stop=None, **kwargs):
        return self._model._get_ls_params(stop=stop, **kwargs)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs: Any):
        # Ask the provider to normalize tool schemas, but bind the resulting kwargs
        # to this adapter so synchronous scheduling is never bypassed.
        bound = self._model.bind_tools(tools, tool_choice=tool_choice, **kwargs)
        return self.bind(**bound.kwargs)


class CredentialBoundEmbeddings(Embeddings):
    """Bind embedding authentication failures to the worker credential state."""

    def __init__(self, embeddings: Embeddings, scheduler: KeyScheduler) -> None:
        self._embeddings = embeddings
        self._scheduler = scheduler

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._call(self._embeddings.embed_documents, texts)

    def embed_query(self, text: str) -> list[float]:
        return self._call(self._embeddings.embed_query, text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._acall(self._embeddings.aembed_documents, texts)

    async def aembed_query(self, text: str) -> list[float]:
        return await self._acall(self._embeddings.aembed_query, text)

    def _call(self, function, value):
        try:
            return function(value)
        except Exception as exc:
            self._raise_sanitized(exc)

    async def _acall(self, function, value):
        try:
            return await function(value)
        except Exception as exc:
            self._raise_sanitized(exc)

    def _raise_sanitized(self, exc) -> None:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            self._scheduler.disable(0)
        detail = f"Embedding credential failed ({type(exc).__name__}"
        if isinstance(status, int):
            detail += f", HTTP {status}"
        raise LLMInvocationError(detail + ")") from None


class PooledChatModel(BaseChatModel):
    """Transparent non-streaming chat adapter; BaseChatModel supplies async executor support.

    The executor keeps the lease until the synchronous HTTP request actually finishes, even
    when an async caller is cancelled. No HTTP async client crosses caller event loops.
    """

    _models: tuple[BaseChatModel, ...] = PrivateAttr()
    _scheduler: KeyScheduler = PrivateAttr()
    _retries: int = PrivateAttr()

    def __init__(
        self,
        models: tuple[BaseChatModel, ...],
        scheduler: KeyScheduler,
        *,
        max_retries: int = 2,
    ) -> None:
        super().__init__(cache=False)
        if len(models) != scheduler._size or max_retries < 0:
            raise ValueError("models must match credential slots and retries must be nonnegative")
        self._models = models
        self._scheduler = scheduler
        self._retries = max_retries

    @property
    def _llm_type(self) -> str:
        return "cortex-key-pool"

    def _get_ls_params(self, stop=None, **kwargs):
        return self._models[0]._get_ls_params(stop=stop, **kwargs)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs: Any):
        bound = self._models[0].bind_tools(tools, tool_choice=tool_choice, **kwargs)
        return self.bind(**bound.kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        for attempt in range(self._retries + 1):
            index = self._scheduler.acquire()
            disabled = False
            cooldown = 0.0
            try:
                # Use the model's generation hook: the outer model owns callbacks.
                # A nested public invoke would emit unsanitized provider errors to callbacks.
                return self._models[index]._generate(messages, stop=stop, **kwargs)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                disabled = status in (401, 403)
                transient = (
                    status == 429
                    or isinstance(status, int) and status >= 500
                    or isinstance(exc, (TimeoutError, ConnectionError))
                    or type(exc).__name__ in {
                        "APIConnectionError", "APITimeoutError", "ConnectError",
                        "ConnectTimeout", "ReadTimeout", "RateLimitError",
                    }
                )
                if transient:
                    cooldown = min(2.0 ** (attempt + 1), 30.0)
                    response = getattr(exc, "response", None)
                    try:
                        retry_after = float(response.headers.get("retry-after", 0))
                        cooldown = max(cooldown, min(retry_after, self._scheduler.wait_timeout))
                    except (AttributeError, TypeError, ValueError):
                        pass
                if attempt == self._retries or not (disabled or transient):
                    # Do not chain provider errors: they can contain credentials and headers.
                    error = f"LLM credential slot {index + 1} failed"
                    error += f" ({type(exc).__name__}"
                    if isinstance(status, int):
                        error += f", HTTP {status}"
                    error += ")"
                    if transient:
                        raise TimeoutError(error) from None
                    raise LLMInvocationError(error) from None
            finally:
                self._scheduler.release(index, disabled=disabled, cooldown=cooldown)
        raise AssertionError("unreachable")
