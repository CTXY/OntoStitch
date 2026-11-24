import asyncio
import json
from copy import deepcopy

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import ValidationError

from cortex.application.online_context import ChoicePoint
from cortex.capabilities.online_context.discovery.organizer import LlmChoiceOrganizer
from cortex.infrastructure.llm import LLMStructuredOutputError
from test_online_candidate_evidence import graph, candidate, EMPTY_INTRA, EMPTY_UPDATE


def proposal():
    sql, memory = candidate("attempt", sql=True), candidate("memory")
    memory["statement"] = sql["statement"]
    return {"slot_augmentations": [], "new_slots": [{
        "slot_id": "same_behavior", "scope": "binding",
        "decision_question": "Which source?", "candidates": [sql, memory],
    }]}


@pytest.mark.parametrize("single", [False, True])
def test_877_agreement_does_not_become_a_one_option_question(single):
    raw = proposal()
    if single:
        only = raw["new_slots"][0]["candidates"][0]
        only["graph_refs"] = ["g00002"]
        only["evidence_assessment"]["source_types"].append("database_schema")
        raw["new_slots"][0]["candidates"] = [only]
    traces = []
    model = FakeListChatModel(responses=[json.dumps(x) for x in [EMPTY_INTRA, raw, EMPTY_UPDATE]])
    result = asyncio.run(LlmChoiceOrganizer(model, trace_sink=lambda s,p: traces.append((s,p))).organize(
        question="List records", initial_sql="SELECT * FROM a", retrieved_graph_state=graph(),
    ))
    assert result.choice_points == ()
    normalized = next(p for s,p in traces if s == "proposal_normalization")
    assert normalized["retained_slot_ids"] == []


def test_duplicate_normalization_cannot_hide_unknown_evidence():
    raw = proposal()
    raw["new_slots"][0]["candidates"][1]["graph_refs"] = ["unknown-ref"]
    model = FakeListChatModel(responses=[json.dumps(x) for x in [EMPTY_INTRA, raw, raw]])
    with pytest.raises(LLMStructuredOutputError, match="unknown Graph"):
        asyncio.run(LlmChoiceOrganizer(model).organize(
            question="List records", initial_sql="SELECT * FROM a", retrieved_graph_state=graph(),
        ))


def test_exact_duplicates_merge_evidence_without_erasing_a_real_alternative():
    raw = proposal()
    other = candidate("other")
    other["statement"] = "Use records from b"
    raw["new_slots"][0]["candidates"].append(other)
    model = FakeListChatModel(responses=[json.dumps(x) for x in [EMPTY_INTRA, raw, EMPTY_UPDATE]])
    result = asyncio.run(LlmChoiceOrganizer(model).organize(
        question="List records", initial_sql="SELECT * FROM a", retrieved_graph_state=graph(),
    ))
    assert len(result.choice_points[0].options) == 2
    first = result.choice_points[0].options[0]
    assert first.initial_sql_evidence == "FROM a" and first.graph_refs == ("table.b",)
    assert set(first.evidence_assessment.source_types) == {"initial_sql", "database_schema"}
    raw_point = result.choice_points[0].model_dump(mode="json")
    raw_point["options"] = raw_point["options"][:1]
    with pytest.raises(ValidationError):
        ChoicePoint.model_validate(raw_point)
