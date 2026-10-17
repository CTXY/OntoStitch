"""Exercise real trial materialization and acceptance at the repair round boundary."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.training.repair import cycle as module
from cortex.capabilities.sql_training.training.repair.plan import (
    CandidateAsset,
    CandidateInitializationResult,
    HeavyRoundPlan,
)
from cortex.capabilities.sql_training.training.repair.trial import CaseWorkingOntology


def evaluation(query, correct=False, **extra):
    return {"query": query, "correct": correct, "evaluation_available": True, **extra}


class RepairRoundBoundaryTests(unittest.TestCase):
    def make_cycle(self, accepted=("a",), trial=None, final=None):
        plan = HeavyRoundPlan.model_validate({
            "round_id": "r1", "diagnostic_sql": "initial SQL",
            "targets": [
                {"target_id": name, "error_key": name, "repair_instruction": "fix", "evidence": "SQL"}
                for name in ("a", "b")
            ],
            "owner_plans": [
                {"target_id": name, "owner": "semantic_mapping", "required_assets": [
                    {"requirement_id": name, "operation": "create", "instruction": "fix"}
                ]} for name in ("a", "b")
            ],
        })

        def initialize(group, *_):
            name = group.target_ids[0]
            asset = NodeAsset.model_validate({
                "id": f"concept.{name}", "node_type": "concept", "name": name,
                "properties": {"targets": []},
            })
            return CandidateInitializationResult(
                candidates=[CandidateAsset(candidate_id=name, asset=asset)],
                candidate_id_by_requirement={name: name},
            )

        assessed = SimpleNamespace(
            candidate_assessments=[SimpleNamespace(
                candidate_id=name, problem="none", application="applied", evidence="used",
                regressed_target_ids=(),
            ) for name in ("a", "b")],
            # Intentionally stale unresolved targets: final execution must supersede
            # this bookkeeping when the retained graph becomes stable correct.
            target_assessments=[SimpleNamespace(target_id=name, status="unresolved")
                                for name in ("a", "b")],
        )
        decision = SimpleNamespace(accept_candidate_ids=accepted, close_reason=None,
                                   refine_candidate_ids=())
        analyze = Mock(return_value=[{"error": "mismatch"}])
        execute_trial = Mock(return_value=trial or evaluation("trial SQL"))
        execute_final = Mock(return_value=final or evaluation("working SQL"))
        runner = module.RepairCycle(
            analyze_errors=analyze, build_plan=Mock(return_value=plan),
            initialize=initialize, execute_trial=execute_trial,
            execute_final=execute_final, assess_trial=Mock(return_value=assessed),
            refine_candidate=Mock(),
        )
        return runner, decision

    def run_cycle(self, runner, decision, max_rounds=1):
        with patch.object(module, "decide_joint_iteration", return_value=decision):
            return runner.run(initial_evaluation=evaluation("initial SQL"),
                              working=CaseWorkingOntology(), max_rounds=max_rounds,
                              max_iterations=1)

    def test_discarded_candidate_replayed_on_retained_graph(self):
        runner, decision = self.make_cycle()
        result = self.run_cycle(runner, decision)
        self.assertEqual([a.id for a in runner.execute_trial.call_args.args[0].assets],
                         ["concept.a", "concept.b"])
        self.assertEqual([a.id for a in runner.execute_final.call_args.args[0].assets],
                         ["concept.a"])
        self.assertEqual(result.evaluation["query"], "working SQL")
        self.assertEqual([a.id for a in result.working.assets], ["concept.a"])

    def test_same_graph_incorrect_evidence_is_reused(self):
        runner, decision = self.make_cycle(accepted=("a", "b"))
        result = self.run_cycle(runner, decision)
        runner.execute_final.assert_not_called()
        self.assertEqual(result.evaluation["query"], "trial SQL")
        self.assertFalse(result.completed)

    def test_working_success_clears_remaining_targets(self):
        runner, decision = self.make_cycle(final=evaluation("working SQL", True))
        result = self.run_cycle(runner, decision)
        self.assertTrue(result.completed)
        self.assertEqual(result.remaining_target_ids, ())
        self.assertIsNone(result.failure_reason)

    def test_all_accepted_correct_preserves_extra_final_replay(self):
        runner, decision = self.make_cycle(accepted=("a", "b"),
                                          trial=evaluation("trial SQL", True),
                                          final=evaluation("working SQL", True))
        result = self.run_cycle(runner, decision)
        runner.execute_final.assert_called_once()
        self.assertTrue(result.completed)

    def test_next_diagnosis_uses_retained_graph_sql(self):
        runner, decision = self.make_cycle()
        runner.analyze_errors.side_effect = [[{"error": "mismatch"}], []]
        self.run_cycle(runner, decision, max_rounds=2)
        self.assertEqual(runner.analyze_errors.call_args.args[0]["query"], "working SQL")
        self.assertEqual([a.id for a in runner.analyze_errors.call_args.args[1].assets],
                         ["concept.a"])

    def test_missing_final_executor_does_not_reuse_discarded_trial_sql(self):
        runner, decision = self.make_cycle()
        runner.execute_final = None
        result = self.run_cycle(runner, decision, max_rounds=2)
        self.assertEqual(result.failure_reason, "working_replay_required")
        self.assertEqual(result.evaluation["query"], "")
        self.assertFalse(result.evaluation["evaluation_available"])
        runner.analyze_errors.assert_called_once()

    def test_final_execution_failures_do_not_enter_diagnosis(self):
        failures = [
            (evaluation("SQL", evaluation_available=False), "evaluation_unavailable"),
            (evaluation("", evaluation_available=False), "evaluation_unavailable"),
            (evaluation("", candidate_failure={"kind": "generation"}), "candidate_generation_failed"),
            (evaluation("", candidate_failure={"kind": "ontology_contract"}), "ontology_contract_failed"),
        ]
        for final, reason in failures:
            with self.subTest(reason=reason):
                runner, decision = self.make_cycle(final=final)
                result = self.run_cycle(runner, decision, max_rounds=2)
                self.assertEqual(result.failure_reason, reason)
                runner.analyze_errors.assert_called_once()

    def test_unavailable_trial_bypasses_assessment_and_replays_working(self):
        runner, decision = self.make_cycle(
            trial=evaluation("SQL", evaluation_available=False),
            final=evaluation("working SQL", True),
        )
        result = self.run_cycle(runner, decision)
        runner.assess_trial.assert_not_called()
        self.assertEqual(runner.execute_final.call_args.args[0].assets, [])
        self.assertTrue(result.completed)

    def test_initial_unavailable_does_not_enter_diagnosis(self):
        runner, _ = self.make_cycle()
        result = runner.run(initial_evaluation=evaluation("SQL", evaluation_available=False),
                            working=CaseWorkingOntology(), max_rounds=2, max_iterations=1)
        self.assertEqual(result.failure_reason, "evaluation_unavailable")
        runner.analyze_errors.assert_not_called()


if __name__ == "__main__":
    unittest.main()
