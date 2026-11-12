"""Interactive protocol tests with real SQLite and real online-context checkpoints."""

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from ontology_v2_fixtures import discussion_state

from cortex.application.online_context import (
    CandidateEvidenceAssessment,
    ChoicePoint,
    InteractionOption,
    OnlineContextService,
)
from cortex.application.retrieval import RelevanceDecision
from cortex.application.retrieve_db import RetrieveDbService
from cortex.capabilities.nl2sql.database import QueryDatabase
from cortex.capabilities.nl2sql.grounding import ground_sql
from cortex.capabilities.online_context.engine import OnlineContextCapability
from cortex.capabilities.online_context.output.guidance import GuidanceSynthesis
from cortex.capabilities.retrieve_db.engine import RetrieveDbCapability
from cortex.schema import GraphState, load_root_schema
from cortex.web.app import create_app


class Agent:
    async def act(self, **kwargs):
        if kwargs["stage"] == "draft":
            return {
                "action": "sql",
                "sql": "SELECT COUNT(users.Id) FROM users JOIN posts ON users.Id = posts.OwnerUserId",
            }
        # The selected real ontology rule and user confirmation must reach the new agent.
        assert kwargs["context"]["user_confirmations"]
        assert kwargs["context"]["context"]["guidance"]
        return {
            "action": "sql",
            "sql": "SELECT COUNT(DISTINCT users.Id) AS user_count FROM users JOIN posts ON users.Id = posts.OwnerUserId",
            "explanation": "Count each user once",
        }

    async def review(self, **kwargs):
        return {"consistent": True, "issues": ""}

    async def summarize(self, **kwargs):
        return f"There are {kwargs['result']['rows'][0][0]} distinct users."


@pytest.fixture
def configured(tmp_path):
    dbroot = tmp_path / "databases"
    path = dbroot / "financial" / "financial.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE users(Id INTEGER PRIMARY KEY); CREATE TABLE posts(Id INTEGER PRIMARY KEY,OwnerUserId INTEGER REFERENCES users(Id)); INSERT INTO users VALUES (1),(2); INSERT INTO posts VALUES (1,1),(2,1),(3,2);"
        )
    state = discussion_state()
    # Match any additional physical fields carried by the current-contract fixture.
    tables = {
        n.id: n.properties["table_name"]
        for n in state.graph.nodes
        if n.node_type == "table"
    }
    with sqlite3.connect(path) as db:
        actual = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for name in tables.values():
            if name not in actual:
                db.execute(
                    'CREATE TABLE "'
                    + name.replace('"', '""')
                    + '" ("__placeholder" INTEGER)'
                )
        for n in state.graph.nodes:
            if n.node_type == "column":
                table = tables[n.properties["table_ref"]]
                col = n.properties["column_name"]
                existing = {
                    r[1]
                    for r in db.execute(
                        'PRAGMA table_info("' + table.replace('"', '""') + '")'
                    )
                }
                if col not in existing:
                    db.execute(
                        'ALTER TABLE "'
                        + table.replace('"', '""')
                        + '" ADD COLUMN "'
                        + col.replace('"', '""')
                        + '" INTEGER'
                    )
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "financial.graph.json").write_text(
        json.dumps({"state": state.model_dump(mode="json")})
    )
    calls = {"retrieve": 0, "resume": 0}
    conflict = state.node_conflicts[0]

    class Selector:
        async def select(self, *, representations, **kwargs):
            calls["retrieve"] += 1
            return tuple(
                RelevanceDecision(
                    document_id=r.document.document_id,
                    status="relevant",
                    reason="Count policy",
                )
                for r in representations
            )

    class Organizer:
        async def organize(self, **kwargs):
            calls["organized_question"] = kwargs["question"]
            calls["organized_initial_sql"] = kwargs["initial_sql"]
            organization = SimpleNamespace(
                choice_points=(
                    ChoicePoint(
                        choice_point_id="count-unit",
                        decision_question="Count each user once or every joined record?",
                        scope="calculation",
                        options=tuple(
                            InteractionOption(
                                option_id=a.alternative_id,
                                statement="Count each user once"
                                if i == 0
                                else "Count every joined record",
                                candidate_sources=("memory_graph",),
                                graph_refs=(a.alternative_id,),
                                evidence_assessment=CandidateEvidenceAssessment(
                                    source_types=("ontology_asset",),
                                    applicability="applicable",
                                    explanation="Count policy",
                                ),
                            )
                            for i, a in enumerate(conflict.alternatives)
                        ),
                    ),
                ),
                intra_node_assessments=(),
            )

            if calls.get("rounds", 1) > 1:
                point = organization.choice_points[0]
                organization.choice_points = (
                    point,
                    point.model_copy(
                        update={
                            "choice_point_id": "second-count-check",
                            "decision_question": "Confirm the output unit for this report",
                        }
                    ),
                )
            return organization

    class Guidance:
        async def synthesize(self, **kwargs):
            return GuidanceSynthesis(
                guidance_content="Count each user once", interaction_guidance=()
            )

    agent_holder = [Agent()]

    def runtime(directory, *, on_retrieved=None):
        schema = load_root_schema()
        retrieval = RetrieveDbService(
            RetrieveDbCapability(
                relevance_selector=Selector(),
                ontology_schema=schema,
                checkpoint_root=directory / "retrieval",
            ),
            schema=schema,
        )
        online = OnlineContextService(
            OnlineContextCapability(
                on_retrieved=on_retrieved,
                retrieval_service=retrieval,
                choice_organizer=Organizer(),
                guidance_synthesizer=Guidance(),
                ontology_schema=schema,
                checkpoint_path=directory / "online.sqlite",
            ),
            schema=schema,
        )
        return agent_holder[0], online

    def factory(agent=None):
        if agent is not None:
            agent_holder[0] = agent
        return create_app(
            database_root=dbroot,
            storage=tmp_path / "storage",
            examples=examples,
            query_runtime_factory=runtime,
        )

    return factory, calls, state


def wait_task(client, key, statuses):
    for _ in range(150):
        task = client.get("/api/nl2sql/tasks/" + key).json()
        if task["status"] in statuses:
            return task
        time.sleep(0.03)
    pytest.fail(str(task))


def test_real_online_pause_restart_answer_execute_and_event_replay(configured):
    factory, calls, _state = configured
    with TestClient(factory()) as client:
        session = client.post(
            "/ontology/api/nl2sql/sessions",
            json={
                "database_id": "financial",
                "ontology_source": {"kind": "archive"},
                "request_id": "session",
            },
        ).json()
        assert "id" in session, session
        response = client.post(
            "/api/nl2sql/sessions/" + session["id"] + "/tasks",
            json={"question": "How many users have posts?", "request_id": "task"},
        )
        assert response.status_code == 202, response.text
        key = response.json()["id"]
        task = wait_task(client, key, {"awaiting_user", "failed"})
        assert task["status"] == "awaiting_user", task
        assert "pending" not in task
        assert task["graphs"]["retrieved"]
        assert calls["retrieve"] == 1
        question = task["interaction"]["questions"][0]
        answer = {
            "interaction_id": task["interaction"]["id"],
            "expected_state_version": task["state_version"],
            "request_id": "answer",
            "answers": [
                {
                    "question_id": question["question_id"],
                    "selected_option_ids": [question["options"][0]["option_id"]],
                }
            ],
        }
        invalid = {
            **answer,
            "answers": [
                {"question_id": question["question_id"], "selected_option_ids": ["bad"]}
            ],
        }
        assert (
            client.post(
                "/api/nl2sql/tasks/" + key + "/answers", json=invalid
            ).status_code
            == 422
        )
    # New API instance reopens the SAME real online-context checkpoint.
    with TestClient(factory()) as client:
        assert (
            client.get("/api/nl2sql/tasks/" + key).json()["status"] == "awaiting_user"
        )
        response = client.post("/api/nl2sql/tasks/" + key + "/answers", json=answer)
        assert response.status_code == 202, response.text
        assert (
            client.post(
                "/api/nl2sql/tasks/" + key + "/answers", json=answer
            ).status_code
            == 202
        )
        done = wait_task(client, key, {"completed", "failed"})
        assert done["status"] == "completed", done
        assert done["result"]["rows"] == [[2]]
        assert done["sql_revisions"][-1]["status"] == "executed"
        assert done["sql_revisions"][-1]["grounding"]["bindings"]
        assert done["graphs"]["processed"]
        assert done["context"]["projection_trace"]
        assert calls["retrieve"] == 1  # resume does not retrieve again
        assert "2 distinct users" in done["answer"]
        event_text = client.get("/api/nl2sql/tasks/" + key + "/events?after=0").text
        assert (
            "clarification.requested" in event_text and "answer.completed" in event_text
        )
        last = done["last_event_seq"]
        assert (
            "event: update"
            not in client.get(f"/api/nl2sql/tasks/{key}/events?after={last}").text
        )
        assert (
            client.post(
                "/api/nl2sql/tasks/" + key + "/answers",
                json={**answer, "request_id": "stale"},
            ).status_code
            == 409
        )


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "SELECT 1; SELECT 2",
        "ATTACH DATABASE ':memory:' AS other",
        "CREATE TABLE x(id)",
        "SELECT load_extension('bad')",
    ],
)
def test_sql_write_and_function_guards(tmp_path, sql):
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE t(id)")
    with pytest.raises((ValueError, sqlite3.Error)):
        QueryDatabase(path).execute(sql)
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT count(*) FROM sqlite_master WHERE name='t'").fetchone()[
                0
            ]
            == 1
        )


def test_query_bounds_and_ast_scope_links(tmp_path):
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE t(id); INSERT INTO t VALUES(1),(2),(3);")
    assert QueryDatabase(path, max_rows=2).execute("SELECT * FROM t")["truncated"]
    graph = {
        "nodes": [
            {"id": "table.t", "node_type": "table", "properties": {"table_name": "t"}},
            {
                "id": "col.t.id",
                "node_type": "column",
                "properties": {"table_ref": "table.t", "column_name": "id"},
            },
        ],
        "edges": [],
    }
    sql = "SELECT '😀', x.id FROM t AS x"
    binding = ground_sql(sql, graph)["bindings"][0]
    # JavaScript UTF-16 ranges must select the actual identifier after an emoji.
    encoded = sql.encode("utf-16-le")
    assert (
        encoded[binding["start"] * 2 : binding["end"] * 2].decode("utf-16-le") == "id"
    )
    assert not ground_sql("SELECT q.id FROM (SELECT id FROM t) AS q", graph)[
        "bindings"
    ][0]["expression"].startswith("q.")


def start_and_choose(client, *, suffix="one"):
    session = client.post(
        "/api/nl2sql/sessions",
        json={
            "database_id": "financial",
            "ontology_source": {"kind": "archive"},
            "request_id": "session-" + suffix,
        },
    ).json()
    task = client.post(
        "/api/nl2sql/sessions/" + session["id"] + "/tasks",
        json={"question": "How many users have posts?", "request_id": "task-" + suffix},
    ).json()
    waiting = wait_task(client, task["id"], {"awaiting_user", "failed"})
    assert waiting["status"] == "awaiting_user", waiting
    q = waiting["interaction"]["questions"][0]
    answer = {
        "interaction_id": waiting["interaction"]["id"],
        "expected_state_version": waiting["state_version"],
        "request_id": "answer-" + suffix,
        "answers": [
            {
                "question_id": q["question_id"],
                "selected_option_ids": [q["options"][0]["option_id"]],
            }
        ],
    }
    return session, waiting, answer


def test_sql_failure_repairs_and_preserves_versions(configured):
    factory, _, _state = configured

    class RepairAgent(Agent):
        async def act(self, **kwargs):
            if kwargs["stage"] == "final" and not kwargs["feedback"]:
                return {"action": "sql", "sql": "SELECT nonexistent FROM users"}
            return await super().act(**kwargs)

    with TestClient(factory(RepairAgent())) as client:
        _, waiting, answer = start_and_choose(client)
        client.post("/api/nl2sql/tasks/" + waiting["id"] + "/answers", json=answer)
        done = wait_task(client, waiting["id"], {"completed", "failed"})
        assert done["status"] == "completed", done
        assert [v["status"] for v in done["sql_revisions"]] == ["failed", "executed"]
        assert "nonexistent" in done["sql_revisions"][0]["error"]
        assert done["sql_revisions"][-1]["meaning_review"]["consistent"]


def test_semantic_review_repairs_executable_wrong_meaning(configured):
    factory, _, _state = configured

    class ReviewAgent(Agent):
        async def act(self, **kwargs):
            if kwargs["stage"] == "final" and not kwargs["feedback"]:
                return {"action": "sql", "sql": "SELECT COUNT(*) FROM posts"}
            return await super().act(**kwargs)

        async def review(self, **kwargs):
            if "DISTINCT" not in kwargs["sql"]:
                return {
                    "consistent": False,
                    "issues": "User chose distinct users, not joined records",
                }
            return {"consistent": True, "issues": ""}

    with TestClient(factory(ReviewAgent())) as client:
        _, waiting, answer = start_and_choose(client)
        client.post("/api/nl2sql/tasks/" + waiting["id"] + "/answers", json=answer)
        done = wait_task(client, waiting["id"], {"completed", "failed"})
        assert done["status"] == "completed", done
        assert done["sql_revisions"][0]["meaning_review"]["consistent"] is False
        assert done["result"]["rows"] == [[2]]


def test_empty_query_result_is_success(configured):
    factory, _, _state = configured

    class EmptyAgent(Agent):
        async def act(self, **kwargs):
            if kwargs["stage"] == "final":
                return {
                    "action": "sql",
                    "sql": "SELECT users.Id FROM users WHERE users.Id < 0",
                }
            return await super().act(**kwargs)

        async def summarize(self, **kwargs):
            assert kwargs["result"]["rows"] == []
            return "No matching users."

    with TestClient(factory(EmptyAgent())) as client:
        _, waiting, answer = start_and_choose(client)
        client.post("/api/nl2sql/tasks/" + waiting["id"] + "/answers", json=answer)
        done = wait_task(client, waiting["id"], {"completed", "failed"})
        assert done["status"] == "completed", done
        assert done["result"]["rows"] == [] and done["result"]["row_count"] == 0


def test_stale_answers_task_idempotency_and_cancel(configured):
    factory, _, _state = configured
    with TestClient(factory()) as client:
        session, waiting, answer = start_and_choose(client)
        path = "/api/nl2sql/sessions/" + session["id"] + "/tasks"
        same = client.post(
            path,
            json={"question": "How many users have posts?", "request_id": "task-one"},
        )
        assert same.json()["id"] == waiting["id"]
        assert (
            client.post(
                path, json={"question": "Different question", "request_id": "task-one"}
            ).status_code
            == 409
        )
        stale = {**answer, "expected_state_version": 0}
        assert (
            client.post(
                "/api/nl2sql/tasks/" + waiting["id"] + "/answers", json=stale
            ).status_code
            == 409
        )
        assert (
            client.post("/api/nl2sql/tasks/" + waiting["id"] + "/cancel").json()[
                "status"
            ]
            == "cancelled"
        )
        assert (
            client.post(
                "/api/nl2sql/tasks/" + waiting["id"] + "/answers", json=answer
            ).status_code
            == 409
        )
        assert client.get("/api/nl2sql/tasks/" + waiting["id"]).json()["result"] is None
    with TestClient(factory()) as client:
        assert (
            client.get("/api/nl2sql/tasks/" + waiting["id"]).json()["status"]
            == "cancelled"
        )


def test_interrupted_agent_can_resume_after_restart(configured):
    import asyncio

    factory, _, _state = configured

    class OnceSlowAgent(Agent):
        calls = 0

        async def act(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(30)
            return await super().act(**kwargs)

    agent = OnceSlowAgent()
    with TestClient(factory(agent)) as client:
        _, task, answer = start_and_choose(client, suffix="interrupt")
        assert agent.calls == 0  # ontology clarification precedes any SQL-model call
        client.post("/api/nl2sql/tasks/" + task["id"] + "/answers", json=answer)
        for _ in range(100):
            if agent.calls:
                break
            time.sleep(0.01)
        assert agent.calls == 1
    with TestClient(factory()) as client:
        assert (
            client.get("/api/nl2sql/tasks/" + task["id"]).json()["status"]
            == "interrupted"
        )
        assert (
            client.post("/api/nl2sql/tasks/" + task["id"] + "/retry").status_code == 202
        )
        done = wait_task(client, task["id"], {"completed", "failed"})
        assert done["status"] == "completed", done


def test_sql_timeout_interrupts_recursive_query(tmp_path):
    path = tmp_path / "db.sqlite"
    sqlite3.connect(path).close()
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        QueryDatabase(path, timeout=0.01).execute(
            "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM nums) SELECT sum(n) FROM nums"
        )


def test_multiple_real_online_rounds_reuse_retrieval(configured):
    factory, calls, _state = configured
    calls["rounds"] = 2
    with TestClient(factory()) as client:
        _, waiting, first = start_and_choose(client)
        path = "/api/nl2sql/tasks/" + waiting["id"]
        client.post(path + "/answers", json=first)
        second = wait_task(client, waiting["id"], {"awaiting_user", "failed"})
        assert second["status"] == "awaiting_user", second
        assert second["interaction"]["id"] != waiting["interaction"]["id"]
        assert second["sql_revisions"] == []
        question = second["interaction"]["questions"][0]
        answer = {
            "request_id": "second-answer",
            "interaction_id": second["interaction"]["id"],
            "expected_state_version": second["state_version"],
            "answers": [
                {
                    "question_id": question["question_id"],
                    "selected_option_ids": [question["options"][0]["option_id"]],
                }
            ],
        }
        client.post(path + "/answers", json=answer)
        done = wait_task(client, waiting["id"], {"completed", "failed"})
        assert done["status"] == "completed", done
        assert len(done["confirmations"]) == 2 and calls["retrieve"] == 1


def test_free_text_answer_is_consumed_as_custom_guidance(configured):
    factory, _, _state = configured
    with TestClient(factory()) as client:
        _, waiting, answer = start_and_choose(client)
        answer["answers"][0]["selected_option_ids"] = []
        answer["answers"][0]["explanation"] = (
            "Count each user once, even when they have multiple posts."
        )
        response = client.post(
            "/api/nl2sql/tasks/" + waiting["id"] + "/answers", json=answer
        )
        assert response.status_code == 202, response.text
        done = wait_task(client, waiting["id"], {"completed", "failed", "needs_review"})
        assert done["status"] == "completed", done
        assert done["context"]["status"] == "completed_with_custom_guidance"
        assert done["confirmations"][0]["explanation"].startswith(
            "Count each user once"
        )


@pytest.mark.parametrize(
    "context_status,expected_status",
    [("no_match", "completed"), ("unavailable", "failed")],
)
def test_missing_context_is_explicit_and_unavailable_is_retryable(
    configured, context_status, expected_status
):
    from cortex.application.online_context import (
        OnlineContextResult,
        calculate_graph_state_fingerprint,
    )

    factory, _, _state = configured

    class SchemaAgent(Agent):
        async def act(self, **kwargs):
            return {"action": "sql", "sql": "SELECT COUNT(*) FROM users"}

    app = factory(SchemaAgent())
    original = app.state.query_service.runtime_factory

    class MissingOnline:
        async def run(self, **kwargs):
            return OnlineContextResult(
                status=context_status,
                question=kwargs["question"],
                graph_state_fingerprint=calculate_graph_state_fingerprint(
                    kwargs["graph_state"]
                ),
            )

    def runtime(directory, **kwargs):
        agent, _online = original(directory, **kwargs)
        return agent, MissingOnline()

    app.state.query_service.runtime_factory = runtime
    with TestClient(app) as client:
        session = client.post(
            "/api/nl2sql/sessions",
            json={
                "database_id": "financial",
                "ontology_source": {"kind": "archive"},
                "request_id": "missing-context",
            },
        ).json()
        task = client.post(
            "/api/nl2sql/sessions/" + session["id"] + "/tasks",
            json={"question": "How many users?", "request_id": "missing-task"},
        ).json()
        done = wait_task(client, task["id"], {"completed", "failed"})
        assert done["status"] == expected_status, done
        assert done["context"]["status"] == context_status
        if context_status == "no_match":
            assert done["result"]["rows"] == [[2]]
            assert done["sql_revisions"][-1]["grounding"]["bindings"] == []
        else:
            assert done["sql_revisions"] == [] and done["result"] is None


def test_session_title_uses_first_question_and_survives_followups(configured):
    from cortex.capabilities.nl2sql.service import conversation_title

    assert (
        conversation_title("帮我统计账户所有者持有的高级信用卡情况。请先确认统计口径。")
        == "统计账户所有者持有的高级信用卡情况"
    )
    factory, _, _state = configured
    with TestClient(factory()) as client:
        session, waiting, _answer = start_and_choose(client)
        path = "/api/nl2sql/sessions/" + session["id"]
        first = client.get(path).json()
        client.post("/api/nl2sql/tasks/" + waiting["id"] + "/cancel")
        second = client.post(
            path + "/tasks",
            json={
                "question": "Now count something different",
                "request_id": "follow-up-title",
            },
        ).json()
        updated = client.get(path).json()
        assert updated["title"] == first["title"]
        assert updated["first_question"] == "How many users have posts?"
        client.post("/api/nl2sql/tasks/" + second["id"] + "/cancel")


def test_legacy_grounding_is_presented_as_precise_usage_without_changing_sql():
    from cortex.capabilities.nl2sql.service import public_task

    graph = {
        "nodes": [
            {
                "id": "table.card",
                "node_type": "table",
                "properties": {"table_name": "card"},
            },
            {
                "id": "table.disp",
                "node_type": "table",
                "properties": {"table_name": "disp"},
            },
            {
                "id": "column.card.type",
                "node_type": "column",
                "properties": {"table_ref": "table.card", "column_name": "type"},
            },
            {
                "id": "column.disp.type",
                "node_type": "column",
                "properties": {"table_ref": "table.disp", "column_name": "type"},
            },
        ],
        "edges": [],
    }
    sql = (
        "SELECT 1 FROM card AS c, disp AS d WHERE c.type = 'gold' AND d.type = 'OWNER'"
    )
    original = {
        "graphs": {"processed": graph},
        "sql_revisions": [
            {
                "sql": sql,
                "graph_snapshot": "processed",
                "status": "executed",
                "grounding": {"bindings": []},
            }
        ],
    }
    display = public_task(original)
    revision = display["sql_revisions"][0]
    assert {b["expression"] for b in revision["grounding"]["bindings"]} == {
        "c.type = 'gold'",
        "d.type = 'OWNER'",
    }
    assert all(b["clause"] == "where" for b in revision["grounding"]["bindings"])
    assert revision["sql"] == sql and revision["status"] == "executed"
    assert original["sql_revisions"][0]["grounding"] == {"bindings": []}


def test_natural_question_is_clarified_by_graph_before_agent_generation(configured):
    factory, calls, _state = configured

    class NoPrematureAgent(Agent):
        calls = 0

        async def act(self, **kwargs):
            self.calls += 1
            assert kwargs["stage"] == "final"
            assert kwargs["context"]["user_confirmations"]
            return await super().act(**kwargs)

    agent = NoPrematureAgent()
    with TestClient(factory(agent)) as client:
        _, waiting, answer = start_and_choose(client)
        assert agent.calls == 0
        assert waiting["interaction"]["source"] == "online_context"
        assert waiting["question"] == "How many users have posts?"
        assert waiting["initial_sql"] is None
        assert calls["organized_question"] == "How many users have posts?"
        assert calls["organized_initial_sql"] is None
        assert all(
            o["graph_refs"]
            for q in waiting["interaction"]["questions"]
            for o in q["options"]
        )
        assert calls["retrieve"] == 1
        client.post("/api/nl2sql/tasks/" + waiting["id"] + "/answers", json=answer)
        done = wait_task(client, waiting["id"], {"completed", "failed"})
        assert done["status"] == "completed", done
        assert agent.calls == 1 and calls["retrieve"] == 1


def test_archiving_smoke_session_keeps_original_task_accessible(configured):
    factory, _, _state = configured
    with TestClient(factory()) as client:
        session, waiting, _answer = start_and_choose(client)
        path = "/api/nl2sql/sessions/" + session["id"]
        assert client.post(path + "/archive").status_code == 409
        client.post("/api/nl2sql/tasks/" + waiting["id"] + "/cancel")
        assert client.post(path + "/archive").status_code == 200
        assert not client.get("/api/nl2sql/sessions").json()["sessions"]
        assert client.get("/api/nl2sql/tasks/" + waiting["id"]).status_code == 200
        assert client.get(path).status_code == 200


def test_graph_first_retrieval_closes_table_metadata_column_names():
    from cortex.application.ontology_graph import GraphManager
    from cortex.capabilities.retrieve_db.graph.context import (
        build_retrieved_graph_state,
        expand_context,
    )
    from cortex.schema import GraphNode, GraphSpace

    table = GraphNode(
        id="table.badges",
        node_type="table",
        name="badges",
        properties={
            "table_name": "badges",
            "primary_keys": ["Id"],
            "important_columns": ["UserId"],
            "available_time_columns": ["Date"],
        },
    )
    columns = tuple(
        GraphNode(
            id=f"column.badges.{name}",
            node_type="column",
            name=name,
            properties={"table_ref": table.id, "column_name": name},
        )
        for name in ("Id", "UserId", "Date")
    )
    state, _ = GraphManager(load_root_schema()).prepare_state(
        GraphState(graph=GraphSpace(nodes=(table, *columns)))
    )
    ids = expand_context(state, {table.id})
    assert {column.id for column in columns} <= ids
    GraphManager(load_root_schema()).validate_state(
        build_retrieved_graph_state(state, ids)
    )


def test_retrieval_replay_reassembles_dependencies_without_model_calls(tmp_path):
    import asyncio

    from cortex.application.retrieve_db import RetrieveDbResult, RetrieveDbTraceStep
    from cortex.capabilities.retrieve_db.workflow.runtime import RetrievalWorkflow

    class Nodes:
        version = "before-fix"
        selections = 0

        def signals(self, state):
            return {"initial_sql": None}

        def recall(self, state):
            return {"representations": []}

        def select(self, state):
            self.selections += 1
            return {"relevance": []}

        def assemble(self, state):
            return {
                "result": RetrieveDbResult(
                    status="no_match",
                    question=state["request"]["question"],
                    graph_fingerprint="same-graph",
                    run_id=state["request"]["run_id"],
                    trace=(
                        RetrieveDbTraceStep(
                            action="dependency_closure", observation=self.version
                        ),
                    ),
                ).model_dump(mode="json")
            }

    async def check():
        nodes = Nodes()
        workflow = RetrievalWorkflow(
            nodes, checkpoint_root=tmp_path / "checkpoints", context_id="same-context"
        )
        request = {
            "run_id": "resume-cache",
            "question": "one natural question",
            "graph_fingerprint": "same-graph",
        }
        first = await workflow.run(request)
        assert first.trace[0].observation == "before-fix"
        nodes.version = "complete-dependencies"
        resumed = await workflow.run(request)
        assert resumed.trace[0].observation == "complete-dependencies"
        assert nodes.selections == 1
        assert (await workflow.run(request)).trace == resumed.trace

    asyncio.run(check())


def test_display_relevance_excludes_uncertain_protected_and_dependency_nodes():
    from copy import deepcopy

    from cortex.capabilities.nl2sql.service import public_task

    representations = [
        {"document": {"document_id": doc, "canonical_node_id": node}}
        for doc, node in [
            ("r1", "rule"),
            ("r2", "rule"),
            ("u", "uncertain"),
            ("p", "protected"),
            ("c", "column"),
        ]
    ]
    relevance = [
        {"document_id": doc, "status": status}
        for doc, status in [
            ("r1", "irrelevant"),
            ("r2", "relevant"),
            ("u", "uncertain"),
            ("p", "irrelevant"),
            ("c", "relevant"),
        ]
    ]
    task = {
        "graphs": {
            "retrieved": {
                "nodes": [
                    {"id": n}
                    for n in ["rule", "column", "uncertain", "protected", "dependency"]
                ],
                "edges": [],
            }
        },
        "context_result": {"representations": representations, "relevance": relevance},
    }
    original = deepcopy(task)
    display = public_task(task)
    assert display["retrieval_selection"] == {"relevant_node_ids": ["column", "rule"]}
    assert display["graphs"] == original["graphs"]
    assert "context_result" not in display
    assert task == original
    assert public_task({})["retrieval_selection"] is None
