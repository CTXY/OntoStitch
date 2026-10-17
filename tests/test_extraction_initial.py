"""Behavioral checks for direct retention and the unchanged stable replay gate."""
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.training.initial.flow import run_initial_flow
from cortex.capabilities.sql_training.training.replay import ReplayAttempt, run_stable_replay


def rule():
    return NodeAsset.model_validate({
        "id": "generation_rule.count", "node_type": "generation_rule", "name": "Count",
        "properties": {"triggers": {"question": {"phrases": ["how many"]}},
                       "condition": "The request asks for a count.",
                       "action": "Count the requested records.",
                       "reason": "Counting returns the number of records."},
    })


class InitialRetentionTests(unittest.TestCase):
    def run_flow(self, evaluations, assets=None):
        self.calls = []
        self.published = []
        self.traces = []

        def replay(stage, working):
            self.calls.append((stage, [item.id for item in working]))
            return run_stable_replay(
                lambda n: ReplayAttempt(attempt=n, trace_id=f"test-{n}", evaluation=evaluations[n-1]),
                required_correct_attempts=3,
            )

        return run_initial_flow(
            record={"source_id": "test", "question": "How many?", "sql": "SELECT COUNT(*) FROM t"},
            staged_assets=[rule()] if assets is None else assets,
            provider=object(), replay=replay,
            publish_working=lambda items: self.published.append(items),
            trace=lambda **event: self.traces.append(event),
        )

    def test_success_retains_unselected_knowledge_without_assessment_or_second_gate(self):
        result = self.run_flow([{"correct": True, "query": "SELECT 1"}] * 3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(result.events[0]["replay_attempts"]), 3)
        self.assertEqual(result.accepted_asset_ids, [rule().id])
        self.assertEqual([item.id for item in result.working_assets], result.accepted_asset_ids)
        self.assertFalse(result.requires_repair)
        self.assertIsNone(result.failure_reason)
        self.assertFalse(self.traces[0]["summary"]["semantic_assessment_performed"])

    def test_first_failure_after_success_goes_directly_to_diagnosis(self):
        result = self.run_flow([
            {"correct": True, "query": "SELECT 1"},
            {"correct": False, "query": "SELECT 2"},
        ])
        self.assertTrue(result.requires_repair)
        self.assertEqual(result.evaluation["query"], "SELECT 2")
        self.assertEqual(result.evaluation["first_failure_attempt"], 2)
        self.assertEqual(result.accepted_asset_ids, [rule().id])
        self.assertEqual(len(self.calls), 1)

    def test_unavailable_preserves_knowledge_without_sql_diagnosis(self):
        result = self.run_flow([{"correct": False, "evaluation_available": False, "query": "SELECT 1"}])
        self.assertFalse(result.requires_repair)
        self.assertEqual(result.accepted_asset_ids, [rule().id])
        self.assertEqual(result.failure_reason, "initial_working_evaluation_unavailable")

    def test_generation_failure_preserves_knowledge(self):
        result = self.run_flow([{"correct": False, "query": "", "candidate_failure": {"kind": "generation"}}])
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


class ExportConsistencyTests(unittest.TestCase):
    def export_case(self, *, accepted=True, available=True):
        from cortex.capabilities.sql_training.pipeline.train import (
            TrainPipeline, _joint_case_result, _write_semantic_draft_assets,
        )
        from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
        from cortex.capabilities.sql_training.training.repair.contracts import ontology_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            runner = TrainPipeline(SqlTrainingRuntimeConfig(workspace=tmp))
            runner.workspace.init()
            record = {"source_id": "export-test", "question": "How many?", "sql": "SELECT 1"}

            def run_working(*args, **kwargs):
                _write_semantic_draft_assets(runner.workspace, [rule()])
                return _joint_case_result(
                    record,
                    {"correct": available, "query": "SELECT 1", "evaluation_available": available,
                     "replay_status": "stable_correct" if available else "unavailable",
                     "required_correct_replays": 3, "completed_replays": 3 if available else 1,
                     "ontology_fingerprint": ontology_fingerprint([rule()])},
                    [{"stage": "initial_direct_extraction", "retained_asset_ids": [rule().id]}],
                    accepted_rule_ids=[rule().id] if accepted else [],
                    failed_closed=not available,
                )

            runner._run_case_joint_working = Mock(side_effect=run_working)
            result = runner._run_case_joint(
                record, provider=Mock(), max_iterations=1, sql_timeout=30,
                trace_prefix="test", prepared_path=tmp, run_id="test", case_index=1,
                trace_logger=Mock(),
            )
            saved = json.loads((Path(result["bundle_path"]) / "case_result.json").read_text())
            nodes = json.loads((Path(result["bundle_path"]) / "draft_nodes.json").read_text())
            return result, saved, nodes

    def test_direct_retention_ids_export_and_evaluation_match(self):
        result, saved, nodes = self.export_case()
        self.assertEqual(result["directly_retained_asset_ids"], [rule().id])
        self.assertEqual(result["assessment_accepted_asset_ids"], [])
        self.assertEqual(result["draft_node_ids"], [rule().id])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(result["evaluation_ontology_fingerprint"], result["final_ontology_fingerprint"])
        self.assertEqual(saved["completion_reason"], "stable_correct")

    def test_export_filter_mismatch_cannot_report_success(self):
        result, saved, nodes = self.export_case(accepted=False)
        self.assertFalse(result["correct"])
        self.assertFalse(result["case_stable_correct"])
        self.assertEqual(saved["failure_reason"], "export_ontology_evaluation_mismatch")
        self.assertEqual(nodes, [])

    def test_unavailable_status_survives_export_without_discarding_knowledge(self):
        result, saved, nodes = self.export_case(available=False)
        self.assertFalse(result["evaluation_available"])
        self.assertEqual(saved["completion_reason"], "oracle_unavailable")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(result["asset_outcome"], "partial")


if __name__ == "__main__":
    unittest.main()
