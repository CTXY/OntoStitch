"""File text normalization is stage 2, independent of database enrichment."""

import csv
import json
import sqlite3

import pytest
from click.testing import CliRunner
from langchain_core.messages import AIMessage

from cortex.bootstrap.metadata import build_metadata_service
from cortex.capabilities.metadata.models import BuildOptions
from cortex.capabilities.metadata.run import AgentExecutionError, AgentResult
from cortex.cli.metadata import metadata_cli
from cortex.infrastructure.metadata.sqlite_source import SQLiteMetadataSource


def inputs(tmp_path, count=2, blank=False):
    database = tmp_path / "data.sqlite"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE items (" + ", ".join(f"c{i} TEXT" for i in range(count)) + ")"
        )
    directory = tmp_path / "database_description"
    directory.mkdir()
    with (directory / "items.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "original_column_name",
                "column_description",
                "value_description",
                "data_format",
            ]
        )
        for i in range(count):
            writer.writerow(
                [
                    f"c{i}",
                    "" if blank else "A means active. Record lifecycle state.",
                    "" if blank else "The column describes lifecycle status.",
                    "text",
                ]
            )
    return database, directory


class FileModel:
    def __init__(self, mode="valid"):
        self.calls = []
        self.mode = mode

    def bind_tools(self, tools):
        assert [tool.name for tool in tools] == ["submit_file_metadata"]
        return self

    def invoke(self, messages):
        batch = json.loads(messages[1].content)["columns"]
        self.calls.append(batch)
        assert "faithful extraction" in messages[0].content
        for item in batch:
            assert "profile" not in item and item["file_records"]
            assert item["file_records"][0]["column_description"].startswith("A means")
        entries = [
            {
                "object_id": item["object_id"],
                "description": "Record lifecycle state.",
                "value_description": "A means active.",
            }
            for item in batch
        ]
        if self.mode == "missing":
            entries = entries[:-1]
        elif self.mode == "duplicate":
            entries.append(entries[0])
        elif self.mode == "unknown":
            entries[0]["object_id"] = "column.items.nonexistent"
        elif self.mode == "ambiguous":
            entries[0]["unresolved"] = "Conflicting source descriptions"
        elif self.mode == "extra_field":
            entries[0]["representation"] = "Should be rejected"
        elif self.mode == "failure":
            raise RuntimeError("provider unavailable")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "normalize",
                    "name": "submit_file_metadata",
                    "args": {"entries": entries},
                }
            ],
        )


def test_bird_file_agent_batches_without_database_enrichment(tmp_path):
    database, directory = inputs(tmp_path, 23)
    model = FileModel()
    service = build_metadata_service(
        tmp_path / "ws", loader="bird", file_mode="loader", model=model
    )

    class NoQueries(SQLiteMetadataSource):
        def query(self, sql):
            raise AssertionError("File normalization must not probe the database")

    service.source_factory = NoQueries
    snapshot = service.build(database, initial_files=[directory])
    assert [len(batch) for batch in model.calls] == [20, 3]
    assert [stage for stage, _ in service.runs] == ["files"]
    assert service.agent is None and service.file_agent is None
    assert all(
        c.description == "Record lifecycle state." for c in snapshot.tables[0].columns
    )
    assert all(
        c.value_description == "A means active." for c in snapshot.tables[0].columns
    )
    assert all(c.source_refs for c in snapshot.tables[0].columns)
    assert all(s.kind != "database_probe" for s in snapshot.sources)


def test_three_stage_order_and_enrichment_reads_normalized_files(tmp_path):
    database, directory = inputs(tmp_path)
    events = []
    model = FileModel()
    original = model.invoke

    def invoke(messages):
        events.append("files")
        return original(messages)

    model.invoke = invoke
    service = build_metadata_service(
        tmp_path / "ws", loader="bird", file_mode="loader", model=model
    )

    class RecordingSource(SQLiteMetadataSource):
        def scan(self):
            events.append("profiling")
            return super().scan()

    class Enrichment:
        def run(self, snapshot, source, *, options):
            events.append("enrichment")
            assert snapshot.tables[0].columns[0].value_description == "A means active."
            assert source.query("SELECT COUNT(*) n FROM items")["rows"] == [{"n": 0}]
            return AgentResult(snapshot)

    service.source_factory = RecordingSource
    service.agent = Enrichment()
    service.build(database, initial_files=[directory])
    assert events == ["profiling", "files", "enrichment"]
    assert [stage for stage, _ in service.runs] == ["files", "enrich"]


@pytest.mark.parametrize("mode", ["missing", "duplicate", "unknown", "extra_field"])
def test_invalid_batch_is_retried_then_retains_raw_import(tmp_path, mode):
    database, directory = inputs(tmp_path)
    model = FileModel(mode)
    service = build_metadata_service(
        tmp_path / "ws", loader="bird", file_mode="loader", model=model
    )
    snapshot = service.build(database, initial_files=[directory])
    assert len(model.calls) == 2
    assert service.runs[0][1].status == "incomplete"
    assert all(c.description is None for c in snapshot.tables[0].columns)
    assert any(
        ("Extra inputs" if mode == "extra_field" else "exactly once") in issue
        for issue in snapshot.issues
    )


def test_ambiguous_record_preserved_and_other_records_normalized(tmp_path):
    database, directory = inputs(tmp_path)
    service = build_metadata_service(
        tmp_path / "ws",
        loader="bird",
        file_mode="loader",
        model=FileModel("ambiguous"),
    )
    snapshot = service.build(database, initial_files=[directory])
    assert snapshot.tables[0].columns[0].description is None
    assert snapshot.tables[0].columns[1].description == "Record lifecycle state."
    assert service.runs[0][1].status == "incomplete"


def test_budget_retains_unprocessed_records(tmp_path):
    database, directory = inputs(tmp_path, 21)
    service = build_metadata_service(
        tmp_path / "ws",
        loader="bird",
        file_mode="loader",
        model=FileModel(),
        options=BuildOptions(max_agent_calls=1),
    )
    snapshot = service.build(database, initial_files=[directory])
    assert snapshot.tables[0].columns[19].description == "Record lifecycle state."
    assert snapshot.tables[0].columns[20].description is None
    assert service.runs[0][1].status == "incomplete"


def test_blank_file_records_do_not_invoke_model(tmp_path):
    database, directory = inputs(tmp_path, blank=True)
    model = FileModel()
    service = build_metadata_service(
        tmp_path / "ws", loader="bird", file_mode="loader", model=model
    )
    service.build(database, initial_files=[directory])
    assert model.calls == [] and service.runs == []


def test_failed_normalizer_does_not_publish(tmp_path):
    database, directory = inputs(tmp_path)
    service = build_metadata_service(
        tmp_path / "ws",
        loader="bird",
        file_mode="loader",
        model=FileModel("failure"),
    )
    with pytest.raises(AgentExecutionError):
        service.build(database, initial_files=[directory])
    assert service.runs[0][0] == "files"
    assert service.runs[0][1].status == "failed"
    assert not (tmp_path / "ws" / "metadata.sqlite").exists()


def test_cli_file_normalization_without_stage_three(tmp_path, monkeypatch):
    import cortex.infrastructure.llm

    database, directory = inputs(tmp_path)
    model = FileModel()
    monkeypatch.setattr(
        cortex.infrastructure.llm, "create_chat_model", lambda config: model
    )
    result = CliRunner().invoke(
        metadata_cli,
        [
            "build",
            "--database",
            str(database),
            "--workspace",
            str(tmp_path / "ws"),
            "--initial-file",
            str(directory),
            "--loader",
            "bird",
            "--file-mode",
            "loader",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "ws" / "file-agent-trace-r1.json").exists()
    assert not (tmp_path / "ws" / "agent-trace-r1.json").exists()


def test_bird_requires_model_for_nonempty_descriptions_and_only_two_settings(tmp_path):
    database, directory = inputs(tmp_path)
    service = build_metadata_service(tmp_path / "ws", loader="bird")
    with pytest.raises(ValueError, match="requires a model"):
        service.build(database, initial_files=[directory])
    with pytest.raises(ValueError, match="loader or agent"):
        build_metadata_service(
            tmp_path / "other",
            loader="bird",
            file_mode="loader-agent",
            model=FileModel(),
        )


def test_bird_sends_only_nonempty_description_fields(tmp_path):
    database, directory = inputs(tmp_path)
    (directory / "items.csv").write_text(
        "original_column_name,column_description,value_description,column_name,data_format\n"
        "c0,,A means active. Lifecycle state.,Ignored label,Ignored format\n"
        "c1,   ,   ,Ignored label,Ignored format\n"
    )

    class CheckFields:
        def bind_tools(self, tools):
            assert [t.name for t in tools] == ["submit_file_metadata"]
            return self

        def invoke(self, messages):
            records = json.loads(messages[1].content)["columns"]
            assert len(records) == 1
            text = records[0]["file_records"][0]
            assert set(text) == {"source_ref", "value_description"}
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "convert",
                        "name": "submit_file_metadata",
                        "args": {
                            "entries": [
                                {
                                    "object_id": records[0]["object_id"],
                                    "description": "Lifecycle state.",
                                    "value_description": "A means active.",
                                }
                            ]
                        },
                    }
                ],
            )

    snapshot = build_metadata_service(
        tmp_path / "ws", loader="bird", model=CheckFields()
    ).build(database, initial_files=[directory])
    assert snapshot.tables[0].columns[0].description == "Lifecycle state."
    assert snapshot.tables[0].columns[0].representation is None
    assert snapshot.tables[0].columns[1].description is None


@pytest.mark.parametrize(
    "output", ["Low Grade", None, "Lowest grade offered by the school"]
)
def test_label_only_source_cannot_be_expanded(tmp_path, output):
    database = tmp_path / "labels.sqlite"
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE items ("Low Grade" TEXT)')
    directory = tmp_path / "description"
    directory.mkdir()
    (directory / "items.csv").write_text(
        "original_column_name,column_description,value_description\nLow Grade,Low Grade,\n"
    )

    class LabelModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            assert "faithful extraction" in messages[0].content
            column = json.loads(messages[1].content)["columns"][0]
            assert "data_type" not in column
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "extract",
                        "name": "submit_file_metadata",
                        "args": {
                            "entries": [
                                {
                                    "object_id": column["object_id"],
                                    "description": output,
                                    "value_description": None,
                                }
                            ]
                        },
                    }
                ],
            )

    service = build_metadata_service(tmp_path / "ws", loader="bird", model=LabelModel())
    snapshot = service.build(database, initial_files=[directory])
    column = snapshot.tables[0].columns[0]
    if output in {"Low Grade", None}:
        assert column.description == output
        assert service.runs[0][1].status == "completed"
    else:
        assert column.description is None
        assert any("SOURCE_LABEL_EXPANDED" in issue for issue in snapshot.issues)
        assert service.runs[0][1].status == "incomplete"


def test_bird_whitespace_matching_is_unique_and_exact_first(tmp_path):
    database = tmp_path / "names.sqlite"
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE items ("School Code" TEXT, "x" TEXT, " x " TEXT)')
    directory = tmp_path / "descriptions"
    directory.mkdir()
    (directory / "items.csv").write_text(
        "original_column_name,column_description,value_description\n"
        "School Code ,School identifier,\n"
        "x,Exact x,\n"
        "  x  ,Ambiguous x,\n"
    )
    seen = []

    class Names:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            columns = json.loads(messages[1].content)["columns"]
            seen.extend(c["name"] for c in columns)
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "names",
                        "name": "submit_file_metadata",
                        "args": {
                            "entries": [
                                {
                                    "object_id": c["object_id"],
                                    "description": c["file_records"][0][
                                        "column_description"
                                    ],
                                    "value_description": None,
                                }
                                for c in columns
                            ]
                        },
                    }
                ],
            )

    snapshot = build_metadata_service(
        tmp_path / "ws", loader="bird", model=Names()
    ).build(database, initial_files=[directory])
    assert seen == ["School Code", "x"]
    assert len(snapshot.issues) == 1 and "  x  " in snapshot.issues[0]
