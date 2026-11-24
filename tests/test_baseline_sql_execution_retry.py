"""The final SQL path retries only failed executions, using actual SQLite errors."""

import json
from types import SimpleNamespace

import pytest
import yaml
from nl2sql_baseline.config import load_settings
from nl2sql_baseline.database import Database
from nl2sql_baseline.generation import SqlGenerator
from nl2sql_baseline.workflow import Workflow
from sqlalchemy import create_engine


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def invoke(self, messages):
        self.messages.append(messages)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(content=response)


class RecordingDatabase(Database):
    def __init__(self, engine):
        super().__init__(str(engine.url), engine=engine, query_timeout=3)
        self.queries = []

    def execute_read_only(self, sql):
        self.queries.append(sql)
        return super().execute_read_only(sql)


def run_case(
    tmp_path,
    responses,
    *,
    mode="none",
    gold="SELECT value FROM numbers /* GOLD_ONLY */",
    resume_attempts=None,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE numbers(value INTEGER)")
        connection.exec_driver_sql("CREATE TABLE other(value INTEGER)")
        connection.exec_driver_sql("INSERT INTO numbers VALUES (7)")
        connection.exec_driver_sql("INSERT INTO other VALUES (7)")
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "source_id": "unit",
                    "question": "Return the stored number.",
                    "evidence": "Use the numbers table.",
                    "sql": gold,
                }
            ]
        )
    )
    guidance = tmp_path / "guidance.json"
    guidance.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "source_id": "unit",
                        "guidance_content": "Keep the stored value unchanged.",
                    }
                ]
            }
        )
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "dataset": {"type": "bird"},
                "test_set_path": str(cases),
                "database": {"root": str(tmp_path)},
                "workspace_root": str(tmp_path / "results"),
                "schema_linking": {"type": "full_schema"},
                "memory": {"type": mode, "source_path": str(guidance)},
                "llm": {"model": "offline", "api_key": "test-only"},
                "evaluation": {"sql_timeout": 3},
            }
        )
    )
    model = ScriptedModel(responses)
    generator = SqlGenerator(
        model="offline",
        api_key="test-only",
        base_url=None,
        temperature=0,
        timeout=3,
        llm=model,
    )
    database = RecordingDatabase(engine)
    try:
        run, _ = Workflow(
            load_settings(config), database=database, generator=generator
        ).evaluate(resume_sql_attempts={"unit": resume_attempts} if resume_attempts else None)
        prediction = json.loads((run / "predictions.jsonl").read_text().splitlines()[0])
        attempts = (
            json.loads(next((run / "sql_attempts").glob("*.json")).read_text())
            if (run / "sql_attempts").exists()
            else []
        )
        return prediction, model.messages, attempts, database.queries
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["none", "downstream_guidance"])
def test_execution_error_is_returned_to_model_with_original_context(tmp_path, mode):
    broken = "SELECT value FROM numbers JOIN other ON numbers.value=other.value"
    fixed = "SELECT numbers.value FROM numbers JOIN other ON numbers.value=other.value"
    result, messages, attempts, queries = run_case(tmp_path, [broken, fixed], mode=mode)
    assert result["evaluation"]["exec"] == 1
    assert len(messages) == 2
    assert messages[1][-2] == {"role": "assistant", "content": broken}
    assert "ambiguous column name: value" in messages[1][-1]["content"]
    assert messages[1][:2] == messages[0]
    assert "Use the numbers table." in messages[1][1]["content"]
    if mode != "none":
        assert "Keep the stored value unchanged." in messages[1][1]["content"]
    assert "GOLD_ONLY" not in json.dumps(messages)
    assert queries == [broken, fixed, "SELECT value FROM numbers /* GOLD_ONLY */"]
    assert attempts[0]["error"] and attempts[1]["error"] is None
    assert result["sql_attempts"] == attempts


def test_prose_is_rejected_but_one_exact_fence_is_unwrapped(tmp_path):
    prose = "Here is the query:\nSELECT value FROM numbers"
    fenced = "```sql\nSELECT value FROM numbers\n```"
    result, messages, attempts, _ = run_case(
        tmp_path, [prose, fenced, "SELECT value FROM numbers"]
    )
    assert result["evaluation"]["exec"] == 1
    assert len(messages) == 2
    assert attempts[0]["sql"] == prose and attempts[1]["sql"] == 'SELECT value FROM numbers'
    assert (
        "SQL must be one read-only SELECT or WITH query" in messages[1][-1]["content"]
    )


@pytest.mark.parametrize('mode', ['none', 'downstream_guidance'])
def test_exact_fenced_sql_uses_same_parser_in_all_arms(tmp_path, mode):
    result, messages, attempts, _ = run_case(tmp_path, ['```sql\nSELECT value FROM numbers\n```'], mode=mode)
    assert result['evaluation']['exec'] == 1 and len(messages) == 1
    assert attempts == [{'sql': 'SELECT value FROM numbers', 'error': None}]


@pytest.mark.parametrize('text', ['Here is SQL:\n```sql\nSELECT 1\n```',
    '```sql\nSELECT 1\n```\nAdditional text', '```sql\nSELECT 1\n```\n```sql\nSELECT 2\n```'])
def test_sql_parser_never_extracts_query_from_mixed_output(text):
    from nl2sql_baseline.generation import _content
    assert _content(SimpleNamespace(content=text)) == text


def test_fence_normalization_preserves_identifiers_and_read_only_guard(tmp_path):
    from nl2sql_baseline.generation import _content
    from nl2sql_baseline.database import _ensure_read_only
    assert _content(SimpleNamespace(content='```sql\nSELECT `a column` FROM t\n```')) == 'SELECT `a column` FROM t'
    with pytest.raises(ValueError, match='read-only'):
        _ensure_read_only(_content(SimpleNamespace(content='```sql\nDELETE FROM t\n```')))
    with pytest.raises(ValueError, match='read-only'):
        _ensure_read_only(_content(SimpleNamespace(content='```sql\nSELECT 1; SELECT 2\n```')))


def test_third_execution_failure_is_terminal_without_a_fourth_generation(tmp_path):
    result, messages, attempts, queries = run_case(
        tmp_path, ["SELECT missing FROM numbers"] * 4
    )
    assert len(messages) == 3 and len(attempts) == 3 and len(queries) == 3
    assert result["evaluation"]["exec"] == 0
    assert "no such column: missing" in result["error"]
    assert result["error_stage"] == "sql_execution"


def test_successful_sql_with_wrong_answer_is_not_retried(tmp_path):
    result, messages, attempts, _ = run_case(
        tmp_path, ["SELECT value+1 FROM numbers", "SELECT value FROM numbers"]
    )
    assert len(messages) == 1 and len(attempts) == 1
    assert result["evaluation"] == {
        "exec": 0,
        "reason": "Result row sets do not match.",
    }
    assert result["error"] is None


def test_gold_failure_cannot_trigger_prediction_repair(tmp_path):
    result, messages, attempts, _ = run_case(
        tmp_path, ["SELECT value FROM numbers"], gold="SELECT * FROM missing_gold_table"
    )
    assert len(messages) == 1 and len(attempts) == 1 and attempts[0]["error"] is None
    assert "missing_gold_table" in result["error"]
    assert result["error_stage"] == "gold_execution"
    assert "missing_gold_table" not in json.dumps(messages)


def test_generation_exception_is_not_treated_as_sql_execution_error(tmp_path):
    result, messages, attempts, queries = run_case(
        tmp_path, [RuntimeError("generation failed")]
    )
    assert len(messages) == 1 and not attempts and not queries
    assert result["error"] == "generation failed"
    assert result["error_stage"] == "generation"


@pytest.mark.parametrize("mode", ["none", "downstream_guidance"])
def test_network_resume_keeps_used_sql_budget_without_reexecuting_old_sql(tmp_path, mode):
    previous = [
        {"sql": "SELECT * FROM old_missing", "error": "no such table: old_missing"},
        {"sql": "SELECT old_column FROM numbers", "error": "no such column: old_column"},
    ]
    result, messages, attempts, queries = run_case(
        tmp_path, ["SELECT new_missing FROM numbers"], mode=mode,
        resume_attempts=previous,
    )
    assert len(messages) == 1 and len(attempts) == 3
    assert attempts[:2] == previous
    assert len(queries) == 1 and "new_missing" in queries[0]
    assert messages[0][-2]["content"] == previous[-1]["sql"]
    assert "no such column: old_column" in messages[0][-1]["content"]
    assert result["error_stage"] == "sql_execution"


@pytest.mark.parametrize(
    "stage,sql_attempts,expected_runs",
    [
        ("sql_execution", [{"sql": "SELECT bad", "error": "502"}], 1),
        ("generation", [{"sql": "SELECT bad", "error": "bad column"}], 1),
        ("generation", [], 3),
    ],
)
def test_outer_transport_retry_cannot_reset_sql_retry_budget(
    tmp_path, monkeypatch, stage, sql_attempts, expected_runs
):
    import importlib
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    runner = importlib.import_module("run_graph_guided_test")
    config_module = importlib.import_module("nl2sql_baseline.config")
    workflow_module = importlib.import_module("nl2sql_baseline.workflow")
    runs = []

    class FailedWorkflow:
        def __init__(self, *args, **kwargs):
            pass

        def evaluate(self, *, output):
            runs.append(output)
            output.mkdir()
            prediction = {
                "error_stage": stage,
                "sql_attempts": sql_attempts,
                "error": "Error code: 502",
                "evaluation": {"exec": 0},
            }
            (output / "predictions.jsonl").write_text(json.dumps(prediction) + "\n")
            return output, {}

    monkeypatch.setattr(workflow_module, "Workflow", FailedWorkflow)
    monkeypatch.setattr(config_module, "load_settings", lambda *a, **kw: object())
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    runner.baseline(
        {"source_id": "unit"},
        None,
        "plain",
        tmp_path,
        object(),
        SimpleNamespace(database_root=tmp_path, model="offline"),
    )
    assert len(runs) == expected_runs


@pytest.mark.parametrize('provider_error',['Error code: 502','Error code: 402 - Insufficient Balance'])
def test_runner_resumes_cached_transport_failure_and_archives_old_record(tmp_path, monkeypatch,provider_error):
    import importlib
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    runner = importlib.import_module("run_graph_guided_test")
    workflow_module = importlib.import_module("nl2sql_baseline.workflow")
    config_module = importlib.import_module("nl2sql_baseline.config")
    attempts = [{"sql": "SELECT bad", "error": "no such column: bad"}]
    previous = {"error_stage": "generation", "sql_attempts": attempts, "error": provider_error}
    saved = tmp_path / "plain_prediction.json"
    saved.write_text(json.dumps(previous))
    calls = []

    class ResumedWorkflow:
        def __init__(self, *args, **kwargs): pass

        def evaluate(self, *, output, resume_sql_attempts):
            calls.append(resume_sql_attempts)
            output.mkdir()
            (output / "predictions.jsonl").write_text(json.dumps({"error": None}) + "\n")
            return output, {}

    monkeypatch.setattr(workflow_module, "Workflow", ResumedWorkflow)
    monkeypatch.setattr(config_module, "load_settings", lambda *a, **kw: object())
    runner.baseline({"source_id": "unit"}, None, "plain", tmp_path, object(),
                    SimpleNamespace(database_root=tmp_path, model="offline"))
    assert calls == [{"unit": attempts}]
    archive = next((tmp_path / "transport_resume_history").glob("*.json"))
    assert json.loads(archive.read_text()) == previous
