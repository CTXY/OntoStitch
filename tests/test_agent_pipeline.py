import json
from pathlib import Path

from conftest import make_rule
from test_agent_runtime import (
    ScriptModel,
    assessment_reply,
    call,
    finish,
    message,
    read_case_context,
)
from test_case_workflow import Validator

from cortex.application.sql_training import QASqlTrainingRecord, SqlTrainingService
from cortex.bootstrap.graph_merge import build_graph_merge_operator
from cortex.capabilities.sql_training.engine import SqlTrainingCapability
from cortex.capabilities.sql_training.runtime_config import (
    RuntimeDatabaseConfig,
    RuntimeValidationConfig,
    SqlTrainingRuntimeConfig,
)
from cortex.config import CortexConfig
from cortex.config.models import SQLiteDatabaseConfig
from cortex.schema import GraphSpace, GraphState, load_root_schema


def test_default_application_runs_case_graph_and_merges_valid_incoming(case_graph):
    _store, database, workspace = case_graph

    def create(messages):
        context = read_case_context(messages)
        return message(
            call(
                "edit_graph",
                {
                    "edit_id": "direct",
                    "expected_revision": context["working_revision"],
                    "upserts": [make_rule().model_dump(mode="json")],
                },
            )
        )

    model = ScriptModel([create, finish(), assessment_reply])
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

    class Repository:
        def load(self):
            return GraphState(graph=GraphSpace(), node_conflicts=())

        def save(self, state):
            self.saved = state

    repository = Repository()
    service = SqlTrainingService(
        capability,
        graph_merge=build_graph_merge_operator(config, live_semantics=False),
        state_repository=repository,
    )
    trained = service.run(
        (
            QASqlTrainingRecord(
                source_id="case",
                question="How many orders?",
                sql="SELECT COUNT(*) FROM orders",
            ),
        ),
        load_root_schema(),
    )
    state, report = trained.state, trained.evaluation_report
    assert repository.saved == state
    assert report.success, report.model_dump()
    assert report.passed == 1
    assert validator.generate_calls == 3
    assert len(model._calls) == 3  # no legacy pre-extractor or optimizer calls
    assert {n.node_type for n in state.graph.nodes} >= {
        "table",
        "column",
        "generation_rule",
    }
    assert any(n.node_type == "table" and n.name == "unused" for n in state.graph.nodes)
    result_file = Path(report.cases[0].result_path)
    case_root = result_file.parent.parent
    raw = json.loads(result_file.read_text())
    assert raw["asset_outcome"] == "ready"
    assert raw["direct_completed"]
    assert raw["draft_node_ids"] == [make_rule().id]
    snapshot = json.loads(
        (case_root / "ontology" / f"{raw['final_revision']}.json").read_text()
    )
    assert "column.unused.code" in {a["id"] for a in snapshot["assets"]}
    assert (case_root / "checkpoints.sqlite").exists()


def test_resource_shutdown_failure_does_not_erase_retained_assets_or_publish_passed_bundle(
    case_graph,
):
    from contextlib import contextmanager

    from cortex.capabilities.sql_training.pipeline.train import TrainPipeline
    from cortex.capabilities.sql_training.training.trace import TrainTraceLogger

    store, database, workspace = case_graph

    def create(messages):
        context = read_case_context(messages)
        return message(
            call(
                "edit_graph",
                {
                    "edit_id": "direct",
                    "expected_revision": context["working_revision"],
                    "upserts": [make_rule().model_dump(mode="json")],
                },
            )
        )

    model = ScriptModel([create, finish(), assessment_reply])

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
    assert result["result_path"] is None
    assert result["ontology_path"] is None


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
