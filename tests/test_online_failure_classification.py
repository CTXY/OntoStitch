import asyncio
from types import SimpleNamespace

import httpx

from cortex.application.online_context import OnlineCheckpointCompatibilityError
from cortex.infrastructure.llm import LLMStructuredOutputError
from cortex.infrastructure.workflow.diagnostics import classify_workflow_error, case_outcome


def test_checkpoint_error_keeps_safe_reason_and_is_not_a_network_retry():
    exc = OnlineCheckpointCompatibilityError("cannot reuse run ID with changed configuration")
    d = classify_workflow_error(exc)
    assert d["error_code"] == "checkpoint_incompatible"
    assert d["error_stage"] == "online_checkpoint" and not d["retryable"]
    assert "changed configuration" in d["safe_message"]


def test_nested_timeout_is_retryable_but_contract_failure_is_not():
    outer = RuntimeError("model invocation")
    outer.__cause__ = httpx.ReadTimeout("upstream read timeout")
    assert classify_workflow_error(outer)["retryable"]
    d = classify_workflow_error(LLMStructuredOutputError("one candidate is not a choice"))
    assert d["error_code"] == "output_contract_error" and not d["retryable"]


def test_empty_provider_response_is_retryable_and_not_an_sql_contract_error():
    from cortex.infrastructure.llm.adaptive_limit import EmptyCompletion
    wrapper=RuntimeError('LLM invocation failed');wrapper.__cause__=EmptyCompletion('Completion has no final content')
    d=classify_workflow_error(wrapper)
    assert d['error_code']=='provider_empty_completion' and d['retryable']
    assert d['error_stage']=='provider_response'


def test_quota_failure_is_not_an_internal_or_sql_error():
    class QuotaError(RuntimeError):status_code=402
    exc=RuntimeError('LLM invocation failed')
    exc.__cause__=QuotaError('Insufficient Balance')
    result=classify_workflow_error(exc)
    assert result['error_code']=='provider_quota_error'
    assert result['error_stage']=='provider_response' and not result['retryable']


def test_error_chain_never_exposes_credential_text():
    d = classify_workflow_error(RuntimeError("api_key=sk-secret-token Authorization: Bearer private-token"))
    assert "sk-secret-token" not in str(d) and "private-token" not in str(d)


def test_no_guided_sql_is_not_reported_as_ea_mismatch():
    assert case_outcome({"status": "error"}, None) == "online_blocked"
    assert case_outcome({}, {"error": "SQL failed"}) == "sql_error"
    assert case_outcome({}, {"evaluation": {"exec": 0}}) == "ea_mismatch"


def test_evaluation_record_preserves_checkpoint_failure_instead_of_generic_case_failed(monkeypatch):
    import sys
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    from run_bird_dev_online_guided_baseline import _online_case

    class Service:
        async def run(self, **kwargs):
            raise OnlineCheckpointCompatibilityError("cannot reuse run ID with changed configuration")

    result = asyncio.run(_online_case(
        SimpleNamespace(service=Service(), traces=[]), graph_state=None, column_context=(),
        case={"source_id": "unit", "database_id": "unit", "question": "List records", "evidence": ""},
    ))
    assert result["error_code"] == "checkpoint_incompatible"
    assert result["error_stage"] == "online_checkpoint"
    assert "changed configuration" in result["error"] and not result["retryable"]
