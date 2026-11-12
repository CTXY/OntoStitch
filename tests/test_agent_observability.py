import json

import pytest
from langsmith import Client, tracing_context
from test_agent_runtime import ScriptModel, call, finish, message, session_for

from cortex.capabilities.sql_training.learning.runtime import LearningAgent
from cortex.capabilities.sql_training.training.observability import ExtractTracer
from cortex.infrastructure.observability import redact_credentials


class RecordingClient(Client):
    def __init__(self):
        super().__init__(api_key="local-test-key", auto_batch_tracing=False)
        self.payloads = []

    def create_run(self, *args, **kwargs):
        self.payloads.append(("create", kwargs))

    def update_run(self, *args, **kwargs):
        self.payloads.append(("update", kwargs))


def assert_parents_exist(client):
    created = [payload for kind, payload in client.payloads if kind == "create"]
    ids = {str(p["id"]) for p in created}
    assert created
    assert all(
        not p.get("parent_run_id") or str(p["parent_run_id"]) in ids for p in created
    )
    by_id = {str(p["id"]): p for p in created}
    for child in created:
        if child.get("parent_run_id"):
            parent = by_id[str(child["parent_run_id"])]
            assert str(child["trace_id"]) == str(parent["trace_id"])
            assert child["dotted_order"].startswith(parent["dotted_order"] + ".")
    return created


def test_native_agent_model_tools_capture_actual_io_and_usage(case_graph, monkeypatch):
    from cortex.infrastructure.llm.pool import (
        KeyScheduler,
        PooledChatModel,
        ThreadedChatModel,
    )

    client = RecordingClient()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    session = session_for(case_graph)
    session.record["evidence"] = "SOURCE_CONTENT_SENTINEL"
    ref = session.evidence.put("observation", {"sql_result": "RESULT_CONTENT_SENTINEL"})
    response = message(call("read_evidence", {"evidence_id": ref}))
    response.usage_metadata = {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
    }
    model = PooledChatModel(
        (ThreadedChatModel(ScriptModel([response, finish()])),), KeyScheduler(1)
    )
    with (
        tracing_context(client=client),
        ExtractTracer(client=client).span(
            "extract.case", inputs={"source": session.record}
        ),
    ):
        assert LearningAgent(model).run(session).direct_completed
    client.flush()
    created = assert_parents_exist(client)
    assert sum(p["run_type"] == "llm" for p in created) == 2
    assert any(
        p["run_type"] == "tool" and p["name"] == "read_evidence" for p in created
    )
    text = json.dumps(client.payloads, default=str)
    model_inputs = json.dumps(
        [p["inputs"] for p in created if p["run_type"] == "llm"], default=str
    )
    assert "SOURCE_CONTENT_SENTINEL" in model_inputs
    assert "SOURCE_CONTENT_SENTINEL" in text
    assert "RESULT_CONTENT_SENTINEL" in text
    assert '"input_tokens": 100' in text
    assert "Complete this ontology learning episode" in text
    assert model._get_ls_params() == model._models[0]._model._get_ls_params()


def test_native_state_and_domain_evidence_are_visible(case_graph, monkeypatch):
    from conftest import make_rule
    from test_case_workflow import EditingAgent, make_workflow

    class NotesAgent(EditingAgent):
        def run(self, session):
            session.notes.current_plan = "WORKFLOW_NOTES_SENTINEL"
            return super().run(session)

    workflow, _services, _validator = make_workflow(
        case_graph, NotesAgent([[make_rule()]])
    )
    client = RecordingClient()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    with tracing_context(enabled=True, client=client):
        state = workflow.run()
    client.flush()
    created = assert_parents_exist(client)
    assert state.result_ref
    assert any(p["name"] == "extract.case_graph" for p in created)
    assert any(p["name"] == "validate_initial" for p in created)
    text = json.dumps(client.payloads, default=str)
    assert "WORKFLOW_NOTES_SENTINEL" in text
    assert "semantic_nodes" in text
    assert "terminal_evaluation" in text
    assert "stable_correct" in text


def test_redaction_keeps_content_and_usage_but_removes_credentials(monkeypatch):
    monkeypatch.setenv("CORTEX_LLM_API_KEY", "private-provider-key")
    payload = {
        "api_key": "private-value",
        "usage_metadata": {"input_tokens": 10},
        "messages": [
            {
                "content": "SELECT count(*) FROM orders private-provider-key sk-1234567890abcdef lsv2_tr_example"
            }
        ],
        "error": "postgres://alice:password@localhost/db",
        "extra": {"authorization": "Bearer test"},
    }
    result = redact_credentials(payload)
    text = json.dumps(result)
    assert "SELECT count(*) FROM orders" in text
    assert result["usage_metadata"]["input_tokens"] == 10
    assert "private-" not in text
    assert "1234567890abcdef" not in text
    assert "lsv2_tr_example" not in text
    assert "alice" not in text
    assert payload["api_key"] == "private-value"  # egress must not mutate live state


def test_failure_remains_a_trace_error():
    client = RecordingClient()
    with (
        pytest.raises(ValueError, match="SQL failure"),
        ExtractTracer(client=client, enabled=True).span("extract.failure"),
    ):
        raise ValueError("SQL failure")
    client.flush()
    assert any(p.get("error") for _, p in client.payloads)


def test_sdk_egress_redacts_native_messages_metadata_and_errors(
    case_graph, monkeypatch
):
    import requests

    from cortex.infrastructure import observability

    class RecordingSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.payloads = []

        def request(self, method, url, **kwargs):
            raw = kwargs.get("data") or kwargs.get("json") or {}
            if isinstance(raw, (str, bytes)):
                raw = json.loads(raw)
            self.payloads.append(raw)
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            return response

    transport = RecordingSession()
    clients = []

    def client_factory(**options):
        client = Client(
            api_url="http://langsmith.test",
            api_key="local-test",
            session=transport,
            auto_batch_tracing=False,
            **options,
        )
        clients.append(client)
        return client

    observability.tracing_client.cache_clear()
    monkeypatch.setattr(observability, "Client", client_factory)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("CORTEX_LLM_API_KEY", "private-egress-provider-key")
    session = session_for(case_graph)
    session.record["evidence"] = "visible source private-egress-provider-key"
    try:
        with ExtractTracer().span(
            "extract.case",
            inputs={"source": session.record},
            api_key="private-egress-metadata-key",
        ):
            assert LearningAgent(ScriptModel([finish()])).run(session).direct_completed
        with pytest.raises(ValueError), ExtractTracer().span("extract.failure"):
            raise ValueError("private-egress-provider-key")
        clients[0].flush()
        serialized = json.dumps(transport.payloads, default=str)
        assert "visible source" in serialized
        assert "How many orders?" in serialized
        assert "private-egress" not in serialized
        assert "[REDACTED]" in serialized
    finally:
        for client in clients:
            client.close()
        observability.tracing_client.cache_clear()


def test_diagnosis_assessment_and_acceptance_have_readable_evidence(
    case_graph, monkeypatch
):
    from conftest import make_rule
    from test_case_workflow import EditingAgent, Validator, assessment, make_workflow

    client = RecordingClient()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    workflow, _, _ = make_workflow(
        case_graph,
        EditingAgent([[make_rule("m", "m1")], [make_rule("m", "m2")]]),
        Validator(lambda graph: "SELECT 0"),
        lambda model, **payload: assessment(payload["groups"], ["generation_rule.m"], payload["targets"], payload["validation"]),
        iterations=1,
    )
    with tracing_context(enabled=True, client=client):
        state = workflow.run()
    client.flush()
    assert state.completion_reason == "round_limit_reached"
    assert_parents_exist(client)
    for name, content in [
        ("diagnose", "fix SQL"),
        ("assess", "unit_results"),
        ("accept", "accepted_member_ids"),
        ("seal", "round_limit_reached"),
    ]:
        run = next(
            p
            for kind, p in client.payloads
            if kind == "create" and p["name"] == "extract." + name
        )
        outputs = [
            p["outputs"]
            for kind, p in client.payloads
            if kind == "update"
            and str(p.get("run_id")) == str(run["id"])
            and p.get("outputs")
        ]
        assert content in json.dumps(outputs, default=str)
