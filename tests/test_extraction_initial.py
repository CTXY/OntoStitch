"""Behavioral checks for direct retention and the unchanged stable replay gate."""

import json
from pathlib import Path

from cortex.capabilities.sql_training.models import NodeAsset


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


def test_direct_retention_export_and_evaluation_match(case_graph):
    from test_case_workflow import EditingAgent, make_workflow

    from cortex.capabilities.sql_training.artifacts.cases import case_result, seal_case

    ctrl, services, _ = make_workflow(case_graph, EditingAgent([[rule()]]))
    state = ctrl.run()
    result = seal_case(
        ctrl.root,
        case_result(ctrl.runtime.record, state, ctrl.runtime.graph, services, ctrl.root),
        ctrl.runtime.graph,
    )
    saved = json.loads(Path(result["result_path"]).read_text())
    assert result["directly_retained_asset_ids"] == [rule().id]
    assert result["assessment_accepted_asset_ids"] == []
    assert result["draft_node_ids"] == [rule().id]
    assert (
        result["evaluation_ontology_fingerprint"]
        == result["final_ontology_fingerprint"]
    )
    assert saved["completion_reason"] == "stable_correct"


def test_unavailable_status_preserves_retained_knowledge(case_graph):
    from test_case_workflow import EditingAgent, Validator, make_workflow

    from cortex.capabilities.sql_training.artifacts.cases import case_result

    class UnavailableValidator(Validator):
        def retrieve(self, **kwargs):
            raise ConnectionError("offline")

    ctrl, services, _ = make_workflow(
        case_graph, EditingAgent([[rule()]]), UnavailableValidator()
    )
    import pytest
    from cortex.capabilities.sql_training.training.workflow.nodes import ValidationUnavailable
    with pytest.raises(ValidationUnavailable):
        ctrl.run()
    state = ctrl.state.model_copy(update={"operational_failure": True})
    result = case_result(ctrl.runtime.record, state, ctrl.runtime.graph, services, ctrl.root)
    assert not result["correct"]
    assert result["validation_status"] == "unavailable"
    assert result["draft_node_count"] == 1
    assert result["asset_outcome"] == "partial"
