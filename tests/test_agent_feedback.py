import pytest

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.training.feedback import AssessmentFeedback


def feedback(verdicts, merge=()):
    return AssessmentFeedback.model_validate(
        {
            "group_results": [
                {"group_id": key, "verdict": verdict, "evidence": "SQL and source"}
                for key, verdict in verdicts
            ],
            "target_results": [],
            "merge_groups": list(merge),
        }
    )


def test_independent_groups_partial_accept_but_coupled_groups_atomic():
    groups = [
        ChangeGroup(group_id="a", member_ids=["concept.a"]),
        ChangeGroup(group_id="b", member_ids=["metric.b"]),
    ]
    assert feedback([("a", "acceptable"), ("b", "reject")]).accepted_members(
        groups
    ) == ["concept.a"]
    assert (
        feedback([("a", "acceptable"), ("b", "reject")], [["a", "b"]]).accepted_members(
            groups
        )
        == []
    )
    with pytest.raises(ValueError, match="every group"):
        feedback([("a", "acceptable")]).accepted_members(groups)


def test_independent_assessor_gets_compact_prior_definitions_and_all_current_nodes(
    case_graph,
):
    import json

    from conftest import make_rule
    from langchain_core.messages import AIMessage
    from test_agent_graph import edit
    from test_agent_runtime import ScriptModel

    from cortex.capabilities.sql_training.training.feedback import assess

    store, database, _ = case_graph
    edit(store, database, make_rule("changed", "old"))
    edit(store, database, make_rule("untouched"), edit_id="second")
    store.retain_direct()
    before = store.accepted
    trial = edit(store, database, make_rule("changed", "new"), "update", "third")
    model = ScriptModel(
        [
            AIMessage(
                content=json.dumps(
                    {
                        "group_results": [
                            {
                                "group_id": "g1",
                                "verdict": "acceptable",
                                "evidence": "used",
                            }
                        ],
                        "target_results": [{"target_id": "t", "status": "resolved"}],
                    }
                )
            )
        ]
    )
    result = assess(
        model,
        record={"question": "How many?", "sql": "SELECT COUNT(*) FROM orders"},
        baseline=before,
        accepted=before,
        trial=trial,
        groups=[ChangeGroup(group_id="g1", member_ids=["generation_rule.changed"])],
        targets=[{"target_id": "t"}],
        validation={"status": "stable_correct"},
        context_limit=64000,
    )
    assert result.group_results[0].verdict == "acceptable"
    payload = json.loads(model._calls[0][1].content)
    assert len(payload["baseline"]) == len(payload["accepted_before"]) == 1
    assert len(payload["current"]) == 2
