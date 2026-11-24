"""Advance a live shared HTTP gate without restarting its workers.

Workers own request leases and immediate failure backoff. The controller alone
raises admission; both use the same flock so no lease or failure is overwritten.
"""

import fcntl
import json
import os
import time
from pathlib import Path


DEFAULT_POLICY = {
    "max_limit": 4,
    "stages": [[4, 60], [8, 120], [12, 180]],
    "failure_action": "Worker immediately reduces to 1; 30s cooldown; two consecutive failures open circuit",
}


def advance_admission(directory, policy=None, *, now=None):
    directory = Path(directory)
    policy = DEFAULT_POLICY if policy is None else policy
    maximum = policy["max_limit"]
    stages = policy["stages"]
    if not 1 <= maximum <= len(stages) + 1:
        raise ValueError("Each increase must have a stability stage")
    if any(count < 1 or seconds < 0 for count, seconds in stages):
        raise ValueError("Stages require positive success counts and nonnegative times")
    now = time.time() if now is None else now
    with (directory / "control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "state.json"
        state = json.loads(path.read_text())
        limit = state["limit"]
        state["adaptive_policy"] = policy
        state["policy_checked_at"] = now
        if (
            not state["circuit_open"]
            and not state["consecutive_failures"]
            and now >= state["cooldown_until"]
            and limit < maximum
        ):
            count, seconds = stages[limit - 1]
            if (
                state["stable_completions"] >= count
                and now - state["stage_started_at"] >= seconds
            ):
                transition = {
                    "time": now,
                    "from": limit,
                    "to": limit + 1,
                    "reason": "controller_stable_complete_responses",
                    "stable_completions": state["stable_completions"],
                }
                state["transitions"].append(transition)
                state["limit"] += 1
                state["stable_completions"] = 0
                state["stage_started_at"] = now
                with (directory / "events.jsonl").open("a") as out:
                    out.write(json.dumps({"event": "admission_increase", **transition}) + "\n")
        temporary = path.with_name(f".state.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n")
        temporary.replace(path)
        return state
