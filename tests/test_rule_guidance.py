import asyncio
import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import ValidationError

from cortex.application.online_context import ClarificationFilterDecision
from cortex.capabilities.online_context.output.guidance import GuidanceAgent
from cortex.capabilities.online_context.output.sql_handoff import render_sql_handoff


def test_all_bounded_graph_definitions_are_exported_without_a_second_filter():
    assets = [{'node_id': 'patient', 'type': 'concept', 'summary': 'Patient definition'},
              {'node_id': 'unrelated', 'type': 'concept', 'summary': 'Creatinine definition'}]
    text = render_sql_handoff({'ontology_assets': assets})
    assert 'Patient definition' in text and 'Creatinine definition' in text
    assert len(assets) == 2


def test_no_guidance_model_call_without_slots_or_rules():
    class NoCall:
        async def ainvoke(self, *args, **kwargs):
            raise AssertionError('Definitions must not trigger an extra filter call')
    result = asyncio.run(GuidanceAgent(NoCall()).synthesize(question='test', dialect='sqlite',
        choice_points=(), answers=()))
    assert result.guidance_content == '' and result.interaction_guidance == ()


def test_filter_cannot_emit_implementation_or_irrelevance():
    for extra in ({"selected_option_id": "x"}, {"outcome": "not_relevant"}):
        with pytest.raises(ValidationError):
            ClarificationFilterDecision.model_validate({"choice_point_id": "x", "outcome": "keep", "reason": "test", **extra})


def test_rules_assessed_even_without_clarifications():
    rules = ({"rule_id": "missing_values", "condition": "ranking nullable measurements", "action": "Exclude NULL from the ranked measurements."},
             {"rule_id": "contact_layout", "condition": "request includes contact and entity name", "action": "return contact and name"})
    assessments = [dict(rule_id="missing_values", include=True, reason="ranking nullable measurements requested", instruction="Exclude NULL from the ranked measurements."),
                   dict(rule_id="contact_layout", include=False, reason="entity name not requested", instruction="")]
    model = FakeListChatModel(responses=[json.dumps({"guidance_by_slot": [], "rule_guidance": assessments})])
    result = asyncio.run(GuidanceAgent(model).synthesize(question="Lowest known measurement", dialect="sqlite", choice_points=(), answers=(), rule_candidates=rules))
    assert len(result.rule_guidance) == 2
    text = render_sql_handoff({"rule_guidance": [x.model_dump() for x in result.rule_guidance]})
    assert "Exclude NULL" in text
    assert "return contact and name" not in text


def test_rule_coverage_is_not_silently_dropped():
    model = FakeListChatModel(responses=[json.dumps({"guidance_by_slot": [], "rule_guidance": []})])
    with pytest.raises(ValueError, match="each candidate Rule"):
        asyncio.run(GuidanceAgent(model).synthesize(question="test", dialect="sqlite", choice_points=(), answers=(), rule_candidates=({"rule_id": "r"},)))


def test_missing_rule_is_repaired_with_original_context_and_ids():
    from langchain_core.messages import AIMessage
    class Chat:
        def __init__(self): self.requests = []
        async def ainvoke(self, messages):
            self.requests.append(messages)
            rules = [{"rule_id": "r1", "include": False, "reason": "not applicable", "instruction": ""}]
            if len(self.requests) == 2:
                rules.append({"rule_id": "r2", "include": False, "reason": "not applicable", "instruction": ""})
            return AIMessage(content=json.dumps({"guidance_by_slot": [], "rule_guidance": rules}))
    model = Chat()
    result = asyncio.run(GuidanceAgent(model).synthesize(question="unchanged task", dialect="sqlite", choice_points=(), answers=(), rule_candidates=({"rule_id": "r1"}, {"rule_id": "r2"})))
    assert len(result.rule_guidance) == 2 and len(model.requests) == 2
    retry = model.requests[1]
    assert retry[:2] == model.requests[0]
    assert "missing_rule_ids=['r2']" in retry[-1]["content"]
    assert retry[-1]["content"].count('"properties"') == 0


def test_duplicate_rule_cannot_satisfy_coverage():
    item = {"rule_id": "r", "include": False, "reason": "unrelated", "instruction": ""}
    model = FakeListChatModel(responses=[json.dumps({"guidance_by_slot": [], "rule_guidance": [item, item]})])
    with pytest.raises(ValueError, match="duplicate_rule_ids"):
        asyncio.run(GuidanceAgent(model).synthesize(question="task", dialect="sqlite", choice_points=(), answers=(), rule_candidates=({"rule_id": "r"},)))


def test_rule_action_rewrite_is_repaired_without_losing_task_context():
    from langchain_core.messages import AIMessage

    action = "Return the requested attribute of the selected row."

    class Chat:
        def __init__(self):
            self.requests = []

        async def ainvoke(self, messages):
            self.requests.append(messages)
            instruction = "Return the stored balance." if len(self.requests) == 1 else action
            return AIMessage(content=json.dumps({
                "guidance_by_slot": [],
                "rule_guidance": [{
                    "rule_id": "extreme", "include": True,
                    "reason": "The requested attribute is established.",
                    "instruction": instruction,
                }],
            }))

    model = Chat()
    result = asyncio.run(GuidanceAgent(model).synthesize(
        question="Return the requested attribute", dialect="sqlite",
        choice_points=(), answers=(),
        rule_candidates=({"rule_id": "extreme", "action": action},),
    ))
    assert result.rule_guidance[0].instruction == action
    assert len(model.requests) == 2
    assert model.requests[1][:2] == model.requests[0]
    assert "rewritten_rule_ids=['extreme']" in model.requests[1][-1]["content"]


def test_persistent_rule_action_rewrite_cannot_reach_sql_handoff():
    response = {
        "guidance_by_slot": [],
        "rule_guidance": [{
            "rule_id": "r", "include": True, "reason": "matches ranking",
            "instruction": "Return an additional field.",
        }],
    }
    model = FakeListChatModel(responses=[json.dumps(response)])
    with pytest.raises(ValueError, match="original action unchanged"):
        asyncio.run(GuidanceAgent(model).synthesize(
            question="List identifiers", dialect="sqlite", choice_points=(), answers=(),
            rule_candidates=({"rule_id": "r", "action": "Return identifiers."},),
        ))
