import json

import pytest
from langsmith import Client, tracing_context
from test_agent_runtime import ScriptModel, call, finish, message, session_for

from cortex.capabilities.sql_training.learning.runtime import LearningAgent
from cortex.capabilities.sql_training.training.observability import SummaryTracer


class RecordingClient(Client):
    def __init__(self):
        super().__init__(api_key="local-test-key", auto_batch_tracing=False)
        self.payloads = []

    def create_run(self, *args, **kwargs):
        self.payloads.append(("create", kwargs))

    def update_run(self, *args, **kwargs):
        self.payloads.append(("update", kwargs))


def test_remote_spans_are_hierarchical_and_never_receive_raw_payloads(
    case_graph, monkeypatch
):
    client = RecordingClient()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    session = session_for(case_graph)
    session.record["evidence"] = "PRIVATE_SOURCE_SENTINEL"
    ref = session.evidence.put("sensitive", {"sql_result": "PRIVATE_RESULT_SENTINEL"})
    model = ScriptModel(
        [message(call("read_evidence", {"evidence_id": ref})), finish()]
    )
    with tracing_context(client=client):
        with SummaryTracer(client=client).span(
            "extract.case", case_id="allowed-case-id", raw="PRIVATE_META_SENTINEL"
        ):
            assert LearningAgent(model).run(session).direct_completed
        with pytest.raises(ValueError, match="PRIVATE_ERROR_SENTINEL"):  # noqa: SIM117
            with SummaryTracer(client=client).span(
                "extract.failure", case_id="allowed-case-id"
            ):
                raise ValueError("PRIVATE_ERROR_SENTINEL")
    client.flush()
    text = json.dumps(client.payloads, default=str)
    assert client.payloads
    assert "allowed-case-id" in text
    assert "PRIVATE_" not in text
    roots = [payload for kind, payload in client.payloads if kind == "create"]
    assert any(payload.get("parent_run_id") for payload in roots)
    assert all(payload.get("inputs") == {} for payload in roots)


def test_direct_case_workflow_never_enables_native_state_payload_tracing(
    case_graph, monkeypatch
):
    from conftest import make_rule
    from test_case_workflow import EditingAgent, make_workflow

    class NotesAgent(EditingAgent):
        def run(self, session):
            session.notes.current_plan = "PRIVATE_WORKFLOW_NOTES_SENTINEL"
            return super().run(session)

    workflow, _services, _validator = make_workflow(
        case_graph, NotesAgent([[make_rule()]])
    )
    client = RecordingClient()
    from cortex.capabilities.sql_training.training.workflow import graph as graph_module

    monkeypatch.setattr(
        graph_module, "SummaryTracer", lambda: SummaryTracer(client=client)
    )
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    # Intentionally call the public workflow directly, outside TrainPipeline's
    # SummaryTracer wrapper. Native graph instrumentation would upload State.
    with tracing_context(enabled=True, client=client):
        state = workflow.run()
    client.flush()
    assert state.notes.current_plan == "PRIVATE_WORKFLOW_NOTES_SENTINEL"
    assert (
        workflow.inspect().values["notes"]["current_plan"] == state.notes.current_plan
    )
    payloads = json.dumps(client.payloads, default=str)
    assert client.payloads
    assert "PRIVATE_" not in payloads
    created = [payload for kind, payload in client.payloads if kind == "create"]
    assert all(payload.get("inputs") == {} for payload in created)
    assert all(payload["name"].startswith("extract.") for payload in created)
