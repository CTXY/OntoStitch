"""Full and serialized split training share the same gate and integration result."""

from dataclasses import dataclass, field

import pytest
from pydantic import ValidationError

from cortex.application import (
    QASqlTrainingRecord,
    RunStatus,
    SqlTrainingCaseResult,
    SqlTrainingEvaluationReport,
    SqlTrainingService,
    TrainingExtractionResult,
    extract_training,
    merge_training_extraction,
)
from cortex.application.merge_graph import DiagnosticCode, MergeDiagnostic
from cortex.application.ontology_graph import GraphManager
from cortex.bootstrap import build_graph_merge_operator, build_sql_extraction
from cortex.config import CortexConfig
from cortex.schema import GraphNode, GraphSpace, GraphState, load_root_schema


def empty():
    return GraphState(graph=GraphSpace(), node_conflicts=())


def table(name):
    return GraphNode(
        id=f"table.{name}",
        node_type="table",
        name=name,
        properties={"table_name": name},
    )


RECORDS = (QASqlTrainingRecord(source_id="case", question="Count", sql="SELECT 1"),)
SCHEMA = load_root_schema()


@dataclass
class Repository:
    state: GraphState = field(default_factory=empty)
    saves: list = field(default_factory=list)

    def load(self):
        return self.state

    def save(self, state):
        self.saves.append(state)
        self.state = state


class Engine:
    def __init__(self, report=None, state=None, error=None):
        self.report = report or SqlTrainingEvaluationReport(total=1, passed=1, failed=0)
        self.state = state or GraphState(
            graph=GraphSpace(nodes=(table("new"),)), node_conflicts=()
        )
        self.error = error
        self.calls = 0

    def train(self, records, schema, existing):
        self.calls += 1
        if self.error:
            raise self.error
        return self.state, self.report


class Merge:
    def __init__(self, diagnostics=(), error=None, invalid=False):
        self.operator = build_graph_merge_operator(CortexConfig(), live_semantics=False)
        self.diagnostics = diagnostics
        self.error = error
        self.invalid = invalid
        self.calls = 0

    def merge(self, existing, incoming, schema, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        result = self.operator.merge(existing, incoming, schema, **kwargs)
        if self.invalid:
            result = result.model_copy(
                update={
                    "state": GraphState(
                        graph=GraphSpace(
                            nodes=(
                                GraphNode(id="bad", node_type="unknown", name="bad"),
                            )
                        ),
                        node_conflicts=(),
                    )
                }
            )
        return result.model_copy(update={"diagnostics": self.diagnostics})


def portable(engine, base=None):
    value = extract_training(engine, RECORDS, SCHEMA, base)
    return TrainingExtractionResult.model_validate_json(value.model_dump_json())


def diagnostic(code):
    return MergeDiagnostic(code=code, element_kind="merge", message="review evidence")


@pytest.mark.parametrize(
    "report,code,status,merge_calls",
    [
        ({"total": 1, "passed": 1, "failed": 0}, None, RunStatus.COMPLETED, 1),
        ({"total": 1, "passed": 0, "failed": 1}, None, RunStatus.NEEDS_REVIEW, 1),
        (
            {"total": 1, "passed": 1, "failed": 0, "errors": ("case warning",)},
            None,
            RunStatus.NEEDS_REVIEW,
            1,
        ),
        (
            {"total": 1, "passed": 1, "failed": 0, "success": False},
            None,
            RunStatus.NEEDS_REVIEW,
            1,
        ),
        (
            {"total": 1, "passed": 0, "failed": 1, "fatal": True},
            None,
            RunStatus.FAILED,
            0,
        ),
        (
            {
                "total": 1,
                "passed": 0,
                "failed": 1,
                "cases": (
                    SqlTrainingCaseResult(source_id="case", passed=False, fatal=True),
                ),
            },
            None,
            RunStatus.FAILED,
            0,
        ),
        (
            {"total": 1, "passed": 1, "failed": 0},
            DiagnosticCode.MODEL_FAILURE,
            RunStatus.NEEDS_REVIEW,
            1,
        ),
        (
            {"total": 1, "passed": 1, "failed": 0},
            DiagnosticCode.CONSOLIDATION_PRESERVED_UNRESOLVED,
            RunStatus.NEEDS_REVIEW,
            1,
        ),
        (
            {"total": 1, "passed": 1, "failed": 0},
            DiagnosticCode.STALE_REFERENCE_EDGE_DROPPED,
            RunStatus.COMPLETED,
            1,
        ),
        (
            {"total": 1, "passed": 1, "failed": 0},
            DiagnosticCode.INVALIDATED_RELATIONSHIP_DROPPED,
            RunStatus.COMPLETED,
            1,
        ),
    ],
)
def test_full_and_split_have_identical_reports_gate_state_and_persistence(
    report, code, status, merge_calls
):
    engine = Engine(SqlTrainingEvaluationReport(**report))
    base = GraphState(graph=GraphSpace(nodes=(table("old"),)), node_conflicts=())
    repos = [Repository(base), Repository(base)]
    merges = [Merge((diagnostic(code),) if code else ()) for _ in range(2)]
    full = SqlTrainingService(
        engine, graph_merge=merges[0], state_repository=repos[0]
    ).run(
        RECORDS,
        SCHEMA,
        run_id="same-request",
    )
    handoff = portable(engine, base)
    split = merge_training_extraction(
        handoff,
        merges[1],
        SCHEMA,
        state_repository=repos[1],
        run_id="same-request",
    )
    assert full.status == split.status == status
    assert full.state == split.state
    assert full.evaluation_report == split.evaluation_report == engine.report
    assert full.merge_report == split.merge_report
    assert full.diagnostics.metrics == split.diagnostics.metrics
    assert full.diagnostics.context == split.diagnostics.context
    assert repos[0].saves == repos[1].saves
    assert [merge.calls for merge in merges] == [merge_calls, merge_calls]
    if merge_calls:
        assert full.merge_report.node_mappings
        assert full.state == full.merge_report.state
        assert full.merge_report.run_id == "same-request"
    else:
        assert full.merge_report is None
        assert not repos[0].saves
        assert full.state == base


@pytest.mark.parametrize("code", list(DiagnosticCode))
def test_diagnostic_policy_is_explicit_for_every_current_code(code):
    from cortex.application.sql_training import training_status

    merged = Merge((diagnostic(code),)).merge(empty(), Engine().state, SCHEMA)
    informational = {
        DiagnosticCode.STALE_REFERENCE_EDGE_DROPPED,
        DiagnosticCode.INVALIDATED_RELATIONSHIP_DROPPED,
    }
    expected = RunStatus.COMPLETED if code in informational else RunStatus.NEEDS_REVIEW
    assert training_status(Engine().report, merged) == expected


@pytest.mark.parametrize(
    "merge_kwargs,stage",
    [
        ({"error": RuntimeError("merge unavailable")}, "merge"),
        ({"invalid": True}, "final_validation"),
    ],
)
def test_merge_failures_preserve_report_and_do_not_publish_in_either_path(
    merge_kwargs, stage
):
    results = []
    for split in (False, True):
        engine, repo, operator = Engine(), Repository(), Merge(**merge_kwargs)
        result = (
            merge_training_extraction(
                portable(engine), operator, SCHEMA, state_repository=repo
            )
            if split
            else SqlTrainingService(
                engine, graph_merge=operator, state_repository=repo
            ).run(RECORDS, SCHEMA)
        )
        assert result.status == RunStatus.FAILED
        assert result.state == empty()
        assert result.evaluation_report == engine.report
        assert result.diagnostics.context["failure_stage"] == stage
        assert not repo.saves
        assert (result.merge_report is not None) == (stage == "final_validation")
        results.append(result)
    assert results[0].diagnostics.context == results[1].diagnostics.context


def test_extraction_infrastructure_failure_produces_fatal_handoff_and_skips_merge():
    for split in (False, True):
        engine, repo, operator = (
            Engine(error=RuntimeError("database disconnected")),
            Repository(),
            Merge(),
        )
        result = (
            merge_training_extraction(
                portable(engine), operator, SCHEMA, state_repository=repo
            )
            if split
            else SqlTrainingService(
                engine, graph_merge=operator, state_repository=repo
            ).run(RECORDS, SCHEMA)
        )
        assert result.status == RunStatus.FAILED
        assert result.evaluation_report.fatal
        assert result.evaluation_report.errors == ("database disconnected",)
        assert result.merge_report is None
        assert not repo.saves and operator.calls == 0


@pytest.mark.parametrize("records", [(), RECORDS * 2])
def test_record_gate_rejects_empty_and_duplicate_ids_before_engine(records):
    engine = Engine()
    with pytest.raises(ValueError):
        extract_training(engine, records, SCHEMA)
    with pytest.raises(ValueError):
        SqlTrainingService(
            engine, graph_merge=Merge(), state_repository=Repository()
        ).run(records, SCHEMA)
    assert engine.calls == 0


def test_inconsistent_extraction_report_fails_closed():
    extraction = portable(
        Engine(SqlTrainingEvaluationReport(total=0, passed=0, failed=0))
    )
    assert extraction.status == RunStatus.FAILED
    assert extraction.evaluation_report.errors == (
        "extraction report total does not match training records",
    )


@pytest.mark.parametrize("mismatch", ["schema", "base", "fragment"])
def test_invalid_handoff_context_never_reaches_merge_or_save(mismatch):
    extraction = portable(Engine())
    repo = Repository()
    if mismatch == "schema":
        extraction = extraction.model_copy(update={"schema_hash": "wrong"})
    elif mismatch == "base":
        repo.state = GraphState(
            graph=GraphSpace(nodes=(table("other"),)), node_conflicts=()
        )
    else:
        extraction = extraction.model_copy(
            update={
                "state": GraphState(
                    graph=GraphSpace(
                        nodes=(GraphNode(id="bad", node_type="unknown", name="bad"),)
                    ),
                    node_conflicts=(),
                )
            }
        )
    operator = Merge()
    result = merge_training_extraction(
        extraction, operator, SCHEMA, state_repository=repo
    )
    assert result.status == RunStatus.FAILED
    assert result.diagnostics.context["failure_stage"] == "input_validation"
    assert operator.calls == 0 and not repo.saves


def test_incremental_fragment_can_reference_existing_nodes():
    base = GraphState(graph=GraphSpace(nodes=(table("old"),)), node_conflicts=())
    column = GraphNode(
        id="column.old.id",
        node_type="column",
        name="id",
        properties={"column_name": "id", "table_ref": "table.old"},
    )
    incoming = GraphState(graph=GraphSpace(nodes=(column,)), node_conflicts=())
    extraction = portable(Engine(state=incoming), base)
    result = merge_training_extraction(extraction, Merge(), SCHEMA)
    assert result.status == RunStatus.COMPLETED, result.diagnostics
    canonical = next(
        m.canonical_id
        for m in result.merge_report.node_mappings
        if m.input_id == column.id
    )
    assert {node.id for node in result.state.graph.nodes} == {"table.old", canonical}
    assert (
        next(n for n in result.state.graph.nodes if n.id == canonical).properties[
            "table_ref"
        ]
        == "table.old"
    )
    GraphManager(SCHEMA).validate_state(result.state)


def test_repository_revision_failure_is_failed_and_preserves_merge_report():
    class ConcurrentRepository(Repository):
        def load_snapshot(self):
            return self.state, "before"

        def commit(self, state, expected_revision):
            assert expected_revision == "before"
            raise ValueError("revision changed")

    for split in (False, True):
        engine, repo, operator = Engine(), ConcurrentRepository(), Merge()
        result = (
            merge_training_extraction(
                portable(engine), operator, SCHEMA, state_repository=repo
            )
            if split
            else SqlTrainingService(
                engine, graph_merge=operator, state_repository=repo
            ).run(RECORDS, SCHEMA)
        )
        assert result.status == RunStatus.FAILED
        assert result.merge_report is not None
        assert result.state == empty()
        assert result.diagnostics.context["failure_stage"] == "persistence"
        assert result.diagnostics.context["failure_message"] == "revision changed"
        assert not repo.saves


def test_terminal_result_rejects_mismatched_successful_graph():
    result = merge_training_extraction(portable(Engine()), Merge(), SCHEMA)
    with pytest.raises(ValidationError, match="state must match"):
        type(result).model_validate({**result.model_dump(), "state": empty()})


def test_public_extraction_builder_does_not_construct_merge_or_embeddings(
    case_graph, monkeypatch
):
    from test_agent_runtime import ScriptModel
    from test_case_workflow import Validator

    from cortex.bootstrap import knowledge

    def forbidden(*args, **kwargs):
        pytest.fail("extraction builder initialized merge resources")

    monkeypatch.setattr(knowledge, "build_graph_merge_operator", forbidden)
    _store, database, workspace = case_graph
    capability = build_sql_extraction(
        CortexConfig(),
        workspace=workspace.root,
        llm=ScriptModel([]),
        database=database,
        validation_client=Validator(),
    )
    assert capability.database is database
    assert capability.runtime_config.workspace == str(workspace.root.resolve())
    assert callable(capability.train)


def test_existing_unresolved_conflicts_require_review_even_without_diagnostics():
    from conftest import make_rule

    from cortex.schema import NodeContent

    rule = GraphNode.model_validate(make_rule().model_dump(mode="json"))
    left = NodeContent.model_validate(
        rule.model_dump(include=set(NodeContent.model_fields))
    )
    right = left.model_copy(deep=True)
    right.properties["action"] = "Count distinct records."
    node, conflict = GraphManager.compact_conflict(
        rule, [("all", left, ()), ("distinct", right, ())]
    )
    base = GraphState(graph=GraphSpace(nodes=(node,)), node_conflicts=(conflict,))
    GraphManager(SCHEMA).validate_state(base)
    engine = Engine(state=empty())
    for split in (False, True):
        repo, operator = Repository(base), Merge()
        result = (
            merge_training_extraction(
                portable(engine, base), operator, SCHEMA, state_repository=repo
            )
            if split
            else SqlTrainingService(
                engine, graph_merge=operator, state_repository=repo
            ).run(RECORDS, SCHEMA)
        )
        assert result.status == RunStatus.NEEDS_REVIEW
        assert result.state.node_conflicts == (conflict,)
        assert result.merge_report.diagnostics == ()
        assert repo.saves == [result.state]


def test_merge_initialization_failure_and_fatal_gate_are_shared():
    calls = []

    def unavailable():
        calls.append("init")
        raise RuntimeError("embedding configuration missing")

    for fatal in (False, True):
        engine = Engine(
            SqlTrainingEvaluationReport(total=1, passed=0, failed=1, fatal=fatal)
        )
        for split in (False, True):
            before = len(calls)
            repo = Repository()
            result = (
                merge_training_extraction(
                    portable(engine),
                    None,
                    SCHEMA,
                    graph_merge_factory=unavailable,
                    state_repository=repo,
                )
                if split
                else SqlTrainingService(
                    engine, graph_merge_factory=unavailable, state_repository=repo
                ).run(RECORDS, SCHEMA)
            )
            assert result.status == RunStatus.FAILED and result.merge_report is None
            assert not repo.saves
            if fatal:
                assert len(calls) == before
                assert result.diagnostics.context["failure_stage"] == "extraction"
            else:
                assert len(calls) == before + 1
                assert result.diagnostics.context["failure_stage"] == "merge"
                assert (
                    result.diagnostics.context["failure_message"]
                    == "embedding configuration missing"
                )


def test_incoming_edge_mappings_survive_serialized_handoff(case_graph):
    store, _, _ = case_graph
    state = GraphState(
        graph=store.validation_graph(store.working_revision), node_conflicts=()
    )
    engine = Engine(state=state)
    full = SqlTrainingService(
        engine, graph_merge=Merge(), state_repository=Repository()
    ).run(RECORDS, SCHEMA, run_id="edges")
    split = merge_training_extraction(portable(engine), Merge(), SCHEMA, run_id="edges")
    assert full.status == split.status == RunStatus.COMPLETED
    assert full.merge_report == split.merge_report
    assert full.merge_report.edge_mappings
    assert {m.input_id for m in full.merge_report.edge_mappings} == {
        e.id for e in state.graph.edges
    }


def test_fatal_report_before_case_evaluation_is_preserved_verbatim():
    report = SqlTrainingEvaluationReport(
        total=0,
        passed=0,
        failed=0,
        fatal=True,
        success=False,
        errors=("catalog introspection failed",),
        workspace="extraction-workspace",
        trace_path="trace.jsonl",
    )
    extraction = portable(Engine(report))
    assert extraction.evaluation_report == report
    full = SqlTrainingService(
        Engine(report), graph_merge=Merge(), state_repository=Repository()
    ).run(RECORDS, SCHEMA)
    split = merge_training_extraction(extraction, None, SCHEMA)
    assert full.status == split.status == RunStatus.FAILED
    assert full.evaluation_report == split.evaluation_report == report
    assert full.merge_report is split.merge_report is None


@pytest.mark.parametrize(
    "cases,passed",
    [
        ((SqlTrainingCaseResult(source_id="case", passed=False),), 1),
        (
            (
                SqlTrainingCaseResult(source_id="case", passed=True),
                SqlTrainingCaseResult(source_id="case", passed=True),
            ),
            2,
        ),
    ],
)
def test_handoff_rejects_contradictory_case_counts_and_duplicate_case_ids(
    cases, passed
):
    with pytest.raises(ValidationError):
        SqlTrainingEvaluationReport(
            total=len(cases), passed=passed, failed=len(cases) - passed, cases=cases
        )
