"""Failure decisions depend on model rounds and the facts actually supplied."""

import json
from copy import deepcopy

import pytest

from cortex.capabilities.graph_merge.llm.recovery import (
    OutputProtocolRecoveryExhausted,
    RecoveryTracker,
    TaskNoProgress,
)


def test_duplicate_calls_in_one_response_are_one_failure_observation():
    tracker = RecoveryTracker()
    tracker.observe("output_schema", [{}, {"explanation": "candidate"}, {}],
                    [{"error": "missing"}, {"error": "unknown field"}, {"error": "missing"}], "facts-1")
    assert tracker.data["failure_rounds"] == 1
    assert list(tracker.data["failures"].values()) == [1]


def test_whole_decision_changes_even_when_empty_placeholders_repeat():
    tracker = RecoveryTracker()
    tracker.observe("output_schema", [{}, {"explanation": "one"}, {}], {"error": "invalid"}, "facts")
    tracker.observe("output_schema", [{}, {"explanation": "two"}, {}], {"error": "invalid"}, "facts")
    assert tracker.data["blocked"] is None


def test_failure_pairs_ignore_call_order_and_duplicate_multiplicity():
    tracker = RecoveryTracker()
    tracker.observe("output_schema", [{}, {"explanation": "same"}],
                    [{"error": "missing"}, {"error": "invalid"}], "facts")
    with pytest.raises(TaskNoProgress, match="separate model rounds"):
        tracker.observe("output_schema", [{"explanation": "same"}, {}, {}],
                        [{"error": "invalid"}, {"error": "missing"}, {"error": "missing"}], "facts")


def test_pairing_feedback_to_its_output_is_part_of_failure_identity():
    tracker = RecoveryTracker()
    outputs = [{"explanation": "first"}, {"explanation": "second"}]
    tracker.observe("output_schema", outputs, [{"error": "A"}, {"error": "B"}], "facts")
    tracker.observe("output_schema", outputs, [{"error": "B"}, {"error": "A"}], "facts")
    assert tracker.data["blocked"] is None


def test_array_order_inside_a_patch_is_not_discarded():
    tracker = RecoveryTracker()
    tracker.observe("compiler", {"targets": ["A", "B"]}, {"error": "bad"}, "facts")
    tracker.observe("compiler", {"targets": ["B", "A"]}, {"error": "bad"}, "facts")
    assert tracker.data["blocked"] is None


def test_new_visible_facts_allow_the_same_proposal_and_error_again():
    tracker = RecoveryTracker()
    tracker.observe("compiler", {"explanation": "same"}, {"error": "unknown owner"}, "before-read")
    tracker.observe("compiler", {"explanation": "same"}, {"error": "unknown owner"}, "after-source-read")
    assert tracker.data["blocked"] is None
    with pytest.raises(TaskNoProgress, match="unchanged visible facts"):
        tracker.observe("compiler", {"explanation": "same"}, {"error": "unknown owner"}, "after-source-read")


def test_returning_to_an_old_facts_bucket_does_not_erase_prior_failure():
    tracker = RecoveryTracker()
    tracker.observe("compiler", {}, {"error": "missing"}, "old-facts")
    tracker.observe("compiler", {}, {"error": "missing"}, "new-facts")
    with pytest.raises(TaskNoProgress):
        tracker.observe("compiler", {}, {"error": "missing"}, "old-facts")


def test_stage_and_feedback_changes_are_distinct_attempts():
    tracker = RecoveryTracker()
    tracker.observe("output_schema", {}, {"error": "invalid"}, "facts")
    tracker.observe("compiler", {}, {"error": "invalid"}, "facts")
    tracker.observe("compiler", {}, {"error": "more precise"}, "facts")
    assert tracker.data["blocked"] is None


def test_protocol_recovery_counts_rounds_independently_of_changing_outputs_or_facts():
    tracker = RecoveryTracker()
    tracker.observe("output_protocol", [{}, {"explanation": "first"}, {}], {"code": "multiple"}, "facts-1")
    assert not tracker.submit_only
    tracker.observe("output_protocol", [{"explanation": "different"}, {}], {"code": "multiple"}, "facts-2")
    assert tracker.submit_only
    with pytest.raises(OutputProtocolRecoveryExhausted, match="three separate model responses"):
        tracker.observe("output_protocol", [{}, {"explanation": "third"}], {"code": "multiple"}, "facts-3")
    assert tracker.data["failure_rounds"] == 3
    assert not tracker.data["failures"]


def test_schema_failure_proves_protocol_fixed_without_clearing_content_failures():
    tracker = RecoveryTracker()
    tracker.observe("output_schema", {"bad": True}, {"error": "extra field"}, "facts")
    tracker.observe("output_protocol", [{}, {}], {"error": "multiple"}, "facts")
    tracker.observe("output_protocol", [{}, {}], {"error": "multiple"}, "facts")
    assert tracker.submit_only
    with pytest.raises(TaskNoProgress):
        tracker.observe("output_schema", {"bad": True}, {"error": "extra field"}, "facts")
    assert tracker.data["protocol_failures"] == 0
    assert tracker.data["protocol_error"] is None


def test_restart_preserves_protocol_mode_fallback_and_last_error():
    tracker = RecoveryTracker()
    tracker.observe("output_protocol", [{}, {}], {"error": "multiple"}, "facts")
    tracker.observe("output_protocol", [{}, {}], {"error": "multiple"}, "facts")
    tracker.fallback = True
    encoded = json.dumps(tracker.data)
    restored = RecoveryTracker(json.loads(encoded))
    assert restored.submit_only and restored.fallback
    assert restored.data["protocol_error"] == {"error": "multiple"}
    assert restored.data["last_failure"] == tracker.data["last_failure"]
    with pytest.raises(OutputProtocolRecoveryExhausted):
        restored.observe("output_protocol", [{}, {}], {"error": "multiple"}, "facts")


def test_restart_preserves_content_failures_before_threshold():
    tracker = RecoveryTracker()
    tracker.observe("compiler", {"explanation": "same"}, {"error": "bad"}, "facts")
    restored = RecoveryTracker(json.loads(json.dumps(tracker.data)))
    with pytest.raises(TaskNoProgress):
        restored.observe("compiler", {"explanation": "same"}, {"error": "bad"}, "facts")


@pytest.mark.parametrize("stage,error_type", [
    ("output_schema", TaskNoProgress),
    ("output_protocol", OutputProtocolRecoveryExhausted),
])
def test_exhausted_restart_is_blocked_before_another_model_response(stage, error_type):
    tracker = RecoveryTracker()
    rounds = 3 if stage == "output_protocol" else 2
    for _ in range(rounds - 1):
        tracker.observe(stage, {}, {"error": "unchanged"}, "facts")
    with pytest.raises(error_type):
        tracker.observe(stage, {}, {"error": "unchanged"}, "facts")
    restored = RecoveryTracker(json.loads(json.dumps(tracker.data)))
    before = deepcopy(restored.data)
    with pytest.raises(error_type):
        restored.ensure_active()
    with pytest.raises(error_type):
        restored.observe(stage, {}, {"error": "unchanged"}, "facts")
    assert restored.data == before


def test_constructor_detaches_state_and_feedback_without_storing_output_bodies():
    tracker = RecoveryTracker()
    output = {"explanation": "A complete draft " * 200}
    feedback = {"error": "bad", "required_context": [{"id": "N"}]}
    tracker.observe("compiler", output, feedback, "facts")
    data = deepcopy(tracker.data)
    restored = RecoveryTracker(data)
    data["last_failure"]["feedback"]["required_context"].clear()
    feedback["required_context"].clear()
    assert restored.data == tracker.data
    assert len(json.dumps(tracker.data)) < 1000


def test_success_clears_repair_state_without_mutating_prior_serialized_snapshot():
    tracker = RecoveryTracker()
    tracker.observe("output_schema", {}, {"error": "missing"}, "facts")
    tracker.observe("output_protocol", [{}, {}], {"error": "multiple"}, "facts")
    tracker.fallback = True
    before = json.loads(json.dumps(tracker.data))
    tracker.success()
    assert not tracker.submit_only and not tracker.fallback
    assert not tracker.data["failures"] and tracker.data["last_failure"] is None
    assert before["last_failure"] is not None
    tracker.observe("output_schema", {}, {"error": "missing"}, "facts")


def test_new_visible_facts_unblock_content_failure_but_keep_exhausted_old_bucket():
    tracker = RecoveryTracker()
    tracker.observe("patch_compile", {}, {"error": "same"}, "old-facts")
    with pytest.raises(TaskNoProgress):
        tracker.observe("patch_compile", {}, {"error": "same"}, "old-facts")
    restored = RecoveryTracker(json.loads(json.dumps(tracker.data)))
    prior_buckets = deepcopy(restored.data['failures'])
    with pytest.raises(TaskNoProgress):
        restored.ensure_active(facts_hash="old-facts")
    restored.ensure_active(facts_hash="new-facts")
    assert restored.data['blocked'] is None
    assert restored.data['failures'] == prior_buckets
    restored.observe("patch_compile", {}, {"error": "same"}, "new-facts")
    with pytest.raises(TaskNoProgress):
        restored.observe("patch_compile", {}, {"error": "same"}, "old-facts")


def test_observe_can_unblock_content_failure_only_for_new_visible_facts():
    tracker = RecoveryTracker()
    tracker.observe("output_schema", {}, {"error": "bad"}, "old")
    with pytest.raises(TaskNoProgress):
        tracker.observe("output_schema", {}, {"error": "bad"}, "old")
    tracker.observe("output_schema", {}, {"error": "bad"}, "new")
    assert tracker.data['blocked'] is None
    with pytest.raises(TaskNoProgress):
        tracker.observe("output_schema", {}, {"error": "bad"}, "new")


def test_new_facts_never_unblock_exhausted_output_protocol():
    tracker = RecoveryTracker()
    tracker.observe("output_protocol", [{}, {}], {"error": "multiple"}, "old")
    tracker.observe("output_protocol", [{}, {}], {"error": "multiple"}, "old")
    with pytest.raises(OutputProtocolRecoveryExhausted):
        tracker.observe("output_protocol", [{}, {}], {"error": "multiple"}, "old")
    before = deepcopy(tracker.data)
    with pytest.raises(OutputProtocolRecoveryExhausted):
        tracker.ensure_active(facts_hash="new")
    with pytest.raises(OutputProtocolRecoveryExhausted):
        tracker.observe("output_schema", {}, {"error": "different"}, "new")
    assert tracker.data == before


@pytest.mark.parametrize("data", [
    [], {"version": 2}, {"failure_rounds": -1}, {"protocol_failures": True},
    {"fallback": "true"}, {"failures": {"x": 0}}, {"blocked": {"type": "other"}},
    {"protocol_failures": 3},
])
def test_invalid_recovery_state_fails_before_use(data):
    with pytest.raises((ValueError, TypeError)):
        RecoveryTracker(data)
