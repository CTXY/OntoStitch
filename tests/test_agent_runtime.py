from threading import Barrier

import pytest
from conftest import make_rule
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from cortex.capabilities.sql_training.learning.runtime import LearningAgent
from cortex.capabilities.sql_training.learning.session import LearningSession
from cortex.capabilities.sql_training.training.evidence import EvidenceStore
from cortex.config.models import SqlTrainingConfig


class ScriptModel(BaseChatModel):
    _script: list = PrivateAttr()
    _calls: list = PrivateAttr(default_factory=list)

    def __init__(self, script):
        super().__init__()
        self._script = list(script)

    @property
    def _llm_type(self):
        return "scripted-tools"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._calls.append(messages)
        response = self._script.pop(0)
        if callable(response):
            response = response(messages)
        return ChatResult(generations=[ChatGeneration(message=response)])


def call(name, args, ident="tool-1"):
    return {"name": name, "args": args, "id": ident, "type": "tool_call"}


def message(*calls):
    return AIMessage(content="", tool_calls=list(calls))


def session_for(case_graph, **config):
    store, database, _ = case_graph
    return LearningSession(
        mode="direct",
        record={
            "source_id": "case",
            "question": "How many orders?",
            "sql": "SELECT count(*) FROM orders",
        },
        graph=store,
        evidence=EvidenceStore(store.root.parent / "evidence"),
        database=database,
        database_url=database.url,
        dialect="sqlite",
        config=SqlTrainingConfig(**config),
    )


def finish():
    return message(
        call(
            "handoff",
            {
                "outcome": "ready_for_validation",
                "direct_type_coverage": ["concept", "metric", "generation_rule"],
            },
        )
    )


def test_real_runtime_edit_and_handoff(case_graph):
    session = session_for(case_graph)
    model = ScriptModel(
        [
            message(
                call(
                    "edit_graph",
                    {
                        "edit": {
                            "edit_id": "first",
                            "expected_revision": session.graph.working_revision,
                            "operations": [
                                {
                                    "operation": "create",
                                    "node_id": make_rule().id,
                                    "asset": make_rule().model_dump(mode="json"),
                                }
                            ],
                        }
                    },
                )
            ),
            finish(),
        ]
    )
    result = LearningAgent(model).run(session)
    assert result.direct_completed
    assert result.model_calls == 2
    assert result.delivered_revision == session.graph.working_revision
    assert len(session.graph.working.semantic_assets) == 1


def test_mixed_batch_is_fully_rejected_before_any_execution(case_graph):
    session = session_for(case_graph)
    start = session.graph.working_revision

    def check(messages):
        errors = [m for m in messages if isinstance(m, ToolMessage)]
        assert len(errors) == 2
        assert {m.tool_call_id for m in errors} == {"read", "write"}
        assert all(m.status == "error" for m in errors)
        return finish()

    model = ScriptModel(
        [
            message(
                call("inspect_graph", {}, "read"),
                call("edit_graph", {"edit": {}}, "write"),
            ),
            check,
        ]
    )
    result = LearningAgent(model).run(session)
    assert result.direct_completed
    assert session.tool_calls == 1  # only handoff actually ran
    assert session.graph.working_revision == start


def test_read_tools_actually_overlap(case_graph, monkeypatch):
    from langchain_core.tools import tool

    import cortex.capabilities.sql_training.learning.tools as tools_module

    session = session_for(case_graph)
    barrier = Barrier(2)

    @tool
    def inspect_graph():
        """A synchronized read used to check real ToolNode concurrency."""
        barrier.wait(timeout=5)
        return {"status": "ok"}

    @tool
    def read_evidence():
        """A second synchronized read."""
        barrier.wait(timeout=5)
        return {"status": "ok"}

    real_build = tools_module.build_tools
    monkeypatch.setattr(
        "cortex.capabilities.sql_training.learning.runtime.build_tools",
        lambda s: [inspect_graph, read_evidence, real_build(s)[-1]],
    )
    model = ScriptModel(
        [
            message(call("inspect_graph", {}, "a"), call("read_evidence", {}, "b")),
            finish(),
        ]
    )
    assert LearningAgent(model).run(session).direct_completed


def test_model_limit_does_not_skip_last_turn_tools(case_graph):
    session = session_for(case_graph, agent_max_steps=1)
    result = LearningAgent(ScriptModel([finish()])).run(session)
    assert result.direct_completed
    assert result.tool_calls == 1


def test_decision_limit_and_natural_completion(case_graph):
    session = session_for(case_graph, agent_max_steps=2)
    model = ScriptModel(
        [
            message(call("inspect_graph", {"mode": "nodes"}, "a")),
            message(call("inspect_graph", {"mode": "nodes"}, "b")),
        ]
    )
    result = LearningAgent(model).run(session)
    assert result.model_calls == 2
    assert result.outcome == "resource_limit"
    assert not result.direct_completed
    natural = LearningAgent(ScriptModel([AIMessage(content="Done")])).run(
        session_for(case_graph)
    )
    assert not natural.direct_completed


def test_investigation_is_scoped_readonly_and_bounded(case_graph):
    from cortex.capabilities.sql_training.learning.database import investigate_database

    session = session_for(case_graph)
    kwargs = {"mode": "query", "table": "", "column": "", "value": "", "limit": 1}
    rows = investigate_database(
        session, sql="SELECT region FROM orders ORDER BY id", **kwargs
    )
    assert rows["rows"] == [["North"]]
    assert rows["truncated"]
    with pytest.raises(ValueError, match="read-only"):
        investigate_database(session, sql="DELETE FROM orders", **kwargs)
    with pytest.raises(ValueError, match="outside"):
        investigate_database(session, sql="SELECT * FROM sqlite_master", **kwargs)
    with pytest.raises(ValueError, match="outside"):
        investigate_database(
            session,
            sql="SELECT name FROM sqlite_master WHERE EXISTS (WITH sqlite_master AS (SELECT 1 AS x) SELECT x FROM sqlite_master)",
            **kwargs,
        )
    allowed = investigate_database(
        session, sql="WITH x AS (SELECT region FROM orders) SELECT * FROM x", **kwargs
    )
    assert allowed["rows"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders WHERE id =",
        "SELECT * FROM orders AS x JOIN orders AS x ON x.id=x.id",
    ],
)
def test_investigation_syntax_error_is_a_recoverable_tool_message(case_graph, sql):
    session = session_for(case_graph)

    def check(messages):
        errors = [m for m in messages if isinstance(m, ToolMessage)]
        assert errors[-1].status == "error"
        assert "invalid investigation SQL" in errors[-1].content
        return finish()

    model = ScriptModel(
        [
            message(
                call(
                    "inspect_database",
                    {"mode": "query", "sql": sql},
                )
            ),
            check,
        ]
    )
    assert LearningAgent(model).run(session).direct_completed


def test_parallel_large_pages_are_externalized_without_breaking_tool_pairs(case_graph):
    session = session_for(case_graph)
    refs = [
        session.evidence.put(
            "large", {"assets": [{"id": str(n), "text": "z" * 60} for n in range(300)]}
        )
        for _ in range(4)
    ]

    def check(messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        assert len(results) == 4
        assert any("externalized" in m.content for m in results)
        assert len({m.tool_call_id for m in results}) == 4
        return finish()

    calls = [
        call("read_evidence", {"evidence_id": ref, "limit": 12000}, f"r{n}")
        for n, ref in enumerate(refs)
    ]
    result = LearningAgent(ScriptModel([message(*calls), check])).run(session)
    assert result.direct_completed


def test_threaded_and_pooled_tool_binding_keeps_wrappers():
    from langchain_core.tools import tool

    from cortex.infrastructure.llm.pool import (
        KeyScheduler,
        PooledChatModel,
        ThreadedChatModel,
    )

    @tool
    def example():
        """Example tool."""
        return "ok"

    model = ScriptModel([AIMessage(content="ok")])
    threaded = ThreadedChatModel(model)
    assert threaded.bind_tools([example]).bound is threaded
    pooled = PooledChatModel((model,), KeyScheduler(1))
    bound = pooled.bind_tools([example])
    assert bound.bound is pooled
    assert bound.invoke("hello").content == "ok"


def test_type_guides_default_to_compact_schema_and_link_complete_rules(case_graph):
    from cortex.capabilities.sql_training.learning.tools import build_tools

    session = session_for(case_graph)
    inspect_graph = build_tools(session)[0]
    for kind in ("concept", "metric", "generation_rule"):
        result = inspect_graph.invoke({"mode": "guide", "node_type": kind})
        assert "properties_schema" in result and result.get("status") != "truncated"
        contract = session.evidence.read(result["contract_ref"])["payload"]["contract"]
        assert len(contract) > 1000


def test_validation_query_execution_is_readonly_by_default(case_graph):
    from cortex.infrastructure.database.execution import execute_query

    _, database, _ = case_graph
    attempt = execute_query("DELETE FROM orders", database_url=database.url)
    assert not attempt.success
    count = execute_query("SELECT COUNT(*) FROM orders", database_url=database.url)
    assert count.result["rows"] == [[2]]


def test_physical_names_with_spaces_resolve_through_catalog_ids(case_graph):
    from sqlalchemy import text

    from cortex.capabilities.sql_training.builders import build_catalog_assets
    from cortex.capabilities.sql_training.graph.snapshots import GraphStore
    from cortex.capabilities.sql_training.learning.database import (
        GroundingDatabase,
        investigate_database,
    )
    from cortex.schema import load_root_schema

    old, database, workspace = case_graph
    with database.engine.begin() as connection:
        connection.execute(text('CREATE TABLE "Order Details" ("Region Name" TEXT)'))
        connection.execute(text("INSERT INTO \"Order Details\" VALUES ('North')"))
    catalog = database.introspect_catalog()
    assets = build_catalog_assets(catalog.tables, catalog.columns, catalog.joins)
    store = GraphStore(
        old.root.parent / "spaced-snapshots",
        workspace=workspace,
        schema=load_root_schema(),
    )
    store.initialize([*assets.tables, *assets.columns], list(assets.join_edges))
    session = session_for(case_graph)
    session.graph = store
    result = investigate_database(
        session,
        mode="metadata",
        table="Order Details",
        column="",
        value="",
        sql="",
        limit=10,
    )
    assert {row["id"] for row in result["result"]} == {
        "table.Order_Details",
        "column.Order_Details.Region_Name",
    }
    assert GroundingDatabase(session).find_exact_column_values(
        "Order_Details", "Region_Name", "North"
    ) == ["North"]
