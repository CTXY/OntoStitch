"""Behavioral checks for direct retention and the unchanged stable replay gate."""

import json
import unittest
from pathlib import Path

from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.training.initial.flow import run_initial_flow
from cortex.capabilities.sql_training.training.replay import (
    ReplayAttempt,
    run_stable_replay,
)


def rule():
    return NodeAsset.model_validate(
        {
            "id": "generation_rule.count",
            "node_type": "generation_rule",
            "name": "Count",
            "properties": {
                "triggers": {"question": {"phrases": ["how many"]}},
                "condition": "The request asks for a count.",
                "action": "Count the requested records.",
                "reason": "Counting returns the number of records.",
            },
        }
    )


class InitialRetentionTests(unittest.TestCase):
    def run_flow(self, evaluations, assets=None):
        self.calls = []
        self.published = []
        self.traces = []

        def replay(stage, working):
            self.calls.append((stage, [item.id for item in working]))
            return run_stable_replay(
                lambda n: ReplayAttempt(
                    attempt=n, trace_id=f"test-{n}", evaluation=evaluations[n - 1]
                ),
                required_correct_attempts=3,
            )

        return run_initial_flow(
            record={
                "source_id": "test",
                "question": "How many?",
                "sql": "SELECT COUNT(*) FROM t",
            },
            staged_assets=[rule()] if assets is None else assets,
            provider=object(),
            replay=replay,
            publish_working=lambda items: self.published.append(items),
            trace=lambda **event: self.traces.append(event),
        )

    def test_success_retains_unselected_knowledge_without_assessment_or_second_gate(
        self,
    ):
        result = self.run_flow([{"correct": True, "query": "SELECT 1"}] * 3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(result.events[0]["replay_attempts"]), 3)
        self.assertEqual(result.accepted_asset_ids, [rule().id])
        self.assertEqual(
            [item.id for item in result.working_assets], result.accepted_asset_ids
        )
        self.assertFalse(result.requires_repair)
        self.assertIsNone(result.failure_reason)
        self.assertFalse(self.traces[0]["summary"]["semantic_assessment_performed"])

    def test_first_failure_after_success_goes_directly_to_diagnosis(self):
        result = self.run_flow(
            [
                {"correct": True, "query": "SELECT 1"},
                {"correct": False, "query": "SELECT 2"},
            ]
        )
        self.assertTrue(result.requires_repair)
        self.assertEqual(result.evaluation["query"], "SELECT 2")
        self.assertEqual(result.evaluation["first_failure_attempt"], 2)
        self.assertEqual(result.accepted_asset_ids, [rule().id])
        self.assertEqual(len(self.calls), 1)

    def test_unavailable_preserves_knowledge_without_sql_diagnosis(self):
        result = self.run_flow(
            [{"correct": False, "evaluation_available": False, "query": "SELECT 1"}]
        )
        self.assertFalse(result.requires_repair)
        self.assertEqual(result.accepted_asset_ids, [rule().id])
        self.assertEqual(
            result.failure_reason, "initial_working_evaluation_unavailable"
        )

    def test_generation_failure_preserves_knowledge(self):
        result = self.run_flow(
            [
                {
                    "correct": False,
                    "query": "",
                    "candidate_failure": {"kind": "generation"},
                }
            ]
        )
        self.assertFalse(result.requires_repair)
        self.assertEqual(result.accepted_asset_ids, [rule().id])
        self.assertIsNotNone(result.failure_reason)

    def test_empty_extraction_still_evaluates_physical_graph(self):
        result = self.run_flow([{"correct": True, "query": "SELECT 1"}] * 3, assets=[])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result.working_assets, [])
        self.assertTrue(result.evaluation["correct"])

    def test_duplicate_initial_ids_fail_before_replay(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.run_flow([], assets=[rule(), rule()])
        self.assertEqual(self.calls, [])


def test_direct_retention_export_and_evaluation_match(case_graph):
    from test_agent_controller import EditingAgent, make_controller

    from cortex.capabilities.sql_training.artifacts.cases import case_result, seal_case

    ctrl, services, _ = make_controller(case_graph, EditingAgent([[rule()]]))
    state = ctrl.run()
    result = seal_case(
        ctrl.root,
        case_result(ctrl.record, state, ctrl.graph, services, ctrl.root),
        ctrl.graph,
    )
    saved = json.loads((Path(result["bundle_path"]) / "case_result.json").read_text())
    assert result["directly_retained_asset_ids"] == [rule().id]
    assert result["assessment_accepted_asset_ids"] == []
    assert result["draft_node_ids"] == [rule().id]
    assert (
        result["evaluation_ontology_fingerprint"]
        == result["final_ontology_fingerprint"]
    )
    assert saved["completion_reason"] == "stable_correct"


def test_unavailable_status_preserves_retained_knowledge(case_graph):
    from test_agent_controller import EditingAgent, Validator, make_controller

    from cortex.capabilities.sql_training.artifacts.cases import case_result

    class UnavailableValidator(Validator):
        def retrieve(self, **kwargs):
            raise ConnectionError("offline")

    ctrl, services, _ = make_controller(
        case_graph, EditingAgent([[rule()]]), UnavailableValidator()
    )
    state = ctrl.run()
    result = case_result(ctrl.record, state, ctrl.graph, services, ctrl.root)
    assert not result["correct"]
    assert result["validation_status"] == "unavailable"
    assert result["draft_node_count"] == 1
    assert result["asset_outcome"] == "partial"


if __name__ == "__main__":
    unittest.main()
