import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import SecretStr, ValidationError
from typing_extensions import TypedDict

from cortex.capabilities.sql_training.prepare.sqlite import ensure_sqlite_database_ready
from cortex.capabilities.sql_training.runtime_config import _database
from cortex.config import load_config
from cortex.config.models import GraphMergeConfig, SQLiteDatabaseConfig
from cortex.infrastructure.workflow.checkpoints import persistent_checkpointer


def test_official_sqlite_checkpoint_survives_connection_reopen(tmp_path):
    class State(TypedDict):
        count: int

    builder = StateGraph(State)
    builder.add_node("increment", lambda state: {"count": state["count"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    url = SecretStr(f"sqlite:///{tmp_path / 'checkpoints.sqlite'}")
    config = {"configurable": {"thread_id": "durable-run"}}
    with persistent_checkpointer(url) as saver:
        graph = builder.compile(checkpointer=saver)
        assert graph.invoke({"count": 0}, config)["count"] == 1
    with persistent_checkpointer(url) as saver:
        assert builder.compile(checkpointer=saver).get_state(config).values == {
            "count": 1
        }


@pytest.mark.parametrize(
    "url", ["postgresql://localhost/cortex", "sqlite:///:memory:", "sqlite:///"]
)
def test_checkpoint_rejects_nonpersistent_or_external_backends(url):
    with pytest.raises(ValidationError):
        GraphMergeConfig(checkpoint_url=url)
    with pytest.raises(ValueError), persistent_checkpointer(SecretStr(url)):
        pass


def test_checkpoint_path_is_relative_to_config_not_working_directory(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    (root / "configs").mkdir(parents=True)
    path = root / "configs/cortex.yaml"
    path.write_text(
        "services:\n  graph_merge:\n    checkpoint_url: sqlite:///workspace/checkpoint.sqlite\n"
    )
    monkeypatch.chdir(tmp_path)
    config = load_config(path)
    assert (
        config.services.graph_merge.checkpoint_url.get_secret_value()
        == f"sqlite:///{root / 'workspace/checkpoint.sqlite'}"
    )


def test_readonly_sqlite_uri_is_existing_snapshot_and_cannot_write(tmp_path):
    import sqlite3

    from sqlalchemy import create_engine, text

    path = tmp_path / "snapshot with spaces.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE records (id INTEGER)")
    config = _database(SQLiteDatabaseConfig(path=path, read_only=True))
    assert ensure_sqlite_database_ready(config) == path
    engine = create_engine(config.url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM records")).scalar() == 0
            with pytest.raises(Exception, match="readonly"):
                conn.execute(text("INSERT INTO records VALUES (1)"))
    finally:
        engine.dispose()
