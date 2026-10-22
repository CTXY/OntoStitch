import json
from pathlib import Path

from conftest import make_rule
from test_agent_controller import Validator
from test_agent_runtime import ScriptModel, call, finish, message

from cortex.application.sql_training import QASqlTrainingRecord
from cortex.capabilities.sql_training.engine import SqlTrainingCapability
from cortex.capabilities.sql_training.runtime_config import (
    RuntimeDatabaseConfig,
    RuntimeValidationConfig,
    SqlTrainingRuntimeConfig,
)
from cortex.config import CortexConfig
from cortex.config.models import SQLiteDatabaseConfig
from cortex.schema import GraphSpace, load_root_schema


def test_default_capability_uses_unified_agent_and_exports_valid_incoming(case_graph):
    _store, database, workspace = case_graph

    def create(messages):
        system = messages[0].content
        context = json.loads(
            system.split("<case_context>\n", 1)[1].split("\n</case_context>", 1)[0]
        )
        return message(
            call(
                "edit_graph",
                {
                    "edit": {
                        "edit_id": "direct",
                        "expected_revision": context["working_revision"],
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
        )

    model = ScriptModel([create, finish()])
    config = CortexConfig(
        database=SQLiteDatabaseConfig(path=Path(database.engine.url.database))
    )
    runtime = SqlTrainingRuntimeConfig(
        workspace=str(workspace.root),
        database=RuntimeDatabaseConfig(url=database.url, dialect="sqlite"),
        validation=RuntimeValidationConfig(dialect="sqlite"),
    )
    validator = Validator()
    capability = SqlTrainingCapability(
        llm=model,
        config=config,
        runtime_config=runtime,
        validation_client=validator,
        database=database,
    )
    state, report = capability.train(
        (
            QASqlTrainingRecord(
                source_id="case",
                question="How many orders?",
                sql="SELECT COUNT(*) FROM orders",
            ),
        ),
        load_root_schema(),
        GraphSpace(),
    )
    assert report.success, report.model_dump()
    assert report.passed == 1
    assert validator.generate_calls == 3
    assert len(model._calls) == 2  # no legacy pre-extractor or optimizer calls
    assert {n.node_type for n in state.graph.nodes} >= {
        "table",
        "column",
        "generation_rule",
    }
    assert any(n.id == "table.unused" for n in state.graph.nodes)
    bundle = Path(report.cases[0].bundle_path)
    raw = json.loads((bundle / "case_result.json").read_text())
    assert raw["asset_outcome"] == "ready"
    assert raw["direct_completed"]
    assert raw["draft_node_ids"] == [make_rule().id]
    snapshot = json.loads(
        (bundle.parent / "snapshots" / f"{raw['final_revision']}.json").read_text()
    )
    assert "column.unused.code" in {a["id"] for a in snapshot["assets"]}
    assert (bundle.parent / "checkpoint.json").exists()


def test_resource_shutdown_failure_does_not_erase_retained_assets_or_publish_passed_bundle(
    case_graph,
):
    from contextlib import contextmanager

    from cortex.capabilities.sql_training.pipeline.train import TrainPipeline
    from cortex.capabilities.sql_training.training.trace import TrainTraceLogger

    store, database, workspace = case_graph

    def create(messages):
        context = json.loads(
            messages[0]
            .content.split("<case_context>\n", 1)[1]
            .split("\n</case_context>", 1)[0]
        )
        return message(
            call(
                "edit_graph",
                {
                    "edit": {
                        "edit_id": "direct",
                        "expected_revision": context["working_revision"],
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
        )

    model = ScriptModel([create, finish()])

    @contextmanager
    def resources():
        yield model, Validator()
        raise RuntimeError("resource cleanup failed")

    runtime = SqlTrainingRuntimeConfig(
        workspace=str(workspace.root),
        database=RuntimeDatabaseConfig(url=database.url, dialect="sqlite"),
        validation=RuntimeValidationConfig(dialect="sqlite"),
    )
    pipeline = TrainPipeline(
        runtime, model=model, database=database, case_resources_factory=resources
    )
    result = pipeline._case(
        {
            "source_id": "case",
            "question": "How many orders?",
            "sql": "SELECT COUNT(*) FROM orders",
        },
        1,
        "resource-test",
        store.working.physical_assets,
        store.working.catalog_edges,
        TrainTraceLogger(workspace.root, "resource-test"),
    )
    assert not result["correct"]
    assert result["case_status"] == "failed"
    assert result["validation_status"] == "stable_correct"
    assert result["draft_node_ids"] == [make_rule().id]
    assert result["asset_outcome"] == "partial"
    assert not Path(result["bundle_path"]).exists()


def test_prepared_catalog_missing_configured_table_fails_before_learning(
    case_graph, tmp_path
):
    from cortex.capabilities.sql_training.artifacts import Workspace
    from cortex.capabilities.sql_training.core.enums import NodeType

    _, database, workspace = case_graph
    prepared = Workspace(str(tmp_path / "prepared"))
    prepared.init()
    for kind in (NodeType.TABLE, NodeType.COLUMN, NodeType.DIMENSION_VALUE):
        prepared.write_draft_assets(
            kind,
            [
                a
                for a in workspace.read_draft_assets(kind)
                if a.id != "table.unused" and not a.id.startswith("column.unused.")
            ],
        )
    prepared.write_catalog_edges([])
    model = ScriptModel([])
    config = CortexConfig(
        database=SQLiteDatabaseConfig(path=Path(database.engine.url.database))
    )
    runtime = SqlTrainingRuntimeConfig(
        workspace=str(workspace.root),
        database=RuntimeDatabaseConfig(url=database.url, dialect="sqlite"),
        validation=RuntimeValidationConfig(dialect="sqlite"),
    )
    capability = SqlTrainingCapability(
        llm=model,
        config=config,
        runtime_config=runtime,
        validation_client=Validator(),
        database=database,
        prepared_workspace=prepared.root,
    )
    _, report = capability.train(
        (
            QASqlTrainingRecord(
                source_id="case",
                question="How many orders?",
                sql="SELECT COUNT(*) FROM orders",
            ),
        ),
        load_root_schema(),
        GraphSpace(),
    )
    assert report.fatal
    assert "prepared Table/Column catalog" in report.errors[0]
    assert len(model._calls) == 0
