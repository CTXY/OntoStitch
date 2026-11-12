"""Exercise the actual Click commands and exported artifacts without live models."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_offline_training_contract import SCHEMA, Engine, Merge, diagnostic, table

import cortex.cli.sql_training as cli
from cortex.application import SqlTrainingEvaluationReport, SqlTrainingService
from cortex.application.merge_graph import DiagnosticCode
from cortex.cli.main import main
from cortex.infrastructure.artifacts import FileSystemGraphStateRepository
from cortex.schema import GraphSpace, GraphState


@pytest.fixture
def command_inputs(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("{}\n")
    pairs = tmp_path / "pairs.json"
    pairs.write_text(
        json.dumps(
            {
                "queries": [
                    {
                        "id": "case",
                        "query": "Count",
                        "gt_sql": "SELECT 1",
                        "evidence": "count rows",
                    }
                ]
            }
        )
    )
    return config, pairs


def install(monkeypatch, engine, *, code=None, error=None, invalid=False):
    operators = []
    diagnostics = (diagnostic(code),) if code else ()

    def make_operator():
        operator = Merge(diagnostics, error=error, invalid=invalid)
        operators.append(operator)
        return operator

    def build_service(config_path, *, workspace, state_repository):
        return SqlTrainingService(
            engine, graph_merge=make_operator(), state_repository=state_repository
        )

    monkeypatch.setattr(cli, "build_sql_training_service", build_service)
    monkeypatch.setattr(cli, "build_sql_extraction", lambda *a, **kw: engine)
    monkeypatch.setattr(
        cli, "build_graph_merge_operator", lambda *a, **kw: make_operator()
    )
    return operators


def invoke(runner, args, summary):
    outcome = runner.invoke(main, [*args, "--result-file", str(summary)])
    payload = json.loads(summary.read_text()) if summary.exists() else None
    return outcome, payload


@pytest.mark.parametrize(
    "report,code,error,invalid,status,exit_code",
    [
        ({"total": 1, "passed": 1, "failed": 0}, None, None, False, "completed", 0),
        ({"total": 1, "passed": 0, "failed": 1}, None, None, False, "needs_review", 2),
        (
            {"total": 1, "passed": 0, "failed": 1, "fatal": True},
            None,
            None,
            False,
            "failed",
            1,
        ),
        (
            {"total": 1, "passed": 1, "failed": 0},
            DiagnosticCode.MODEL_FAILURE,
            None,
            False,
            "needs_review",
            2,
        ),
        (
            {"total": 1, "passed": 1, "failed": 0},
            DiagnosticCode.STALE_REFERENCE_EDGE_DROPPED,
            None,
            False,
            "completed",
            0,
        ),
        (
            {"total": 1, "passed": 1, "failed": 0},
            None,
            RuntimeError("merge failed"),
            False,
            "failed",
            1,
        ),
        ({"total": 1, "passed": 1, "failed": 0}, None, None, True, "failed", 1),
    ],
)
def test_commands_export_equivalent_terminal_reports_and_exit_codes(
    tmp_path,
    monkeypatch,
    command_inputs,
    report,
    code,
    error,
    invalid,
    status,
    exit_code,
):
    config, pairs = command_inputs
    engine = Engine(SqlTrainingEvaluationReport(**report))
    operators = install(monkeypatch, engine, code=code, error=error, invalid=invalid)
    runner = CliRunner()
    full, full_summary = invoke(
        runner,
        [
            "sql-train",
            "--config",
            str(config),
            "--training-set",
            str(pairs),
            "--output",
            str(tmp_path / "full"),
            "--run-id",
            "same-run",
        ],
        tmp_path / "full-summary.json",
    )
    extracted, extraction_summary = invoke(
        runner,
        [
            "sql-extract",
            "--config",
            str(config),
            "--training-set",
            str(pairs),
            "--output",
            str(tmp_path / "extract"),
        ],
        tmp_path / "extraction-summary.json",
    )
    assert extraction_summary is not None, extracted.output
    extraction_root = Path(extraction_summary["output"])
    assert (extraction_root / "extraction_result.json").is_file()
    split, split_summary = invoke(
        runner,
        [
            "graph-merge",
            "--config",
            str(config),
            "--training-workspace",
            str(extraction_root),
            "--output",
            str(tmp_path / "merged"),
            "--run-id",
            "same-run",
        ],
        tmp_path / "split-summary.json",
    )
    assert full.exit_code == split.exit_code == exit_code, (full.output, split.output)
    assert full_summary["status"] == split_summary["status"] == status
    full_root, split_root = Path(full_summary["output"]), Path(split_summary["output"])
    full_result = json.loads((full_root / "result.json").read_text())
    split_result = json.loads((split_root / "result.json").read_text())
    for key in ("status", "state", "evaluation_report", "merge_report"):
        assert full_result[key] == split_result[key], key
    assert (
        full_result["diagnostics"]["context"] == split_result["diagnostics"]["context"]
    )
    for root in (full_root, split_root):
        if status == "failed":
            assert not (root / "graph_state.json").exists()
        else:
            assert (
                json.loads((root / "graph_state.json").read_text())
                == full_result["state"]
            )
        if full_result["merge_report"] is None:
            assert not (root / "merge_report.json").exists()
        else:
            assert (
                json.loads((root / "merge_report.json").read_text())
                == full_result["merge_report"]
            )
    if report.get("fatal"):
        assert extracted.exit_code == 1
        assert len(operators) == 1  # split did not initialize any merge dependency
        assert operators[0].calls == 0


@pytest.mark.parametrize(
    "suffix,container",
    [
        (".json", "list"),
        (".json", "training_set"),
        (".json", "queries"),
        (".jsonl", None),
        (".ndjson", None),
    ],
)
def test_full_and_extraction_share_input_normalization(
    tmp_path, monkeypatch, command_inputs, suffix, container
):
    config, _ = command_inputs
    row = {"query": "Count", "gt_sql": "SELECT 1", "evidence": "preserve this evidence"}
    records = []

    class RecordingEngine(Engine):
        def train(self, values, schema, existing):
            records.append(values)
            return super().train(values, schema, existing)

    install(monkeypatch, RecordingEngine())
    pairs = tmp_path / ("input" + suffix)
    payload = (
        row
        if container is None
        else [row]
        if container == "list"
        else {container: [row]}
    )
    pairs.write_text(json.dumps(payload) + "\n")
    runner = CliRunner()
    for command in ("sql-train", "sql-extract"):
        result = runner.invoke(
            main,
            [
                command,
                "--config",
                str(config),
                "--training-set",
                str(pairs),
                "--output",
                str(tmp_path / command),
            ],
        )
        assert result.exit_code == 0, result.output
    assert records[0] == records[1]
    assert records[0][0].source_id == "case-0001"
    assert records[0][0].evidence == "preserve this evidence"


@pytest.mark.parametrize(
    "damage", ["missing", "missing_report", "invalid_report", "invalid_graph", "schema"]
)
def test_merge_rejects_incomplete_or_corrupt_handoff(
    tmp_path, monkeypatch, command_inputs, damage
):
    config, pairs = command_inputs
    operators = install(monkeypatch, Engine())
    runner = CliRunner()
    result, summary = invoke(
        runner,
        [
            "sql-extract",
            "--config",
            str(config),
            "--training-set",
            str(pairs),
            "--output",
            str(tmp_path / "extract"),
        ],
        tmp_path / "summary.json",
    )
    assert result.exit_code == 0
    root = Path(summary["output"])
    path = root / "extraction_result.json"
    payload = json.loads(path.read_text())
    if damage == "missing":
        path.unlink()
    else:
        if damage == "missing_report":
            payload.pop("evaluation_report")
        elif damage == "invalid_report":
            payload["evaluation_report"]["passed"] = 99
        elif damage == "invalid_graph":
            payload["state"]["graph"]["nodes"][0]["node_type"] = "unknown"
        else:
            payload["schema_hash"] = "different"
        path.write_text(json.dumps(payload))
    outcome = runner.invoke(
        main,
        [
            "graph-merge",
            "--config",
            str(config),
            "--training-workspace",
            str(root),
            "--output",
            str(tmp_path / "merged"),
        ],
    )
    assert outcome.exit_code == 1, outcome.output
    assert not (tmp_path / "merged" / "graph_state.json").exists()
    assert not any(operator.calls for operator in operators)


@pytest.mark.parametrize("base_option", ["--existing-state", "--graph-state"])
def test_split_can_integrate_into_an_existing_graph(
    tmp_path, monkeypatch, command_inputs, base_option
):
    config, pairs = command_inputs
    install(monkeypatch, Engine())
    existing = GraphState(graph=GraphSpace(nodes=(table("old"),)), node_conflicts=())
    base = tmp_path / (
        "base.json" if base_option == "--existing-state" else "base-bundle"
    )
    if base_option == "--existing-state":
        base.write_text(existing.model_dump_json())
    else:
        FileSystemGraphStateRepository(base, SCHEMA).save(existing)
    runner = CliRunner()
    result, summary = invoke(
        runner,
        [
            "sql-extract",
            "--config",
            str(config),
            "--training-set",
            str(pairs),
            base_option,
            str(base),
            "--output",
            str(tmp_path / "extract"),
        ],
        tmp_path / "summary.json",
    )
    assert result.exit_code == 0, result.output
    if base_option == "--graph-state":
        assert FileSystemGraphStateRepository(base, SCHEMA).load() == existing
    merged = runner.invoke(
        main,
        [
            "graph-merge",
            "--config",
            str(config),
            "--training-workspace",
            summary["output"],
            base_option,
            str(base),
            "--output",
            str(tmp_path / "merged"),
        ],
    )
    assert merged.exit_code == 0, merged.output
    state = GraphState.model_validate_json(
        (tmp_path / "merged" / "graph_state.json").read_text()
    )
    assert {n.properties["table_name"] for n in state.graph.nodes} == {"old", "new"}
    if base_option == "--graph-state":
        assert FileSystemGraphStateRepository(base, SCHEMA).load() == state


def test_changed_persistent_base_fails_without_overwriting_it(
    tmp_path, monkeypatch, command_inputs
):
    config, pairs = command_inputs
    operators = install(monkeypatch, Engine())
    bundle = tmp_path / "base"
    runner = CliRunner()
    _, summary = invoke(
        runner,
        [
            "sql-extract",
            "--config",
            str(config),
            "--training-set",
            str(pairs),
            "--graph-state",
            str(bundle),
            "--output",
            str(tmp_path / "extract"),
        ],
        tmp_path / "summary.json",
    )
    changed = GraphState(
        graph=GraphSpace(nodes=(table("concurrent"),)), node_conflicts=()
    )
    repository = FileSystemGraphStateRepository(bundle, SCHEMA)
    repository.save(changed)
    outcome = runner.invoke(
        main,
        [
            "graph-merge",
            "--config",
            str(config),
            "--training-workspace",
            summary["output"],
            "--graph-state",
            str(bundle),
            "--output",
            str(tmp_path / "merged"),
        ],
    )
    assert outcome.exit_code == 1
    result = json.loads((tmp_path / "merged" / "result.json").read_text())
    assert result["status"] == "failed"
    assert (
        "stored graph does not match"
        in result["diagnostics"]["context"]["failure_message"]
    )
    assert repository.load() == changed
    assert not any(operator.calls for operator in operators)


def test_existing_state_mismatch_is_rejected(tmp_path, monkeypatch, command_inputs):
    config, pairs = command_inputs
    install(monkeypatch, Engine())
    runner = CliRunner()
    _, summary = invoke(
        runner,
        [
            "sql-extract",
            "--config",
            str(config),
            "--training-set",
            str(pairs),
            "--output",
            str(tmp_path / "extract"),
        ],
        tmp_path / "summary.json",
    )
    wrong = tmp_path / "wrong.json"
    wrong.write_text(
        GraphState(
            graph=GraphSpace(nodes=(table("wrong"),)), node_conflicts=()
        ).model_dump_json()
    )
    result = runner.invoke(
        main,
        [
            "graph-merge",
            "--config",
            str(config),
            "--training-workspace",
            summary["output"],
            "--existing-state",
            str(wrong),
            "--output",
            str(tmp_path / "merged"),
        ],
    )
    assert result.exit_code == 1
    assert "does not match extraction existing_state" in result.output


def test_cli_lists_both_independent_stages():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "sql-extract" in result.output and "graph-merge" in result.output


def test_failed_resume_removes_previous_final_graph_exports(
    tmp_path, monkeypatch, command_inputs
):
    config, pairs = command_inputs
    install(monkeypatch, Engine())
    runner = CliRunner()
    _, summary = invoke(
        runner,
        [
            "sql-extract",
            "--config",
            str(config),
            "--training-set",
            str(pairs),
            "--output",
            str(tmp_path / "extract"),
        ],
        tmp_path / "summary.json",
    )
    args = [
        "graph-merge",
        "--config",
        str(config),
        "--training-workspace",
        summary["output"],
        "--run-id",
        "resume",
        "--output",
        str(tmp_path / "merged"),
    ]
    first = runner.invoke(main, args)
    assert first.exit_code == 0, first.output
    assert (tmp_path / "merged" / "graph_state.json").exists()
    # A subsequent failed attempt must not advertise the earlier graph as its output.
    install(monkeypatch, Engine(), error=RuntimeError("merge interrupted"))
    retry = runner.invoke(main, args)
    assert retry.exit_code == 1
    result = json.loads((tmp_path / "merged" / "result.json").read_text())
    assert result["status"] == "failed"
    for name in ("graph.json", "graph_state.json", "graph.html", "merge_report.json"):
        assert not (tmp_path / "merged" / name).exists()
