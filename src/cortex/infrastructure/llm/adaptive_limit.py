"""Bound actual sync/async HTTP exchanges across local worker processes."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx


class AdmissionPaused(httpx.TransportError):
    """The endpoint circuit is open; no request was dispatched."""


class InvalidCompletion(httpx.TransportError):
    """A response did not finish in the explicitly requested model mode."""

    error_code = "provider_response_invalid"
    error_stage = "provider_response"
    retryable = False


class EmptyCompletion(InvalidCompletion):
    """A completed HTTP response has no answer; permit a bounded retry."""

    error_code = "provider_empty_completion"
    retryable = True


class TruncatedCompletion(InvalidCompletion):
    """A local output exceeded its budget; keep it out of EA."""

    error_code = "provider_output_truncated"


class CredentialUnavailable(httpx.TransportError):
    error_stage = "provider_response"
    retryable = False

    def __init__(self, status):
        self.error_code = "provider_quota_error" if status == 402 else "authentication_error"
        super().__init__(f"Worker credential disabled after HTTP {status}")


class AdaptiveRequestGate:
    def __init__(self, directory, *, max_limit=3, stages=((8, 120), (16, 180)),
                 min_long_completions=0, long_prompt_tokens=16000, allow_provider_reasoning=False,
                 initial_limit=1, min_limit=1, decrease_factor=None, failure_threshold=2,
                 cooldown_seconds=30, auto_recover_seconds=0, isolate_credentials=False,
                 wire_request_mode="explicit_none"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"
        self.max_limit = max_limit
        self.stages = stages
        self.min_long_completions = min_long_completions
        self.long_prompt_tokens = long_prompt_tokens
        self.allow_provider_reasoning = allow_provider_reasoning
        self.initial_limit, self.min_limit = initial_limit, min_limit
        self.decrease_factor, self.failure_threshold = decrease_factor, failure_threshold
        self.cooldown_seconds, self.auto_recover_seconds = cooldown_seconds, auto_recover_seconds
        self.isolate_credentials = isolate_credentials
        if wire_request_mode not in ("explicit_none","native_disabled"):
            raise ValueError("Unknown no-thinking request mode")
        self.wire_request_mode = wire_request_mode
        if max_limit < 1 or max_limit > len(stages) + 1:
            raise ValueError("Each admission increase requires a stability stage")
        if not 1 <= min_limit <= initial_limit <= max_limit:
            raise ValueError("Admission limits must satisfy minimum <= initial <= maximum")
        with self._state():
            pass

    @contextmanager
    def _state(self):
        with (self.directory / "control.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {
                    "limit": self.initial_limit,
                    "active": {},
                    "peak_active": 0,
                    "started": 0,
                    "completed": 0,
                    "failed": 0,
                    "consecutive_failures": 0,
                    "stable_completions": 0,
                    "stage_started_at": time.time(),
                    "cooldown_until": 0,
                    "circuit_open": False,
                    "transitions": [],
                }
            )
            try:
                yield state
            finally:
                temporary = self.path.with_name(f".state.{os.getpid()}.tmp")
                temporary.write_text(json.dumps(state, indent=2) + "\n")
                temporary.replace(self.path)
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _event(self, event):
        with (self.directory / "events.jsonl").open("a") as out:
            out.write(json.dumps(event) + "\n")

    def disable_credential(self, fingerprint, status):
        with self._state() as state:
            state.setdefault("disabled_keys", {})[fingerprint] = {"http_status":status,"at":time.time()}

    def try_acquire(self, credential_fingerprint=None):
        with self._state() as state:
            disabled = state.get("disabled_keys", {}).get(credential_fingerprint)
            if disabled:
                raise CredentialUnavailable(disabled["http_status"])
            for token, lease in list(state["active"].items()):
                try:
                    os.kill(lease["pid"], 0)
                except ProcessLookupError:
                    del state["active"][token]
            if state["circuit_open"]:
                if self.auto_recover_seconds and not state.get("circuit_fatal"):
                    if time.time() < state["cooldown_until"]:
                        return None
                    state.update(circuit_open=False, consecutive_failures=0, limit=self.min_limit,
                                 stable_completions=0, stage_started_at=time.time())
                    state["transitions"].append({"time":time.time(),"from":0,"to":self.min_limit,"reason":"transport_cooldown_recovery"})
                else:
                    raise AdmissionPaused(
                        "Endpoint admission paused: " + state.get("pause_reason", "")
                    )
            if (
                time.time() < state["cooldown_until"]
                or len(state["active"]) >= state["limit"]
            ):
                return None
            token = uuid4().hex
            state["active"][token] = {"pid": os.getpid(), "started_at": time.time()}
            state["started"] += 1
            state["peak_active"] = max(state["peak_active"], len(state["active"]))
            self._event(
                {
                    "event": "start",
                    "time": time.time(),
                    "token": token,
                    "pid": os.getpid(),
                    "active": len(state["active"]),
                    "limit": state["limit"],
                }
            )
            return token

    def acquire(self, credential_fingerprint=None):
        while True:
            token = self.try_acquire(credential_fingerprint)
            if token:
                return token
            time.sleep(0.1)

    async def acquire_async(self, credential_fingerprint=None):
        while True:
            token = self.try_acquire(credential_fingerprint)
            if token:
                return token
            await asyncio.sleep(0.1)

    def release(self, token, *, success, detail="", fatal=False, prompt_tokens=0, isolated_credential=False):
        with self._state() as state:
            lease = state["active"].pop(token)
            now = time.time()
            if detail == "CancelledError":
                state["cancelled"] = state.get("cancelled", 0) + 1
                self._event({"event":"cancelled", "time":now, "token":token,
                             "pid":os.getpid(), "seconds":now-lease["started_at"],
                             "active":len(state["active"]), "limit":state["limit"]})
                return
            state["completed" if success else "failed"] += 1
            if isolated_credential:
                state["credential_failures"] = state.get("credential_failures", 0) + 1
            elif success:
                if detail == "complete_with_provider_reasoning":
                    state["provider_reasoning_responses"] = state.get("provider_reasoning_responses", 0) + 1
                state["consecutive_failures"] = 0
                state["stable_completions"] += 1
                if prompt_tokens >= self.long_prompt_tokens:
                    state["stable_long_completions"] = state.get("stable_long_completions", 0) + 1
                    state["last_long_completion_at"] = now
                if state["limit"] < self.max_limit and not state["circuit_open"]:
                    count, seconds = self.stages[state["limit"] - 1]
                    if (
                        state["stable_completions"] >= count
                        and now - state["stage_started_at"] >= seconds
                        and state.get("stable_long_completions", 0) >= self.min_long_completions
                    ):
                        state["transitions"].append(
                            {
                                "time": now,
                                "from": state["limit"],
                                "to": state["limit"] + 1,
                                "reason": "stable_complete_responses",
                            }
                        )
                        state["limit"] += 1
                        state["stable_completions"] = 0
                        state["stage_started_at"] = now
            else:
                state["consecutive_failures"] += 1
                state["stable_completions"] = 0
                state["stable_long_completions"] = 0
                reduced = max(self.min_limit, int(state["limit"] * self.decrease_factor)) if self.decrease_factor else 1
                state["transitions"].append(
                    {"time": now, "from": state["limit"], "to": reduced, "reason": detail}
                )
                state["limit"] = reduced
                state["stage_started_at"] = now
                state["cooldown_until"] = now + self.cooldown_seconds
                if fatal or state["consecutive_failures"] >= self.failure_threshold:
                    state["circuit_open"] = True
                    state["circuit_fatal"] = fatal
                    state["cooldown_until"] = now + max(self.cooldown_seconds,self.auto_recover_seconds)
                    state["pause_reason"] = detail
            self._event(
                {
                    "event": "complete" if success else "error",
                    "time": now,
                    "token": token,
                    "pid": os.getpid(),
                    "seconds": now - lease["started_at"],
                    "detail": detail,
                    "prompt_tokens": prompt_tokens,
                    "active": len(state["active"]),
                    "limit": state["limit"],
                    "circuit_open": state["circuit_open"],
                }
            )


def _check_response(response, *, allow_provider_reasoning=False):
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}", response.status_code in (401, 402, 403)
    try:
        choice = response.json()["choices"][0]
        message = choice["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise InvalidCompletion("Completion envelope is incomplete") from exc
    if choice.get("finish_reason") == "length":
        raise TruncatedCompletion("Completion exceeded its output token budget")
    if choice.get("finish_reason") not in ("stop", "tool_calls"):
        raise InvalidCompletion("Completion did not terminate normally")
    if not message.get("content") and not message.get("tool_calls"):
        raise EmptyCompletion("Completion has no final content")
    if message.get("reasoning_content"):
        if not allow_provider_reasoning:
            raise InvalidCompletion("Explicit no-thinking mode returned reasoning content")
        return True, "complete_with_provider_reasoning", False
    return True, "complete_no_reasoning", False


def install_adaptive_gate(client, gate, *, asynchronous=False, max_retries=0):
    """Retry the same HTTP request, releasing its admission slot between attempts."""
    if getattr(client, "_cortex_adaptive_gate", False):
        return
    original = client.send

    def check_request(request):
        data = json.loads(request.content)
        valid=("reasoning_effort" not in data and data.get("thinking",{}).get("type")=="disabled") if gate.wire_request_mode=="native_disabled" else data.get("reasoning_effort")=="none"
        if not valid or data.get("stream"):
            raise InvalidCompletion("Request does not match the configured no-thinking wire mode")
        credential = request.headers.get("authorization", "").removeprefix("Bearer ")
        return hashlib.sha256(credential.encode()).hexdigest()[:12] if credential else None

    def response_outcome(response, fingerprint):
        outcome = _check_response(response, allow_provider_reasoning=gate.allow_provider_reasoning)
        isolated = gate.isolate_credentials and response.status_code in (401, 402, 403)
        if isolated:
            gate.disable_credential(fingerprint, response.status_code)
            outcome = (False, outcome[1], False)
        tokens = ((response.json().get("usage") or {}).get("prompt_tokens") or 0) if outcome[0] else 0
        retry = response.status_code in (408, 429, 500, 502, 503, 504)
        return outcome, tokens, isolated, retry

    def retry_delay(attempt, fingerprint, detail):
        delay = min(2 * 2 ** attempt, 30)
        with gate._state() as state:
            state["http_retries"] = state.get("http_retries", 0) + 1
        gate._event({"event":"retry_scheduled", "time":time.time(), "pid":os.getpid(),
                     "attempt":attempt+2, "max_attempts":max_retries+1, "delay_seconds":delay,
                     "key_fingerprint":fingerprint, "detail":detail})
        return delay

    def send(request, *args, **kwargs):
        fingerprint = check_request(request)
        for attempt in range(max_retries + 1):
            token = gate.acquire(fingerprint)
            outcome = (False, "transport_interrupted", False)
            prompt_tokens, isolated, retry, caught, response = 0, False, False, None, None
            try:
                response = original(request, *args, **kwargs)
                response.read()
                outcome, prompt_tokens, isolated, retry = response_outcome(response, fingerprint)
            except EmptyCompletion as exc:
                outcome, caught, retry = (False, str(exc), False), exc, True
            except InvalidCompletion as exc:
                outcome, caught = (False, str(exc), not isinstance(exc, TruncatedCompletion)), exc
            except httpx.TransportError as exc:
                outcome, caught, retry = (False, type(exc).__name__, False), exc, True
            except BaseException as exc:
                outcome = (False, type(exc).__name__, False)
                raise
            finally:
                gate.release(token, success=outcome[0], detail=outcome[1], fatal=outcome[2],
                             prompt_tokens=prompt_tokens, isolated_credential=isolated)
            if retry and attempt < max_retries:
                if response is not None:
                    response.close()
                time.sleep(retry_delay(attempt, fingerprint, outcome[1]))
                continue
            if caught is not None:
                raise caught
            return response

    async def async_send(request, *args, **kwargs):
        fingerprint = check_request(request)
        for attempt in range(max_retries + 1):
            token = await gate.acquire_async(fingerprint)
            outcome = (False, "transport_interrupted", False)
            prompt_tokens, isolated, retry, caught, response = 0, False, False, None, None
            try:
                response = await original(request, *args, **kwargs)
                await response.aread()
                outcome, prompt_tokens, isolated, retry = response_outcome(response, fingerprint)
            except EmptyCompletion as exc:
                outcome, caught, retry = (False, str(exc), False), exc, True
            except InvalidCompletion as exc:
                outcome, caught = (False, str(exc), not isinstance(exc, TruncatedCompletion)), exc
            except httpx.TransportError as exc:
                outcome, caught, retry = (False, type(exc).__name__, False), exc, True
            except BaseException as exc:
                outcome = (False, type(exc).__name__, False)
                raise
            finally:
                gate.release(token, success=outcome[0], detail=outcome[1], fatal=outcome[2],
                             prompt_tokens=prompt_tokens, isolated_credential=isolated)
            if retry and attempt < max_retries:
                if response is not None:
                    await response.aclose()
                await asyncio.sleep(retry_delay(attempt, fingerprint, outcome[1]))
                continue
            if caught is not None:
                raise caught
            return response

    client.send = async_send if asynchronous else send
    client._cortex_adaptive_gate = True
