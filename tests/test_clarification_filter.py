"""Filter policy contracts with scripted model responses and real read-only SQLite."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from cortex.application.online_context import (
    CandidateEvidenceAssessment,
    ChoicePoint,
    InteractionAnswer,
    InteractionOption,
    OnlineContextService,
)
from cortex.application.retrieve_db import RetrieveDbResult
from cortex.capabilities.online_context.discovery.prioritization import (
    rank_choice_points,
)
from cortex.capabilities.online_context.engine import OnlineContextCapability
from cortex.capabilities.online_context.filtering import ClarificationFilterAgent
from cortex.capabilities.online_context.llm.prompts import load_prompt
from cortex.capabilities.online_context.output.guidance import GuidanceSynthesis
from cortex.infrastructure.online_context.probes import SQLiteFilterDatabase
from cortex.schema import GraphNode, GraphSpace, GraphState, load_root_schema


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "data.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE transactions_1k(TransactionID INTEGER, Time TEXT);"
            "INSERT INTO transactions_1k VALUES (1,'12:30:00'),(2,'11:00:00');"
        )
    return SQLiteFilterDatabase(path)


def graph():
    return GraphState(
        graph=GraphSpace(
            nodes=(
                GraphNode(
                    id="table.transactions_1k",
                    node_type="table",
                    name="transactions_1k",
                    properties={"table_name": "transactions_1k"},
                ),
            )
        )
    )


def slot(key="count", statements=("COUNT(*)", "COUNT(t.TransactionID)")):
    return ChoicePoint(
        choice_point_id=key,
        decision_question=f"Choose {key}",
        scope="calculation",
        options=tuple(
            InteractionOption(
                option_id=f"{key}:{index}",
                statement=statement,
                candidate_sources=("initial_sql",) if index == 0 else ("memory_graph",),
                initial_sql_evidence=statement if index == 0 else None,
                graph_refs=() if index == 0 else ("table.transactions_1k",),
                evidence_assessment=CandidateEvidenceAssessment(
                    source_types=("initial_sql",)
                    if index == 0
                    else ("ontology_asset",),
                    applicability="applicable",
                    explanation="Supported local implementation",
                ),
            )
            for index, statement in enumerate(statements)
        ),
    )


def llm(*steps):
    return FakeListChatModel(responses=[json.dumps(s) for s in steps])


def keep(point, reason="Insufficient evidence"):
    return {
        "choice_point_id": point.choice_point_id,
        "outcome": "keep",
        "reason": reason,
    }


def explicit(point, quote="morning means before 13:00", **overrides):
    return {
        "choice_point_id": point.choice_point_id,
        "outcome": "question_resolved",
        "question_evidence": quote,
        "excluded_options": {
            o.option_id: "Conflicts with the specified boundary"
            for o in point.options[1:]
        },
        "reason": "The evidence explicitly defines the boundary",
    } | overrides


def equivalent(point, **overrides):
    return {
        "choice_point_id": point.choice_point_id,
        "outcome": "database_equivalent",
        "covered_option_ids": [o.option_id for o in point.options],
        "data_property": "TransactionID has no NULL in the full base table",
        "applicability_reason": "Both options count rows from that same base table",
        "probe_ids": ["probe:1"],
        "reason": "Base column is non-NULL",
    } | overrides


def request(point, sql="SELECT COUNT(*), COUNT(TransactionID) FROM transactions_1k"):
    return {
        "requests": [
            {
                "choice_point_id": point.choice_point_id,
                "tool": "query_database",
                "purpose": "Check full base-column NULL coverage without business filters",
                "sql": sql,
            }
        ]
    }


def run_filter(
    model, points, database=None, question="How many transactions?", **settings
):
    return asyncio.run(
        ClarificationFilterAgent(
            model,
            database=database,
            **settings,
        ).filter(
            question=question,
            initial_sql="SELECT COUNT(*) FROM transactions_1k t WHERE Time < '13:00:00'",
            retrieved_graph_state=graph(),
            choice_points=points,
            dialect="sqlite",
        )
    )


def test_explicit_constraint_needs_no_database():
    point = slot("time", ("t.Time < '13:00:00'", "t.Time < '12:00:00'"))
    result = run_filter(
        llm({"decisions": [explicit(point)]}),
        (point,),
        question="Count morning transactions. Evidence: morning means before 13:00",
    )
    assert result.choice_points == ()
    assert result.observations == ()
    assert "guidance" not in result.model_dump()
    assert "selected_option_id" not in result.decisions[0].model_dump()


@pytest.mark.parametrize(
    "overrides",
    [
        {"question_evidence": "invented evidence"},
        {"excluded_options": {}},
        {"selected_option_id": "invented-option"},
        {"excluded_options": {"count:1": " "}},
    ],
)
def test_invalid_explicit_resolution_keeps_question(overrides):
    point = slot()
    result = run_filter(
        llm({"decisions": [explicit(point, **overrides)]}),
        (point,),
        question="morning means before 13:00",
    )
    assert result.choice_points == (point,)
    assert result.decisions[0].outcome == "keep"
    assert "guidance" not in result.model_dump()


def test_agent_generated_base_probe_executes_and_removes_question(database):
    point = slot()
    result = run_filter(
        llm(request(point), {"decisions": [equivalent(point)]}),
        (point,),
        database,
    )
    assert result.choice_points == ()
    observation = result.observations[0]
    assert observation["result"]["rows"] == [[2, 2]]
    assert "WHERE" not in observation["sql"].upper()
    assert "JOIN" not in observation["sql"].upper()
    assert "guidance" not in result.model_dump()
    assert "selected_option_id" not in result.decisions[0].model_dump()


@pytest.mark.parametrize(
    "changes",
    [
        {"covered_option_ids": ["count:0"]},
        {"probe_ids": ["fabricated"]},
        {"data_property": ""},
        {"applicability_reason": ""},
    ],
)
def test_partial_or_unsupported_equivalence_keeps_whole_slot(database, changes):
    point = slot()
    result = run_filter(
        llm(request(point), {"decisions": [equivalent(point, **changes)]}),
        (point,),
        database,
    )
    assert result.choice_points == (point,)
    assert result.choice_points[0].options == point.options


def test_equivalence_without_sql_observation_is_rejected(database):
    point = slot()
    result = run_filter(llm({"decisions": [equivalent(point)]}), (point,), database)
    assert result.choice_points == (point,)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT missing FROM transactions_1k",
        "DELETE FROM transactions_1k",
        "SELECT 1; DROP TABLE transactions_1k",
    ],
)
def test_failed_or_write_probe_cannot_establish_equivalence(database, sql):
    point = slot()
    result = run_filter(
        llm(request(point, sql), {"decisions": [equivalent(point)]}),
        (point,),
        database,
    )
    assert result.choice_points == (point,)
    assert not result.observations[0]["success"]
    assert database.execute("SELECT COUNT(*) FROM transactions_1k")["rows"] == [[2]]


def test_unavailable_database_does_not_remove_question():
    point = slot()
    result = run_filter(
        llm(request(point), {"decisions": [equivalent(point)]}),
        (point,),
    )
    assert result.choice_points == (point,)
    assert result.observations[0]["error"] == "database tools unavailable"


@pytest.mark.parametrize(
    "sql, max_rows",
    [
        ("SELECT * FROM transactions_1k", 1),
        ("SELECT printf('%05000d',1) FROM transactions_1k", 200),
    ],
)
def test_truncated_observations_are_not_equivalence_evidence(database, sql, max_rows):
    limited = SQLiteFilterDatabase(database.database.path, max_rows=max_rows)
    point = slot()
    result = run_filter(
        llm(request(point, sql), {"decisions": [equivalent(point)]}),
        (point,),
        limited,
    )
    assert result.choice_points == (point,)
    assert result.observations[0]["success"]


def test_timeout_preserves_question(database):
    point = slot()
    database.database.timeout = 0.001
    sql = (
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000000) "
        "SELECT SUM(x) FROM n"
    )
    result = run_filter(
        llm(request(point, sql), {"decisions": [equivalent(point)]}),
        (point,),
        database,
    )
    assert result.choice_points == (point,)
    assert not result.observations[0]["success"]


def test_inspect_schema_works_but_cannot_replace_sql_evidence(database):
    point = slot()
    schema_request = {
        "requests": [
            {
                "choice_point_id": point.choice_point_id,
                "tool": "inspect_schema",
                "purpose": "Inspect declared constraints",
                "tables": ["transactions_1k"],
            }
        ]
    }
    result = run_filter(
        llm(schema_request, {"decisions": [equivalent(point)]}),
        (point,),
        database,
    )
    assert result.observations[0]["success"]
    assert result.choice_points == (point,)


def test_probe_budget_is_bounded_and_failure_keeps_questions(database):
    point = slot()
    result = run_filter(
        llm(request(point)), (point,), database, max_rounds=2, max_probes=1
    )
    assert len(result.observations) == 1
    assert result.choice_points == (point,)
    assert any("budget exhausted" in n for n in result.notes)


def test_invalid_final_coverage_fails_open():
    point = slot()
    result = run_filter(llm({"decisions": [keep(point), keep(point)]}), (point,))
    assert result.choice_points == (point,)
    assert any("duplicate" in n for n in result.notes)


def test_model_failure_preserves_questions():
    point = slot()
    # Both the initial response and the one structured repair are invalid.
    result = run_filter(FakeListChatModel(responses=["not JSON"]), (point,))
    assert result.choice_points == (point,)
    assert any("unavailable" in n for n in result.notes)


def test_foreign_slot_probe_cannot_be_used(database):
    first, second = slot("count"), slot("other")
    result = run_filter(
        llm(request(first), {"decisions": [keep(first), equivalent(second)]}),
        (first, second),
        database,
    )
    assert result.choice_points == (first, second)


def test_prompt_requires_properties_and_preserves_uncertain_joins():
    prompt = load_prompt("filter_clarifications")
    assert "Do not copy the initial SQL's currency/date/time filters or joins" in prompt
    assert "one-to-many join" in prompt
    assert "Outer joins can introduce NULL" in prompt
    assert "Finding base NULLs or duplicates" in prompt
    assert "Partial equivalence" in prompt


def test_base_uniqueness_does_not_imply_join_uniqueness(database):
    # Regression data showing why the Agent must explain applicability.
    with sqlite3.connect(database.database.path) as db:
        db.executescript(
            "CREATE TABLE details(TransactionID INTEGER);"
            "INSERT INTO details VALUES (1),(1),(2);"
        )
    rows = database.execute(
        "SELECT COUNT(t.TransactionID), COUNT(DISTINCT t.TransactionID) "
        "FROM transactions_1k t JOIN details d USING(TransactionID)"
    )["rows"]
    assert rows == [[3, 2]]
    point = slot(
        statements=("COUNT(t.TransactionID)", "COUNT(DISTINCT t.TransactionID)")
    )
    result = run_filter(
        llm(
            {
                "decisions": [
                    keep(point, "Base uniqueness does not survive a one-to-many join")
                ]
            }
        ),
        (point,),
        database,
    )
    assert result.choice_points == (point,)


def workflow(tmp_path, points, filter_agent):
    state = graph()

    class Retrieval:
        async def run(self, **kwargs):
            return RetrieveDbResult(
                status="completed",
                question=kwargs["question"],
                graph_fingerprint="test",
                retrieved_graph_state=state,
                selected_node_ids=("table.transactions_1k",),
            )

    class Organizer:
        async def organize(self, **kwargs):
            return SimpleNamespace(choice_points=points, intra_node_assessments=())

    class Guidance:
        async def synthesize(self, **kwargs):
            return GuidanceSynthesis(guidance_content="", interaction_guidance=())

    return state, OnlineContextService(
        OnlineContextCapability(
            retrieval_service=Retrieval(),
            choice_organizer=Organizer(),
            guidance_synthesizer=Guidance(),
            clarification_filter=filter_agent,
            checkpoint_path=tmp_path / "online.sqlite",
            question_budget=1,
        ),
        schema=load_root_schema(),
    )


def test_filter_persists_decisions_without_guidance_on_resume(tmp_path):
    resolved = slot("time", ("Time < '13:00:00'", "Time < '12:00:00'"))
    remaining = (slot("other"), slot("third"))
    points = (resolved, *remaining)
    agent = ClarificationFilterAgent(
        llm(
            {
                "decisions": [
                    explicit(resolved),
                    *(keep(p) for p in remaining),
                ]
            }
        )
    )
    state, online = workflow(tmp_path, points, agent)

    async def run():
        pending = await online.run(
            question="Count morning transactions; morning means before 13:00",
            graph_state=state,
        )
        assert pending.choice_points == rank_choice_points(remaining)
        assert pending.pending.remaining_budget == 0
        assert pending.interaction_guidance == ()
        # Rebuild the engine with the same identity; no Agent/model invocation on resume.
        _, rebuilt = workflow(tmp_path, points, agent)
        answer = (
            InteractionAnswer(
                question_id=pending.interaction_questions[0].question_id,
                selected_option_ids=(remaining[0].options[0].option_id,),
            ),
        )
        final = await rebuilt.resume(
            graph_state=state, pending=pending.pending, answers=answer
        )
        assert final.status == "completed_with_unresolved"
        assert len(final.unresolved_choice_point_ids) == 1
        assert final.filter_decisions == pending.filter_decisions
        assert final.interaction_guidance == pending.interaction_guidance
        assert (
            "MUST USE:" not in final.downstream_guidance.guidance_content
        )
        assert final.diagnostics.prioritization_policy == "source_score_v2"
        assert final.diagnostics.presented_questions == 1
        assert (
            await rebuilt.resume(
                graph_state=state,
                pending=pending.pending,
                answers=answer,
            )
            == final
        )

    asyncio.run(run())


def test_all_filtered_does_not_select_an_implementation(tmp_path):
    point = slot("time", ("Time < '13:00:00'", "Time < '12:00:00'"))
    agent = ClarificationFilterAgent(llm({"decisions": [explicit(point)]}))
    state, online = workflow(tmp_path, (point,), agent)
    result = asyncio.run(
        online.run(question="morning means before 13:00", graph_state=state)
    )
    assert result.status == "completed"
    assert result.pending is None and not result.choice_points
    assert result.diagnostics.presented_questions == 0
    assert result.filter_decisions[0].outcome == "question_resolved"
    assert "MUST USE:" not in result.downstream_guidance.guidance_content


def test_bootstrap_binds_configured_database_and_preserves_legacy_switch(
    database, monkeypatch
):
    from cortex.bootstrap import online_context as bootstrap
    from cortex.config import CortexConfig, SQLiteDatabaseConfig

    monkeypatch.setattr(
        bootstrap, "build_retrieve_db_service", lambda **kwargs: object()
    )
    config = CortexConfig(database=SQLiteDatabaseConfig(path=database.database.path))
    service = bootstrap.build_online_context_service(
        config=config,
        llm=llm({"decisions": [keep(slot())]}),
        embeddings=None,
    )
    engine = service._engine
    assert engine._clarification_filter.database.database.path == database.database.path
    assert engine._choice_organizer._audit_slots is False
    settings = config.services.online_context
    disabled = settings.model_copy(
        update={
            "clarification_filter": settings.clarification_filter.model_copy(
                update={"enabled": False}
            ),
        }
    )
    config = config.model_copy(
        update={
            "services": config.services.model_copy(update={"online_context": disabled}),
        }
    )
    legacy = bootstrap.build_online_context_service(
        config=config,
        llm=llm({"decisions": [keep(slot())]}),
        embeddings=None,
    )
    assert legacy._engine._clarification_filter is None
    assert legacy._engine._choice_organizer._audit_slots is True


def test_web_runtime_uses_session_database_instead_of_default_config(
    tmp_path, monkeypatch
):
    from cortex.bootstrap import online_context as bootstrap
    from cortex.capabilities.nl2sql.service import default_runtime
    from cortex.config import CortexConfig, SQLiteDatabaseConfig

    path = tmp_path / "database.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE t(id INTEGER)")
    wrong_config = CortexConfig(
        database=SQLiteDatabaseConfig(path=tmp_path / "other.sqlite")
    )
    monkeypatch.setattr("cortex.config.load_config", lambda *_: wrong_config)
    monkeypatch.setattr(
        "cortex.infrastructure.llm.create_chat_model", lambda *_: object()
    )
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(bootstrap, "build_online_context_service", build)
    default_runtime(tmp_path)
    assert captured["filter_database"].database.path == path


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "SELECT 1; DROP TABLE t",
        "SELECT SLEEP(1)",
        "SELECT GET_LOCK('x',1)",
        "SELECT LOAD_FILE('/tmp/private')",
        "SELECT * FROM t FOR UPDATE",
        "SELECT 1 INTO OUTFILE '/tmp/x'",
    ],
)
def test_mysql_probe_rejects_unsafe_sql_before_connecting(sql, monkeypatch):
    from cortex.infrastructure.online_context import probes

    def forbidden(*args, **kwargs):
        raise AssertionError("unsafe SQL reached database execution")

    monkeypatch.setattr(probes, "execute_query", forbidden)
    with pytest.raises(ValueError):
        probes.MySQLFilterDatabase("mysql+pymysql://unused").execute(sql)


def test_mysql_probe_marks_truncation(monkeypatch):
    from cortex.infrastructure.database.execution import QueryExecutionResult
    from cortex.infrastructure.online_context import probes

    monkeypatch.setattr(
        probes,
        "execute_query",
        lambda *args, **kwargs: QueryExecutionResult(
            success=True,
            result={"columns": ["id"], "rows": [[1], [2]]},
        ),
    )
    result = probes.MySQLFilterDatabase("mysql+pymysql://unused", max_rows=1).execute(
        "SELECT id FROM t"
    )
    assert result["truncated"] is True and result["rows"] == [[1]]


@pytest.mark.parametrize(
    "values, expected, statements",
    [
        ([(1,), (None,)], [[2, 1, 1]], ("COUNT(*)", "COUNT(TransactionID)")),
        (
            [(1,), (1,)],
            [[2, 2, 1]],
            ("COUNT(TransactionID)", "COUNT(DISTINCT TransactionID)"),
        ),
    ],
)
def test_base_nulls_or_duplicates_preserve_question(
    database, values, expected, statements
):
    with sqlite3.connect(database.database.path) as db:
        db.execute("DELETE FROM transactions_1k")
        db.executemany("INSERT INTO transactions_1k(TransactionID) VALUES (?)", values)
    point = slot(statements=statements)
    sql = "SELECT COUNT(*),COUNT(TransactionID),COUNT(DISTINCT TransactionID) FROM transactions_1k"
    result = run_filter(
        llm(
            request(point, sql),
            {"decisions": [keep(point, "The inspected property is insufficient")]},
        ),
        (point,),
        database,
    )
    assert result.observations[0]["result"]["rows"] == expected
    assert result.choice_points == (point,)


def test_partial_equivalence_preserves_all_three_options(database):
    point = slot(
        statements=("COUNT(*)", "COUNT(TransactionID)", "COUNT(DISTINCT TransactionID)")
    )
    result = run_filter(
        llm(
            request(point),
            {
                "decisions": [
                    equivalent(point, covered_option_ids=["count:0", "count:1"])
                ]
            },
        ),
        (point,),
        database,
    )
    assert result.choice_points == (point,)
    assert len(result.choice_points[0].options) == 3


def test_filter_runs_with_zero_user_question_budget(tmp_path):
    point = slot("time", ("Time < '13:00:00'", "Time < '12:00:00'"))
    state, online = workflow(
        tmp_path,
        (point,),
        ClarificationFilterAgent(llm({"decisions": [explicit(point)]})),
    )
    result = asyncio.run(
        online.run(
            question="morning means before 13:00",
            graph_state=state,
            question_budget=0,
        )
    )
    assert result.status == "completed"
    assert result.diagnostics.presented_questions == 0
    assert result.interaction_guidance == ()
