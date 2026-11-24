"""Checkpointable failure recovery for completed logical model responses."""

import hashlib
import json
from copy import deepcopy


class TaskNoProgress(RuntimeError):
    """The same rejected decision recurred with the same visible premises."""

    retryable = False


class OutputProtocolRecoveryExhausted(RuntimeError):
    """The bounded output-protocol repair opportunities have been consumed."""

    retryable = False


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _detached(value):
    return json.loads(_json(value))


def _output_set(output, feedback):
    """Canonicalize a response's output/error pairs, not arrays inside a Patch.

    A list of outputs denotes a batch. A matching feedback list is paired by
    position; otherwise each output receives the shared feedback object. A raw
    output that is itself an array must therefore be supplied as ``[raw]``.
    Provider call IDs are intentionally absent from this API.
    """
    outputs = output if isinstance(output, list) else [output]
    paired = isinstance(feedback, list) and len(feedback) == len(outputs)
    return sorted({_json([value, feedback[index] if paired else feedback])
                   for index, value in enumerate(outputs)})


class RecoveryTracker:
    """Count one failure observation per complete response, with its premises.

    ``facts_hash`` must describe the facts actually sent *before* that response,
    including the relevant task/policy versions. Neither transport attempts nor
    response-generated facts are observations of a new model decision.

    ``data`` is detached JSON on construction and can be serialized directly
    into TaskState. It intentionally stores only output signatures, feedback
    and counters, not extra full Patch or graph bodies. Callers must persist
    it even when ``observe`` raises. ``ensure_active`` restores a terminal
    condition without another model call.
    """

    def __init__(self, data=None):
        restored = _detached(data) if data is not None else {}
        if not isinstance(restored, dict):
            raise TypeError("Recovery state must be an object")
        defaults = {
            "version": 1, "failure_rounds": 0, "protocol_failures": 0,
            "protocol_error": None, "fallback": False, "failures": {},
            "last_failure": None, "blocked": None,
        }
        self.data = {**defaults, **restored}
        if self.data["version"] != 1:
            raise ValueError("Unsupported recovery-state version")
        for key in ("failure_rounds", "protocol_failures"):
            if type(self.data[key]) is not int or self.data[key] < 0:
                raise ValueError(f"Invalid recovery counter {key}")
        if type(self.data["fallback"]) is not bool or not isinstance(self.data["failures"], dict):
            raise ValueError("Invalid recovery mode or failure signatures")
        if any(not isinstance(key, str) or type(count) is not int or count < 1
               for key, count in self.data["failures"].items()):
            raise ValueError("Invalid recovery failure count")
        blocked = self.data["blocked"]
        if blocked is not None and (
            not isinstance(blocked, dict) or blocked.get("type") not in {
                "TaskNoProgress", "OutputProtocolRecoveryExhausted"}
            or not isinstance(blocked.get("message"), str)
        ):
            raise ValueError("Invalid blocked recovery state")
        if self.data["protocol_failures"] >= 3 and blocked is None:
            raise ValueError("Exhausted protocol recovery lacks its blocked reason")

    @property
    def submit_only(self):
        return self.data["protocol_failures"] >= 2

    @property
    def fallback(self):
        return self.data["fallback"]

    @fallback.setter
    def fallback(self, value):
        if type(value) is not bool:
            raise ValueError("Fallback mode must be a boolean")
        self.data["fallback"] = value

    def ensure_active(self, facts_hash=None):
        if facts_hash is not None and (not isinstance(facts_hash, str) or not facts_hash):
            raise ValueError("Visible facts hash must be a nonempty string")
        blocked = self.data["blocked"]
        if blocked is not None:
            last = self.data.get("last_failure") or {}
            previous_facts = last.get("facts_hash") if isinstance(last, dict) else None
            if (blocked["type"] == "TaskNoProgress" and facts_hash is not None
                    and isinstance(previous_facts, str) and previous_facts
                    and facts_hash != previous_facts):
                # An explicit new set of injected premises grants another
                # content-repair opportunity. Keep every old failure bucket:
                # returning to its old premises must not restart that loop.
                self.data["blocked"] = None
                return
            exception = (OutputProtocolRecoveryExhausted if blocked["type"] ==
                         "OutputProtocolRecoveryExhausted" else TaskNoProgress)
            raise exception(blocked["message"])

    def observe(self, stage, output, feedback, facts_hash):
        if not isinstance(stage, str) or not stage or not isinstance(facts_hash, str) or not facts_hash:
            raise ValueError("Failure stage and visible facts hash are required")
        self.ensure_active(facts_hash=facts_hash)
        feedback = _detached(feedback)
        output_set = _output_set(output, feedback)
        signature = hashlib.sha256(_json([stage, output_set, facts_hash]).encode()).hexdigest()
        self.data["failure_rounds"] += 1
        self.data["last_failure"] = {
            "stage": stage, "feedback": feedback, "facts_hash": facts_hash,
            "signature": signature,
        }
        if stage == "output_protocol":
            self.data["protocol_failures"] += 1
            self.data["protocol_error"] = deepcopy(feedback)
            if self.data["protocol_failures"] >= 3:
                self.data["blocked"] = {
                    "type": "OutputProtocolRecoveryExhausted", "stage": stage,
                    "message": ("Output protocol repair exhausted after three separate model responses; "
                                "the prior draft and checkpoint are retained"),
                }
        else:
            # Reaching schema/compiler validation proves the single-result
            # protocol was repaired. It must not clear content-failure history.
            self.data["protocol_failures"] = 0
            self.data["protocol_error"] = None
            count = self.data["failures"].get(signature, 0) + 1
            self.data["failures"][signature] = count
            if count >= 2:
                self.data["blocked"] = {
                    "type": "TaskNoProgress", "stage": stage,
                    "message": ("The identical draft/output set and rejection repeated across separate "
                                f"model rounds with unchanged visible facts ({stage}); checkpoint retained"),
                }
        self.ensure_active()

    def success(self):
        """Complete an invocation; callers must not use this for read-only turns."""
        self.ensure_active()
        self.data.update(protocol_failures=0, protocol_error=None, fallback=False,
                         failures={}, last_failure=None)
