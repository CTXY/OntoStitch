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
