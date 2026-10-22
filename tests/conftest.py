import pytest
from sqlalchemy import create_engine, text

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.builders import build_catalog_assets
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.snapshots import GraphStore
from cortex.infrastructure.database.sqlalchemy import SQLAlchemyDatabase
from cortex.schema import load_root_schema


@pytest.fixture
def case_graph(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL)"
            )
        )
        connection.execute(
            text("INSERT INTO orders VALUES (1, 'North', 10), (2, 'South', 20)")
        )
        connection.execute(text("CREATE TABLE unused (code TEXT)"))
    database = SQLAlchemyDatabase(str(engine.url), engine=engine)
    catalog = database.introspect_catalog()
    assets = build_catalog_assets(catalog.tables, catalog.columns, catalog.joins)
    workspace = Workspace(str(tmp_path / "workspace"))
    workspace.init()
    workspace.write_draft_assets(NodeType.TABLE, assets.tables)
    workspace.write_draft_assets(NodeType.COLUMN, assets.columns)
    workspace.write_catalog_edges(assets.join_edges)
    store = GraphStore(
        tmp_path / "case" / "snapshots", workspace=workspace, schema=load_root_schema()
    )
    store.initialize([*assets.tables, *assets.columns], list(assets.join_edges))
    yield store, database, workspace
    engine.dispose()


def make_rule(name="count", action="Count the requested records.", references=()):
    from cortex.capabilities.sql_training.models import NodeAsset

    return NodeAsset.model_validate(
        {
            "id": f"generation_rule.{name}",
            "node_type": "generation_rule",
            "name": name,
            "properties": {
                "triggers": {"question": {"phrases": ["how many"]}},
                "condition": "The request asks for a count.",
                "action": action,
                "reason": "Counting returns the number of records.",
                "references": list(references),
            },
        }
    )
