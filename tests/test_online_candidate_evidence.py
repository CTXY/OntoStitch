"""Invalid evidence must be repaired, never converted into an empty discovery."""

import asyncio
import json
from copy import deepcopy

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import ValidationError

from cortex.capabilities.online_context.discovery.models import (
    CandidateDraft,
    ProvenanceAddition,
)
from cortex.capabilities.online_context.discovery.organizer import LlmChoiceOrganizer
from cortex.infrastructure.llm import LLMStructuredOutputError
from cortex.schema import GraphNode, GraphSpace, GraphState


def candidate(key="memory", *, sql=False):
    return {
        "candidate_id": key,
        "statement": "Use records from a" if sql else "Use records from b",
        "graph_refs": [] if sql else ["g00002"],
        "initial_sql_evidence": "FROM a" if sql else None,
        "evidence_assessment": {
            "source_types": ["initial_sql"] if sql else ["database_schema"],
            "applicability": "applicable",
            "explanation": "The supplied evidence supports this source binding.",
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        {"graph_refs": []},
        {"graph_refs": [""]},
        {"graph_refs": ["g00002", "g00002"]},
        {"initial_sql_evidence": " "},
        {"initial_sql_evidence": "FROM a"},
        {"sources": ["initial_sql"]},
        {"evidence_assessment": {
            "source_types": ["initial_sql"], "applicability": "applicable",
            "explanation": "A prose claim cannot replace an actual SQL fragment.",
        }},
    ],
)
def test_prose_or_declared_sources_cannot_replace_actual_citations(change):
    raw = candidate() | change
    with pytest.raises(ValidationError, match="memory"):
        CandidateDraft.model_validate(raw)


def test_graph_sql_and_combined_evidence_are_valid_without_fabricated_fields():
    graph = CandidateDraft.model_validate(candidate())
    sql = CandidateDraft.model_validate(candidate("sql", sql=True))
    both_raw = candidate()
    both_raw["initial_sql_evidence"] = "FROM a"
    both_raw["evidence_assessment"]["source_types"].append("initial_sql")
    both = CandidateDraft.model_validate(both_raw)
    assert graph.initial_sql_evidence is None and sql.graph_refs == ()
    assert both.graph_refs and both.initial_sql_evidence


def test_provenance_addition_must_supply_the_evidence_it_adds():
    raw = candidate()
    raw.pop("statement")
    assert ProvenanceAddition.model_validate(raw).graph_refs == ("g00002",)
    raw["graph_refs"] = []
    with pytest.raises(ValidationError, match="explanation is not a citation"):
        ProvenanceAddition.model_validate(raw)


class Prompts(BaseCallbackHandler):
    def __init__(self):
        self.calls = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.calls.append(messages[0])


def graph():
    return GraphState(graph=GraphSpace(nodes=tuple(
        GraphNode(id=f"table.{name}", node_type="table", name=name,
                  properties={"table_name": name})
        for name in ("a", "b")
    )))


def slot(*, sql=True):
    first = candidate("attempt", sql=sql)
    if not sql:
        first["statement"] = "Use records from a"
        first["graph_refs"] = ["g00001"]
    return {
        "slot_id": "source", "scope": "binding",
        "decision_question": "Which record source is intended?",
        "candidates": [first, candidate()],
    }


EMPTY_INTRA = {"assessments": [], "slots": []}
EMPTY_UPDATE = {"slot_augmentations": [], "new_slots": []}


@pytest.mark.parametrize("stage", ["intra", "initial", "cross"])
@pytest.mark.parametrize("failure", ["missing", "unknown_ref", "nonverbatim_sql"])
def test_all_discovery_stages_repair_evidence_with_original_context(stage, failure):
    good_slot = slot(sql=stage == "initial")
    good = ({"assessments": [], "slots": [good_slot]} if stage == "intra"
            else {"slot_augmentations": [], "new_slots": [good_slot]})
    bad = deepcopy(good)
    bad_slot = bad["slots" if stage == "intra" else "new_slots"][0]
    if failure == "missing":
        bad_slot["candidates"][1]["graph_refs"] = []
    elif failure == "unknown_ref":
        bad_slot["candidates"][1]["graph_refs"] = ["invented-handle"]
    else:
        c = bad_slot["candidates"][0]
        c["graph_refs"] = []
        c["initial_sql_evidence"] = "FROM not_the_supplied_query"
        c["evidence_assessment"]["source_types"] = ["initial_sql"]
    responses = [bad, good, EMPTY_UPDATE] if stage == "intra" else (
        [EMPTY_INTRA, bad, good, EMPTY_UPDATE] if stage == "initial"
        else [EMPTY_INTRA, EMPTY_UPDATE, bad, good]
    )
    captured = Prompts()
    model = FakeListChatModel(responses=[json.dumps(x) for x in responses], callbacks=[captured])
    # Intra can cite Initial SQL too; when supplied, it also runs the comparison stage.
    if stage == "intra":
        model.responses.append(json.dumps(EMPTY_UPDATE))
    result = asyncio.run(LlmChoiceOrganizer(model).organize(
        question="Which record source?", initial_sql="SELECT * FROM a",
        retrieved_graph_state=graph(),
    ))
    assert len(result.choice_points) == 1
    options = result.choice_points[0].options
    assert options[1].graph_refs == ("table.b",)
    repair = next(call for call in captured.calls
                  if "Validation errors:" in str(call[-1].content))
    assert "Which record source?" in str(repair)
    assert "g00002" in str(repair)
    if stage != "cross":
        assert "SELECT * FROM a" in str(repair)


def test_unrepaired_missing_evidence_fails_instead_of_reporting_no_conflict():
    raw = slot()
    for c in raw["candidates"]:
        c["graph_refs"] = []
        c["initial_sql_evidence"] = None
    invalid = {"slot_augmentations": [], "new_slots": [raw]}
    captured = Prompts()
    model = FakeListChatModel(
        responses=[json.dumps(x) for x in [EMPTY_INTRA, invalid, invalid]],
        callbacks=[captured],
    )
    with pytest.raises(LLMStructuredOutputError, match="after one repair"):
        asyncio.run(LlmChoiceOrganizer(model).organize(
            question="Which records?", initial_sql="SELECT * FROM a",
            retrieved_graph_state=graph(),
        ))
    assert len(captured.calls) == 3  # No second episode or silent cross-stage fallback.


def test_invalid_new_citation_cannot_hide_behind_an_existing_valid_citation():
    intra = {"assessments": [], "slots": [slot()]}
    addition = candidate("attempt", sql=True)
    addition.pop("statement")
    bad = {"new_slots": [], "slot_augmentations": [{
        "slot_id": "source", "provenance_additions": [addition], "new_candidates": [],
    }]}
    bad["slot_augmentations"][0]["provenance_additions"][0]["initial_sql_evidence"] = "FROM invented"
    good = deepcopy(bad)
    good["slot_augmentations"][0]["provenance_additions"][0]["initial_sql_evidence"] = "FROM a"
    captured = Prompts()
    model = FakeListChatModel(
        responses=[json.dumps(x) for x in [intra, bad, good, EMPTY_UPDATE]],
        callbacks=[captured],
    )
    result = asyncio.run(LlmChoiceOrganizer(model).organize(
        question="Which records?", initial_sql="SELECT * FROM a",
        retrieved_graph_state=graph(),
    ))
    assert result.choice_points[0].options[0].initial_sql_evidence == "FROM a"
    assert len(captured.calls) == 4
    assert "not a verbatim fragment" in str(captured.calls[2][-1].content)


def test_a_genuinely_empty_discovery_is_still_accepted():
    model = FakeListChatModel(responses=[json.dumps(x) for x in [EMPTY_INTRA, EMPTY_UPDATE]])
    result = asyncio.run(LlmChoiceOrganizer(model).organize(
        question="List records.", initial_sql=None, retrieved_graph_state=graph(),
    ))
    assert result.choice_points == ()
