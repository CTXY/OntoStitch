import importlib.util
import json
from pathlib import Path

import pytest

from cortex.infrastructure.llm.adaptive_limit import AdaptiveRequestGate

spec = importlib.util.spec_from_file_location(
    "online_http_concurrency",
    Path(__file__).parents[1] / "scripts/online_http_concurrency.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_live_workers_gain_slots_and_failure_immediately_reduces_them(tmp_path):
    # The existing workers retain max_limit=1; the controller can promote them
    # using shared state, without cancelling any in-flight request.
    gate = AdaptiveRequestGate(tmp_path, max_limit=1)
    token = gate.acquire()
    with gate._state() as state:
        state.update(stable_completions=4, stage_started_at=0)
    state = module.advance_admission(tmp_path, now=100)
    assert state["limit"] == 2 and token in state["active"]
    second = gate.try_acquire()
    assert second and gate.try_acquire() is None
    gate.release(token, success=False, detail="HTTP 502")
    state = json.loads(gate.path.read_text())
    assert state["limit"] == 1 and second in state["active"]
    assert gate.try_acquire() is None
    gate.release(second, success=True)
    assert json.loads(gate.path.read_text())["limit"] == 1


@pytest.mark.parametrize("block", ["success_count", "time", "cooldown", "circuit", "failure"])
def test_never_promotes_before_all_health_conditions_hold(tmp_path, block):
    gate = AdaptiveRequestGate(tmp_path, max_limit=1)
    with gate._state() as state:
        state.update(stable_completions=4, stage_started_at=0)
        if block == "success_count": state["stable_completions"] = 3
        if block == "time": state["stage_started_at"] = 99
        if block == "cooldown": state["cooldown_until"] = 101
        if block == "circuit": state["circuit_open"] = True
        if block == "failure": state["consecutive_failures"] = 1
    assert module.advance_admission(tmp_path, now=100)["limit"] == 1


def test_increases_one_stage_at_a_time_and_stops_at_cap(tmp_path):
    gate = AdaptiveRequestGate(tmp_path, max_limit=1)
    for level in range(1, 4):
        with gate._state() as state:
            state.update(stable_completions=100, stage_started_at=0)
        promoted = module.advance_admission(tmp_path, now=1000)
        assert promoted["limit"] == level + 1
        assert promoted["stable_completions"] == 0
        assert module.advance_admission(tmp_path, now=1000)["limit"] == level + 1
    with gate._state() as state:
        state.update(stable_completions=100, stage_started_at=0)
    assert module.advance_admission(tmp_path, now=1000)["limit"] == 4
