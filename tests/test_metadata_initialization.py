import json
import sqlite3

import pytest
from click.testing import CliRunner
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from cortex.bootstrap.metadata import build_metadata_service
from cortex.capabilities.metadata.management import (
    MetadataChange,
    RelationProposal,
    apply_changes,
)
from cortex.capabilities.metadata.models import BuildOptions, MetadataSnapshot
from cortex.cli.metadata import metadata_cli
from cortex.infrastructure.metadata.sqlite_source import SQLiteMetadataSource


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "shop.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE orders (tenant TEXT, id INTEGER, PRIMARY KEY (tenant,id));
            CREATE TABLE items (tenant TEXT, order_id INTEGER, line_no INTEGER,
              amount INTEGER DEFAULT 0, state TEXT, UNIQUE(tenant,order_id,line_no),
              FOREIGN KEY (tenant,order_id) REFERENCES orders);
            INSERT INTO orders VALUES ('a',1);
            INSERT INTO items VALUES ('a',1,1,100,'P'),('a',1,2,200,NULL),('a',1,3,300,'P');
            CREATE VIEW paid_items AS SELECT * FROM items WHERE state='P';
            CREATE TABLE empty (id INTEGER);
        """)
    return path


def test_bootstrap_ddl_profiles_and_unknown_semantics(database, tmp_path):
    service = build_metadata_service(tmp_path / "workspace")
    snapshot = service.build(database)
    tables = {t.name: t for t in snapshot.tables}
    items = tables["items"]
    assert snapshot.revision == 1
    assert tables["orders"].primary_keys == ["tenant", "id"]
    assert items.foreign_keys[0].referenced_columns == tables["orders"].primary_keys
    assert items.unique_keys == [["tenant", "order_id", "line_no"]]
    assert items.grain is None and items.description is None
    amount = next(c for c in items.columns if c.name == "amount")
    assert amount.default_expression == "0"
    assert amount.representation is None
    assert amount.profile.min == 100 and amount.profile.max == 300
    assert items.profile.row_count == 3
    assert tables["paid_items"].object_type == "view"
    assert tables["paid_items"].profile.row_count == 2
    assert tables["empty"].columns[0].profile.null_fraction is None
    assert service.load_snapshot().model_dump() == snapshot.model_dump()


def test_sample_is_not_full_population(database, tmp_path):
    snapshot = build_metadata_service(tmp_path / "ws").build(
        database, options=BuildOptions(profile_rows=2)
    )
    table = next(t for t in snapshot.tables if t.name == "items")
    assert table.profile.scan_mode == "sample"
    assert table.profile.row_count is None
    assert table.profile.rows_examined == 2
    assert all(c.profile.scan_mode == "sample" for c in table.columns)


def test_optional_files_import_and_sources(database, tmp_path):
    csv = tmp_path / "items.csv"
    csv.write_text(
        "original_column_name,column_description,data_format\namount,订单金额,分\n",
        encoding="utf-8",
    )
    notes = tmp_path / "notes.md"
    notes.write_text("Amounts may use cents; verify currency.")
    snapshot = build_metadata_service(
        tmp_path / "ws", loader="bird", model=PassthroughFileModel()
    ).build(database, initial_files=[csv, notes])
    col = snapshot.objects()["column.items.amount"]
    assert col.description == "订单金额"
    assert col.source_refs and col.source_refs[0].startswith("file:")
    assert (
        col.representation is None
    )  # data_format is not blindly interpreted as currency
    assert len(snapshot.sources) == 3
    assert snapshot.issues  # unparsed notes remain available, not silently discarded


def test_normalized_import_and_conflicting_source_preserved(database, tmp_path):
    files = []
    for number, unit in enumerate(["CNY_cent", "CNY"]):
        path = tmp_path / f"{number}.json"
        path.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "table": "items",
                            "column": "amount",
                            "metadata": {"representation": unit},
                        }
                    ]
                }
            )
        )
        files.append(path)
    snapshot = build_metadata_service(tmp_path / "ws").build(
        database, initial_files=files
    )
    assert snapshot.objects()["column.items.amount"].representation == "CNY_cent"
    assert any("STALE_VALUE" in issue for issue in snapshot.issues)
    assert len(snapshot.sources) == 3


def test_updates_are_atomic_versioned_and_semantics_only(database, tmp_path):
    service = build_metadata_service(tmp_path / "ws")
    initial = service.build(database)
    change = MetadataChange(
        object_id="column.items.amount",
        field="description",
        value="明细金额",
        reason="Candidate interpretation",
    )
    with pytest.raises(ValueError, match="READ_ONLY_PROPERTY"):
        apply_changes(
            initial,
            [
                change,
                MetadataChange(
                    object_id="column.items.amount",
                    field="grain",
                    value="not a column field",
                    reason="wrong",
                ),
            ],
        )
    assert initial.objects()["column.items.amount"].description is None
    updated = service.update([change], expected_revision=1)
    assert updated.revision == 2
    assert service.load_snapshot(1).objects()["column.items.amount"].description is None
    assert (
        service.load_snapshot(2).objects()["column.items.amount"].description
        == "明细金额"
    )
    with pytest.raises(ValueError, match="STALE_REVISION"):
        service.update([change], expected_revision=1)
    with pytest.raises(ValueError, match="STALE_VALUE"):
        service.update([change], expected_revision=2)


def test_schema_and_local_references(database, tmp_path):
    snapshot = build_metadata_service(tmp_path / "ws").build(database)
    with pytest.raises(ValueError, match="value_grain"):
        apply_changes(
            snapshot,
            [
                MetadataChange(
                    object_id="column.items.amount",
                    field="value_grain",
                    value={"description": "order", "key_columns": ["column.orders.id"]},
                    reason="bad foreign ref",
                )
            ],
        )
    with pytest.raises(ValidationError):
        MetadataSnapshot.model_validate(
            {**snapshot.model_dump(), "notes": "not allowed"}
        )
    with pytest.raises(ValueError, match="UNKNOWN_SOURCE"):
        apply_changes(
            snapshot,
            [
                MetadataChange(
                    object_id="column.items.amount",
                    field="description",
                    value="x",
                    reason="x",
                    source_refs=["invented"],
                )
            ],
        )


def test_probe_readonly_and_bounded(database):
    source = SQLiteMetadataSource(database, BuildOptions(query_timeout=0.05))
    try:
        assert source.query("SELECT count(*) n FROM items")["rows"] == [{"n": 3}]
        for sql in [
            "DELETE FROM items",
            "PRAGMA user_version=4",
            "ATTACH DATABASE ':memory:' AS other",
        ]:
            with pytest.raises(sqlite3.DatabaseError):
                source.query(sql)
        result = source.query(
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<110) SELECT x FROM n"
        )
        assert result["truncated"] and len(result["rows"]) == 100
        with pytest.raises(sqlite3.DatabaseError, match="interrupted"):
            source.query(
                "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n"
            )
        assert source.query("SELECT count(*) n FROM items")["rows"][0]["n"] == 3
    finally:
        source.close()


class PassthroughFileModel:
    """Fixture preserves already separated source text without real model calls."""

    def bind_tools(self, tools):
        assert [t.name for t in tools] == ["submit_file_metadata"]
        return self

    def invoke(self, messages):
        columns = json.loads(messages[1].content)["columns"]
        entries = []
        for column in columns:
            record = column["file_records"][0]
            entries.append(
                {
                    "object_id": column["object_id"],
                    "description": record.get("column_description"),
                    "value_description": record.get("value_description"),
                }
            )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "convert",
                    "name": "submit_file_metadata",
                    "args": {"entries": entries},
                }
            ],
        )


class ScriptedModel:
    def __init__(self):
        self.step = 0
        self.tools = {}

    def bind_tools(self, tools):
        self.tools = {t.name: t for t in tools}
        self.step = 0
        self.total_calls = getattr(self, "total_calls", 0)
        return self

    def invoke(self, messages):
        self.step += 1
        self.total_calls += 1
        if self.step == 1:
            page = self.tools["list_metadata"].invoke({"kind": "column", "limit": 50})
            addresses = [item["id"] for item in page["items"]]
            for offset in range(0, len(addresses), 20):
                self.tools["inspect_metadata_batch"].invoke(
                    {"object_ids": addresses[offset : offset + 20]}
                )
        if self.step == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "1",
                        "name": "inspect_database",
                        "args": {"sql": "SELECT count(*) n FROM items"},
                    }
                ],
            )
        if self.step == 2:
            evidence_id = json.loads(messages[-1].content)["source_ref"]
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "2",
                        "name": "propose_metadata",
                        "args": {
                            "changes": [
                                {
                                    "object_id": "table.items",
                                    "field": "description",
                                    "value": "Order item records; semantics need confirmation",
                                    "source_refs": [evidence_id],
                                    "reason": "Candidate supported only partially by probe",
                                }
                            ]
                        },
                    }
                ],
            )
        return AIMessage(content="Done; unit remains unknown.")


def test_optional_agent_actually_calls_tools_and_leaves_candidates(database, tmp_path):
    model = ScriptedModel()
    service = build_metadata_service(tmp_path / "ws", model=model, enrich_database=True)
    snapshot = service.build(database)
    assert snapshot.objects()["table.items"].description.startswith("Order item")
    assert snapshot.objects()["table.items"].source_refs[-1].startswith("probe:")
    assert snapshot.sources[-1].kind == "database_probe"
    assert model.total_calls == 3
    assert len([e for e in service.runs[0][1].trace if "coverage" not in e]) == 5
    assert service.runs[0][1].status == "completed"


def test_cli_roundtrip_without_llm(database, tmp_path):
    runner = CliRunner()
    workspace = tmp_path / "ws"
    result = runner.invoke(
        metadata_cli,
        ["build", "--database", str(database), "--workspace", str(workspace)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["revision"] == 1
    result = runner.invoke(
        metadata_cli,
        ["show", "--workspace", str(workspace), "--object-id", "table.items"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["name"] == "items"
    output = tmp_path / "m0.json"
    result = runner.invoke(
        metadata_cli, ["export", "--workspace", str(workspace), "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert MetadataSnapshot.model_validate_json(output.read_text()).revision == 1
    result = runner.invoke(
        metadata_cli,
        ["build", "--database", str(database), "--workspace", str(workspace)],
    )
    assert result.exit_code != 0 and "STALE_REVISION" in result.output


def test_relation_proposals_and_dictionary_reference(database, tmp_path):
    service = build_metadata_service(tmp_path / "ws")
    service.build(database)
    proposal = RelationProposal(
        type="join",
        source_columns=["items.order_id"],
        target_columns=["orders.id"],
        expression="s.order_id=t.id AND s.tenant=t.tenant",
        description="Candidate order link",
        source_refs=["schema"],
        reason="Check complete tenant scope",
    )
    updated = service.propose_relation(proposal, expected_revision=1)
    assert updated.relations[0].source_refs == ["schema"]
    with pytest.raises(ValueError, match="unknown relation column"):
        service.propose_relation(
            proposal.model_copy(update={"target_columns": ["missing"]}),
            expected_revision=2,
        )
    assert service.load_snapshot().revision == 2


def test_agent_budget_marks_incomplete(database, tmp_path):
    service = build_metadata_service(
        tmp_path / "ws",
        model=ScriptedModel(),
        enrich_database=True,
        options=BuildOptions(max_agent_calls=1),
    )
    snapshot = service.build(database)
    assert any("budget exhausted" in issue for issue in snapshot.issues)
    assert snapshot.objects()["table.items"].description is None


def test_punctuation_identifiers_and_quoted_reads(tmp_path):
    path = tmp_path / "quoted.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            'CREATE TABLE "a.b" ("x y" TEXT); CREATE TABLE "a_b" ("x_y" TEXT); INSERT INTO "a.b" VALUES (\'hello\');'
        )
    snapshot = build_metadata_service(tmp_path / "ws").build(path)
    assert len(snapshot.objects()) == 5
    assert snapshot.objects()["column.a%2Eb.x%20y"].profile.examples == ["hello"]


def test_object_reader_does_not_serialize_snapshot_or_table(
    database, tmp_path, monkeypatch
):
    from cortex.capabilities.metadata.models import Table
    from cortex.capabilities.metadata.views import MetadataReader

    snapshot = build_metadata_service(tmp_path / "ws").build(database)
    reader = MetadataReader(snapshot)

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Whole snapshot/table serialization is not a read strategy"
        )

    monkeypatch.setattr(MetadataSnapshot, "model_dump", forbidden)
    monkeypatch.setattr(Table, "model_dump", forbidden)
    table = reader.inspect("table.items", limit=2)
    assert table.columns.total == 5
    assert table.columns.next_offset == 2
    assert len(table.columns.items) == 2
    assert reader.inspect("column.items.amount").profile.max == 300
    assert (
        reader.list(kind="column", table_id="table.items", offset=4).next_offset is None
    )
    with pytest.raises(ValueError, match="UNKNOWN_OBJECT"):
        reader.inspect("missing")


def test_typed_changes_and_schema():
    from pydantic import TypeAdapter

    from cortex.capabilities.metadata.management import MetadataChange
    from cortex.capabilities.metadata.views import MetadataReader

    schema = MetadataReader.schema("column")
    assert schema["json_schema"]["properties"]["representation"]["description"]
    assert "representation" in schema["writable_fields"]
    adapter = TypeAdapter(MetadataChange)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "object_id": "column.items.amount",
                "field": "representation",
                "value": {"unit": "cents"},
            }
        )
    change = adapter.validate_python(
        {
            "object_id": "column.items.amount",
            "field": "representation",
            "value": "Integer cents (CNY)",
        }
    )
    assert change.value == "Integer cents (CNY)"


def test_source_projection_and_legacy_migration(database, tmp_path):
    service = build_metadata_service(tmp_path / "ws")
    snapshot = service.build(database)
    exported = service.export()
    assert all("content" not in source for source in exported["sources"])
    assert service.read_source(snapshot.sources[0].id).text
    old = legacy_snapshot(snapshot)
    old["schema_version"] = "1.0"
    old["evidence"] = old.pop("sources")
    column = old["tables"][0]["columns"][0]
    column["description"] = "Imported meaning"
    column.pop("source_refs")
    column["annotations"] = [
        {
            "field": "description",
            "status": "imported",
            "evidence_ids": [snapshot.sources[0].id],
            "reason": "legacy",
        }
    ]
    migrated = MetadataSnapshot.model_validate(old)
    assert migrated.schema_version == "3.3"
    assert migrated.tables[0].columns[0].source_refs == [snapshot.sources[0].id]
    assert "annotations" not in migrated.model_dump_json()


def test_agent_prompt_and_bound_write_schema(database, tmp_path):
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from cortex.capabilities.metadata.agent import MetadataAgent, load_prompt

    class InspectingModel:
        def bind_tools(self, tools):
            self.tools = {tool.name: tool for tool in tools}
            schema = convert_to_openai_tool(self.tools["propose_metadata"])
            assert "expected_value" in json.dumps(schema)
            assert "ValueDomain" not in json.dumps(schema)
            assert "representation" in json.dumps(schema)
            return self

        def invoke(self, messages):
            assert "initial_files" in messages[1].content
            column = self.tools["inspect_metadata"].invoke(
                {"object_id": "column.items.amount"}
            )
            assert column["profile"]["max"] == 300
            assert "sources" not in column
            return AIMessage(content="Completed inspection; units remain unknown.")

    prompt = load_prompt()
    assert "# Task" in prompt and "# Procedure" in prompt
    assert "NL2SQL" not in prompt and "M_0" not in prompt
    service = build_metadata_service(tmp_path / "ws")
    service.agent = MetadataAgent(InspectingModel())
    service.build(database)


def test_scan_sqlite_identifiers_and_preserve_index_ddl(tmp_path):
    path = tmp_path / "structure.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE Parent(ID INTEGER PRIMARY KEY, email TEXT);
            CREATE TABLE Child(parent_id INTEGER REFERENCES parent(id));
            CREATE TABLE sqlitex_events(id INTEGER);
            CREATE UNIQUE INDEX expression_idx ON Parent(lower(email));
            CREATE UNIQUE INDEX partial_idx ON Parent(email) WHERE email IS NOT NULL;
            INSERT INTO Parent VALUES (1,'x');
            INSERT INTO Child VALUES (1);
        """)
    snapshot = build_metadata_service(tmp_path / "ws").build(path)
    tables = {table.name: table for table in snapshot.tables}
    assert "sqlitex_events" in tables
    fk = tables["Child"].foreign_keys[0]
    assert fk.referenced_table == "Parent"
    assert fk.referenced_columns == ["ID"]
    assert not any("Unresolved" in issue for issue in snapshot.issues)
    indexes = snapshot.sources[0].content["indexes"]
    assert {index["name"] for index in indexes} == {"expression_idx", "partial_idx"}
    assert any("lower(email)" in index["sql"] for index in indexes)
    assert tables["Parent"].unique_keys == []


def test_probe_rejects_duplicate_labels_even_without_rows(database):
    source = SQLiteMetadataSource(database, BuildOptions())
    try:
        for sql in [
            "SELECT order_id AS x, amount AS x FROM items",
            "SELECT order_id AS x, amount AS x FROM items WHERE 0",
        ]:
            with pytest.raises(ValueError, match="DUPLICATE_RESULT_COLUMNS"):
                source.query(sql)
        row = source.query(
            "SELECT order_id AS order_key, amount AS line_amount FROM items LIMIT 1"
        )["rows"][0]
        assert row == {"order_key": 1, "line_amount": 100}
    finally:
        source.close()


def test_bird_loader_requires_column_identity_and_preserves_valid_rows(
    database, tmp_path
):
    directory = tmp_path / "database_description"
    directory.mkdir()
    path = directory / "items.csv"
    path.write_text(
        "original_column_name,column_name,column_description,data_format,value_description\n"
        ",amount,Wrong table description,integer,\n"
        "amount,Amount,Line amount,integer,Amount before adjustments\n"
        "unknown,Unknown,Not in database,text,\n"
    )
    snapshot = build_metadata_service(
        tmp_path / "ws", loader="bird", model=PassthroughFileModel()
    ).build(database, initial_files=[directory])
    table = snapshot.objects()["table.items"]
    column = snapshot.objects()["column.items.amount"]
    assert table.description is None
    assert (
        column.description == "Line amount"
        and column.value_description == "Amount before adjustments"
    )
    assert column.representation is None
    assert column.source_refs and column.source_refs[0].startswith("file:")
    assert sum("unknown or missing" in issue for issue in snapshot.issues) == 2


def test_bird_missing_header_never_becomes_table_description(database, tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("column_description\nAmount in cents\n")
    snapshot = build_metadata_service(
        tmp_path / "ws", loader="bird", model=PassthroughFileModel()
    ).build(database, initial_files=[path])
    assert snapshot.objects()["table.items"].description is None
    assert any("Missing BIRD CSV headers" in issue for issue in snapshot.issues)


def test_normalized_loader_handles_invalid_records_individually(database, tmp_path):
    path = tmp_path / "notes.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {"table": "items", "metadata": None},
                    {
                        "table": "items",
                        "column": None,
                        "metadata": {"description": "wrong"},
                    },
                    {
                        "table": "items",
                        "column": "amount",
                        "metadata": {"description": "valid"},
                    },
                ]
            }
        )
    )
    snapshot = build_metadata_service(tmp_path / "ws", loader="normalized").build(
        database, initial_files=[path]
    )
    assert snapshot.objects()["table.items"].description is None
    assert snapshot.objects()["column.items.amount"].description == "valid"
    assert len(snapshot.issues) == 2


def test_custom_loader_base_contract(database, tmp_path):
    from cortex.capabilities.metadata.loading import LoaderResult, MetadataLoader

    class CustomLoader(MetadataLoader):
        def extract(self, snapshot, source):
            return LoaderResult(
                batches=[
                    [
                        MetadataChange(
                            object_id="table.items",
                            field="description",
                            value="Custom dataset",
                            source_refs=[source.id],
                        )
                    ]
                ]
            )

    path = tmp_path / "custom.txt"
    path.write_text("Dataset-specific format")
    snapshot = build_metadata_service(tmp_path / "ws", loader=CustomLoader()).build(
        database, initial_files=[path]
    )
    assert snapshot.objects()["table.items"].description == "Custom dataset"


class UnknownFileModel:
    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        self.tool_names = {tool.name for tool in tools}
        return self

    def invoke(self, messages):
        assert "File structure is unknown" in messages[0].content
        assert "read_source" in self.tool_names
        step = self.step
        self.step += 1
        if step == 0:
            name, args = "list_sources", {}
        elif step == 1:
            sources = json.loads(messages[-1].content)["items"]
            self.ref = next(s["id"] for s in sources if s["kind"] == "initial_file")
            name, args = "read_source", {"source_ref": self.ref}
        elif step == 2:
            assert (
                "amount means line revenue" in json.loads(messages[-1].content)["text"]
            )
            name, args = "inspect_metadata", {"object_id": "column.items.amount"}
        elif step == 3:
            current = json.loads(messages[-1].content)
            name, args = (
                "propose_metadata",
                {
                    "changes": [
                        {
                            "object_id": "column.items.amount",
                            "field": "description",
                            "expected_value": current["description"],
                            "value": "Line revenue",
                            "source_refs": [self.ref],
                        }
                    ]
                },
            )
        elif step == 4:
            assert "error" not in json.loads(messages[-1].content)
            name, args = "inspect_metadata", {"object_id": "column.items.amount"}
        else:
            assert json.loads(messages[-1].content)["description"] == "Line revenue"
            return AIMessage(content="File interpretation completed.")
        return AIMessage(
            content="", tool_calls=[{"name": name, "args": args, "id": str(step)}]
        )


def test_unknown_file_agent_mode_without_dataset_loader(database, tmp_path):
    path = tmp_path / "notes.md"
    path.write_text(
        "An unfamiliar document structure: amount means line revenue in items."
    )
    service = build_metadata_service(
        tmp_path / "ws",
        model=UnknownFileModel(),
        file_mode="agent",
        enrich_database=False,
    )
    snapshot = service.build(database, initial_files=[path])
    assert service.loader is None and service.agent is None
    assert snapshot.objects()["column.items.amount"].description == "Line revenue"
    assert snapshot.objects()["column.items.amount"].source_refs[0].startswith("file:")
    assert not snapshot.issues
    assert service.runs[0][1].trace
    assert service.inspect("column.items.amount").description == "Line revenue"


def test_file_settings_reject_ambiguous_configuration(tmp_path):
    with pytest.raises(ValueError, match="requires a model"):
        build_metadata_service(tmp_path / "ws", file_mode="agent")
    with pytest.raises(ValueError, match="does not accept"):
        build_metadata_service(
            tmp_path / "ws", model=object(), file_mode="agent", loader="bird"
        )


def test_cli_bird_loader_and_unknown_file_agent(database, tmp_path, monkeypatch):
    import cortex.infrastructure.llm

    monkeypatch.setattr(
        cortex.infrastructure.llm,
        "create_chat_model",
        lambda config: PassthroughFileModel(),
    )
    bird = tmp_path / "items.csv"
    bird.write_text("original_column_name,column_description\namount,Line amount\n")
    runner = CliRunner()
    result = runner.invoke(
        metadata_cli,
        [
            "build",
            "--database",
            str(database),
            "--workspace",
            str(tmp_path / "bird"),
            "--initial-file",
            str(bird),
            "--loader",
            "bird",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (
        build_metadata_service(tmp_path / "bird")
        .inspect("column.items.amount")
        .description
        == "Line amount"
    )
    notes = tmp_path / "notes.txt"
    notes.write_text("amount means line revenue in items")
    monkeypatch.setattr(
        cortex.infrastructure.llm,
        "create_chat_model",
        lambda config: UnknownFileModel(),
    )
    result = runner.invoke(
        metadata_cli,
        [
            "build",
            "--database",
            str(database),
            "--workspace",
            str(tmp_path / "agent"),
            "--initial-file",
            str(notes),
            "--file-mode",
            "agent",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "agent" / "file-agent-trace-r1.json").exists()
    assert not (tmp_path / "agent" / "agent-trace-r1.json").exists()
    assert (
        build_metadata_service(tmp_path / "agent")
        .inspect("column.items.amount")
        .description
        == "Line revenue"
    )


def test_file_agent_precedes_optional_database_enrichment(database, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("amount means line revenue in items")
    service = build_metadata_service(
        tmp_path / "ws", model=UnknownFileModel(), file_mode="agent"
    )

    class CheckingEnricher:
        called = False

        def run(self, snapshot, source, *, options):
            assert (
                snapshot.objects()["column.items.amount"].description == "Line revenue"
            )
            self.called = True
            from cortex.capabilities.metadata.run import AgentResult

            return AgentResult(snapshot)

    checker = CheckingEnricher()
    service.agent = checker
    service.build(database, initial_files=[notes])
    assert checker.called


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_repository_never_writes_source_on_collision(database, tmp_path, alias):
    import os

    from cortex.infrastructure.metadata.repository import MetadataRepository

    original = database.read_bytes()
    target = database if alias == "same" else tmp_path / "alias.sqlite"
    if alias == "symlink":
        target.symlink_to(database)
    elif alias == "hardlink":
        os.link(database, target)
    service = build_metadata_service(tmp_path / "ws")
    service.repository = MetadataRepository(target)
    assert database.read_bytes() == original
    with pytest.raises(ValueError, match="different files"):
        service.build(database)
    assert database.read_bytes() == original


def test_repository_read_does_not_create_files(tmp_path):
    service = build_metadata_service(tmp_path / "absent")
    with pytest.raises(LookupError):
        service.load_snapshot()
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("mixed", [False, True])
def test_invalid_tool_calls_are_explicitly_incomplete(database, tmp_path, mixed):
    class InvalidModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(
                content="",
                invalid_tool_calls=[
                    {
                        "name": "propose_metadata",
                        "args": "{broken",
                        "id": "bad",
                        "error": "JSON parse failure",
                    }
                ],
                tool_calls=[
                    {
                        "name": "inspect_metadata",
                        "args": {"object_id": "table.items"},
                        "id": "good",
                    }
                ]
                if mixed
                else [],
            )

    service = build_metadata_service(
        tmp_path / "ws", model=InvalidModel(), enrich_database=True
    )
    snapshot = service.build(database)
    assert service.runs[0][1].status == "incomplete"
    assert any("invalid tool calls" in issue for issue in snapshot.issues)


def test_failed_model_keeps_trace_and_does_not_publish(database, tmp_path, monkeypatch):
    import cortex.infrastructure.llm

    class FailingModel(ScriptedModel):
        def invoke(self, messages):
            if self.step:
                raise RuntimeError("provider unavailable")
            return super().invoke(messages)

    monkeypatch.setattr(
        cortex.infrastructure.llm, "create_chat_model", lambda config: FailingModel()
    )
    workspace = tmp_path / "failed"
    result = CliRunner().invoke(
        metadata_cli,
        [
            "build",
            "--database",
            str(database),
            "--workspace",
            str(workspace),
            "--agent",
        ],
    )
    assert result.exit_code != 0
    reports = list(workspace.glob("agent-trace-failed-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["status"] == "failed" and not report["published"]
    assert report["error"] == "provider unavailable"
    assert any(item.get("tool") == "inspect_database" for item in report["trace"])
    assert not (workspace / "metadata.sqlite").exists()


def test_loader_batch_fast_path_and_record_atomicity(database, tmp_path, monkeypatch):
    import cortex.application.metadata as application
    from cortex.capabilities.metadata.loading import LoaderResult, MetadataLoader

    calls = []
    original = application.apply_changes

    def counted(snapshot, changes, **kwargs):
        calls.append(len(changes))
        return original(snapshot, changes, **kwargs)

    monkeypatch.setattr(application, "apply_changes", counted)
    path = tmp_path / "notes.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "table": "items",
                        "metadata": {
                            "description": "Items",
                            "scope": "All items",
                        },
                    },
                    {"table": "orders", "metadata": {"description": "Orders"}},
                ]
            }
        )
    )
    service = build_metadata_service(tmp_path / "valid")
    service.build(database, initial_files=[path])
    assert calls == [3]
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "table": "items",
                        "metadata": {
                            "description": "Must roll back",
                            "scope": {"unknown": "bad"},
                        },
                    },
                    {"table": "orders", "metadata": {"description": "Orders"}},
                ]
            }
        )
    )
    snapshot = build_metadata_service(tmp_path / "invalid").build(
        database, initial_files=[path]
    )
    assert snapshot.objects()["table.items"].description is None
    assert snapshot.objects()["table.orders"].description == "Orders"
    assert len(snapshot.issues) == 1

    class EmptyLoader(MetadataLoader):
        def extract(self, snapshot, source):
            return LoaderResult()

    snapshot = build_metadata_service(tmp_path / "empty", loader=EmptyLoader()).build(
        database, initial_files=[path]
    )
    assert not snapshot.issues


def test_relations_have_stable_identity(database, tmp_path):
    from cortex.capabilities.metadata.management import add_relation

    snapshot = build_metadata_service(tmp_path / "ws").build(database)
    proposal = RelationProposal(
        type="join",
        source_columns=["items.order_id"],
        target_columns=["orders.id"],
        expression="s.order_id=t.id",
        description="Order dictionary",
    )
    updated = add_relation(snapshot, proposal)
    with pytest.raises(ValueError, match="already exists"):
        add_relation(
            updated, proposal.model_copy(update={"description": "Different wording"})
        )
    relation = updated.relations[0]
    updated = apply_changes(
        updated,
        [
            MetadataChange(
                object_id=relation.id,
                field="description",
                expected_value=relation.description,
                value="Updated wording",
            )
        ],
    )
    assert updated.relations[0].id == relation.id


def test_table_read_view_is_detached(database, tmp_path):
    from cortex.capabilities.metadata.views import MetadataReader

    snapshot = build_metadata_service(tmp_path / "ws").build(database)
    reader = MetadataReader(snapshot)
    view = reader.inspect("table.items")
    view.profile.row_count = 999
    assert snapshot.objects()["table.items"].profile.row_count == 3


def test_bird_parser_shared_with_training_consumer(database, tmp_path):
    from cortex.infrastructure.online_context.column_context import (
        DatabaseDescriptionLoader,
    )

    directory = tmp_path / "descriptions"
    directory.mkdir()
    path = directory / "items.csv"
    path.write_text(
        "original_column_name,column_name,column_description,data_format,value_description\n"
        "amount,Amount,Line revenue,integer,Before adjustments\n"
    )
    context = DatabaseDescriptionLoader().load(directory)[0]
    metadata = build_metadata_service(
        tmp_path / "ws", loader="bird", model=PassthroughFileModel()
    ).build(database, initial_files=[directory])
    column = metadata.objects()["column.items.amount"]
    assert context.column_name == column.name
    assert (
        context.description in column.description
        and context.value_description == column.value_description
    )
    assert context.data_format == "integer"


def test_repository_conflict_keeps_completed_attempt_trace(
    database, tmp_path, monkeypatch
):
    import cortex.infrastructure.llm

    workspace = tmp_path / "ws"
    build_metadata_service(workspace).build(database)
    monkeypatch.setattr(
        cortex.infrastructure.llm, "create_chat_model", lambda config: ScriptedModel()
    )
    result = CliRunner().invoke(
        metadata_cli,
        [
            "build",
            "--database",
            str(database),
            "--workspace",
            str(workspace),
            "--agent",
        ],
    )
    assert result.exit_code != 0
    report = json.loads(next(workspace.glob("agent-trace-failed-*.json")).read_text())
    assert report["status"] == "completed" and report["published"] is False
    assert "STALE_REVISION" in report["error"]
    assert report["trace"]
    assert build_metadata_service(workspace).load_snapshot().revision == 1


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_abnormal_finish_is_incomplete(database, tmp_path, reason):
    class TruncatedModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(
                content="Partial", response_metadata={"finish_reason": reason}
            )

    service = build_metadata_service(
        tmp_path / "ws", model=TruncatedModel(), enrich_database=True
    )
    snapshot = service.build(database)
    assert service.runs[0][1].status == "incomplete"
    assert any("abnormally" in issue for issue in snapshot.issues)


def test_enrichment_is_explicit_and_file_context_counts_agent_imports(
    database, tmp_path
):
    from cortex.capabilities.metadata.agent import initial_context

    service = build_metadata_service(tmp_path / "ws", model=object())
    assert service.agent is None
    with pytest.raises(ValueError, match="requires a model"):
        build_metadata_service(tmp_path / "other", enrich_database=True)
    snapshot = service.build(database)
    snapshot = apply_changes(
        snapshot,
        [MetadataChange(object_id="table.items", field="description", value="Items")],
    )
    context = initial_context(snapshot, BuildOptions())
    assert '"semantic_properties": 1' in context


def test_flat_schema_and_text_writes(database, tmp_path):
    service = build_metadata_service(tmp_path / "ws")
    snapshot = service.build(database)
    updated = service.update(
        [
            MetadataChange(
                object_id="table.items", field="grain", value="One item per order line."
            ),
            MetadataChange(
                object_id="table.items",
                field="scope",
                value="Current and cancelled order items.",
            ),
            MetadataChange(
                object_id="column.items.amount",
                field="description",
                value="Order total, repeated on each line; -1 means unknown.",
            ),
            MetadataChange(
                object_id="column.items.amount",
                field="representation",
                value="Integer cents.",
                source_refs=["schema"],
            ),
        ],
        expected_revision=snapshot.revision,
    )
    column = service.inspect("column.items.amount").model_dump()
    table = service.inspect("table.items").model_dump()
    assert column["representation"] == "Integer cents."
    assert column["source_refs"] == ["schema"]
    assert table["grain"] == "One item per order line."
    assert not {"value_domain", "value_grain", "provenance"} & column.keys()
    assert not {"schema_name", "ddl", "provenance"} & table.keys()
    assert "grain_duplicate_groups" not in table["profile"]
    assert updated.schema_version == "3.3"
    schema_source = next(s for s in updated.sources if s.kind == "database_schema")
    assert "CREATE TABLE items" in schema_source.content["ddl"]["items"]


def test_object_source_refs_are_accumulated_without_property_wrappers(
    database, tmp_path
):
    snapshot = build_metadata_service(tmp_path / "ws").build(database)
    from cortex.capabilities.metadata.models import SourceRecord

    snapshot.sources.append(
        SourceRecord(
            id="notes", kind="initial_file", locator="notes.txt", content="Cents"
        )
    )
    updated = apply_changes(
        snapshot,
        [
            MetadataChange(
                object_id="column.items.amount",
                field="description",
                value="Amount",
                source_refs=["schema", "schema"],
            ),
            MetadataChange(
                object_id="column.items.amount",
                field="representation",
                value="Cents",
                source_refs=["notes", "schema"],
            ),
        ],
    )
    assert updated.objects()["column.items.amount"].source_refs == ["schema", "notes"]
    assert snapshot.objects()["column.items.amount"].source_refs == []
    with pytest.raises(ValueError, match="unknown source reference"):
        bad = updated.model_dump()
        bad["tables"][0]["source_refs"] = ["nonexistent"]
        MetadataSnapshot.model_validate(bad)


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_legacy_semantics_migrate_to_text_without_rewriting_storage(
    database, tmp_path, version
):
    service = build_metadata_service(tmp_path / "ws")
    snapshot = service.build(database)
    old = legacy_snapshot(snapshot)
    old["schema_version"] = version
    table = next(t for t in old["tables"] if t["name"] == "items")
    table.update(
        schema_name="main",
        ddl="CREATE TABLE items (legacy definition)",
        grain={"description": "One line", "key_columns": ["column.items.line_no"]},
        scope={"description": "All items", "temporal_mode": "event_log"},
    )
    table["profile"]["grain_duplicate_groups"] = None
    column = next(c for c in table["columns"] if c["name"] == "amount")
    column.update(
        description="Amount",
        representation={"format": None, "unit": "CNY_cent", "timezone": None},
        value_grain={
            "description": "Repeated order total",
            "key_columns": ["column.items.order_id"],
        },
        value_domain={
            "allowed_values": [0, "0"],
            "null_meaning": "not supplied",
            "code_meanings": [
                {"value": "P", "meaning": "paid", "condition": "state='done'"}
            ],
            "special_values": [{"value": -1, "meaning": "unknown"}],
            "dictionary_relation": None,
        },
    )
    if version == "1.0":
        old["evidence"] = old.pop("sources")
        column["annotations"] = [
            {"field": "description", "status": "imported", "evidence_ids": ["schema"]}
        ]
    else:
        column["provenance"] = {
            "description": {"origin": "initial_file", "source_refs": ["schema"]}
        }
    payload = json.dumps(old)
    path = tmp_path / "ws" / "metadata.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE revisions SET payload=? WHERE revision=1", (payload,)
        )
    migrated = service.load_snapshot()
    col = migrated.objects()["column.items.amount"]
    assert col.source_refs == ["schema"]
    assert col.representation == "unit: CNY_cent"
    for expected in [
        '[0, "0"]',
        "not supplied",
        "paid",
        "state='done'",
        "-1",
        "unknown",
        "Repeated order total",
        "column.items.order_id",
    ]:
        assert expected in col.description
    assert "column.items.line_no" in migrated.objects()["table.items"].grain
    assert "event_log" in migrated.objects()["table.items"].scope
    assert (
        next(s for s in migrated.sources if s.id == "schema").content["ddl"]["items"]
        == table["ddl"]
    )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT payload FROM revisions WHERE revision=1"
            ).fetchone()[0]
            == payload
        )
    exported = service.export()
    assert exported["schema_version"] == "3.3"
    assert MetadataSnapshot.model_validate(exported).model_dump(
        mode="json"
    ) == exported | {
        "sources": [dict(source, content=None) for source in exported["sources"]]
    }


def test_profiling_and_all_file_imports_complete_before_model(database, tmp_path):
    from cortex.infrastructure.metadata.loaders import BirdMetadataLoader

    events = []
    items = tmp_path / "items.csv"
    orders = tmp_path / "orders.csv"
    items.write_text(
        "original_column_name,column_description\namount,Amount from documentation\n"
    )
    orders.write_text(
        "original_column_name,column_description\nid,Order identifier from documentation\n"
    )

    class RecordingLoader(BirdMetadataLoader):
        def extract(self, snapshot, source):
            assert snapshot.objects()["table.items"].profile.row_count == 3
            events.append("loader:" + source.locator.split("/")[-1])
            return super().extract(snapshot, source)

    class BaselineModel(ScriptedModel):
        def invoke(self, messages):
            events.append("model")
            assert "Start by reading this existing metadata" in messages[0].content
            for column, expected in [
                ("column.items.amount", "Amount from documentation"),
                ("column.orders.id", "Order identifier from documentation"),
            ]:
                current = self.tools["inspect_metadata"].invoke({"object_id": column})
                assert current["description"] == expected
                assert current["profile"] is not None
                assert current["source_refs"]
            return super().invoke(messages)

    class RecordingSource(SQLiteMetadataSource):
        def scan(self):
            events.append("profiling")
            return super().scan()

    service = build_metadata_service(
        tmp_path / "ws",
        loader=RecordingLoader(PassthroughFileModel()),
        model=BaselineModel(),
        enrich_database=True,
    )
    service.source_factory = RecordingSource
    read_files = service.read_files

    def recording_read(paths):
        events.append("read_files")
        return read_files(paths)

    service.read_files = recording_read
    snapshot = service.build(database, initial_files=[items, orders])
    assert events[:5] == [
        "profiling",
        "read_files",
        "loader:items.csv",
        "loader:orders.csv",
        "model",
    ]
    assert (
        snapshot.objects()["column.items.amount"].description
        == "Amount from documentation"
    )
    assert any(
        entry.get("tool") == "inspect_database" for entry in service.runs[-1][1].trace
    )
    assert snapshot.objects()["table.items"].description is not None


def legacy_snapshot(snapshot):
    """Construct the historical v2 shape, including physical reference IDs."""
    from cortex.capabilities.metadata.models import object_id

    old = snapshot.model_dump(mode="json")
    old["schema_version"] = "2.0"
    for table in old["tables"]:
        name = table["name"]
        table["id"] = object_id("table", name)
        table["primary_keys"] = [
            object_id("column", name, c) for c in table["primary_keys"]
        ]
        table["unique_keys"] = [
            [object_id("column", name, c) for c in key] for key in table["unique_keys"]
        ]
        for index, fk in enumerate(table["foreign_keys"]):
            fk["id"] = object_id("fk", name, str(index))
            fk["columns"] = [object_id("column", name, c) for c in fk["columns"]]
            fk["referenced_columns"] = [
                object_id("column", fk["referenced_table"], c)
                for c in fk["referenced_columns"]
            ]
            fk["referenced_table"] = object_id("table", fk["referenced_table"])
        for column in table["columns"]:
            column["id"] = object_id("column", name, column["name"])
    return old


def test_named_metadata_and_legacy_v2_roundtrip(database, tmp_path):
    snapshot = build_metadata_service(tmp_path / "ws").build(database)
    payload = snapshot.model_dump(mode="json")
    for table in payload["tables"]:
        assert "id" not in table
        assert all("id" not in fk for fk in table["foreign_keys"])
        assert all("id" not in c for c in table["columns"])
    restored = MetadataSnapshot.model_validate(legacy_snapshot(snapshot))
    assert restored.model_dump(mode="json") == payload
    # Identical column names in different tables must stay independently editable.
    from cortex.capabilities.metadata.management import apply_changes

    updated = apply_changes(
        snapshot,
        [
            MetadataChange(
                object_id="column.items.tenant",
                field="description",
                value="Item tenant",
            )
        ],
    )
    assert updated.objects()["column.items.tenant"].description == "Item tenant"
    assert updated.objects()["column.orders.tenant"].description is None


def test_batch_read_limits_profiles_and_atomic_error_details(database, tmp_path):
    from cortex.capabilities.metadata.views import MetadataReader

    service = build_metadata_service(tmp_path / "ws")
    snapshot = service.build(database)
    addresses = ["column.items.amount", "column.orders.id"]
    batch = service.inspect_many(addresses)
    assert list(batch["items"]) == addresses
    assert all("profile" not in item for item in batch["items"].values())
    assert service.inspect_many(addresses, include_profiles=True)["items"][
        addresses[0]
    ]["profile"]
    batch["items"][addresses[0]]["description"] = "detached"
    assert service.inspect(addresses[0]).description is None
    for invalid in [[], addresses * 11, [addresses[0], addresses[0]], ["missing"]]:
        with pytest.raises(ValueError):
            service.inspect_many(invalid)
    changes = [
        MetadataChange(object_id=addresses[0], field="description", value="Revenue"),
        MetadataChange(
            object_id=addresses[1],
            field="description",
            value="ID",
            source_refs=["missing"],
        ),
    ]
    with pytest.raises(
        ValueError, match=r"change\[1\] column.orders.id.description: UNKNOWN_SOURCE"
    ):
        apply_changes(snapshot, changes)
    assert snapshot.objects()[addresses[0]].description is None
    snapshot.objects()[addresses[0]].description = "x" * 64001
    with pytest.raises(ValueError, match="BATCH_TOO_LARGE"):
        MetadataReader(snapshot).inspect_many([addresses[0]])


def test_profiles_only_publish_numeric_bounds(tmp_path):
    path = tmp_path / "types.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE values_test(code TEXT, amount REAL, mixed, empty TEXT);
            INSERT INTO values_test VALUES ('001', 1.5, 4, NULL), ('009', 9.0, 'text', NULL);
        """)
    snapshot = build_metadata_service(tmp_path / "ws").build(path)
    columns = {c.name: c for c in snapshot.tables[0].columns}
    for name in ("code", "mixed", "empty"):
        profile = columns[name].profile
        assert profile.min is None and profile.max is None
        assert "min" not in profile.model_dump() and "max" not in profile.model_dump()
    assert columns["code"].profile.examples == ["001", "009"]
    assert columns["amount"].profile.min == 1.5
    assert columns["amount"].profile.max == 9.0


def test_value_explanation_import_and_atomic_semantic_update(database, tmp_path):
    directory = tmp_path / "descriptions"
    directory.mkdir()
    (directory / "items.csv").write_text(
        "original_column_name,column_description,value_description\n"
        "state,Payment state,P means paid; NULL means not recorded\n"
    )
    service = build_metadata_service(
        tmp_path / "ws", loader="bird", model=PassthroughFileModel()
    )
    snapshot = service.build(database, initial_files=[directory])
    column = snapshot.objects()["column.items.state"]
    assert column.description == "Payment state"
    assert column.value_description == "P means paid; NULL means not recorded"
    assert column.source_refs
    updated = service.update(
        [
            MetadataChange(
                object_id="column.items.state",
                field="description",
                expected_value=column.description,
                value="Recorded payment status",
            ),
            MetadataChange(
                object_id="column.items.state",
                field="value_description",
                expected_value=column.value_description,
                value="P: paid. NULL: not recorded.",
            ),
        ],
        expected_revision=1,
    )
    assert (
        updated.objects()["column.items.state"].value_description
        == "P: paid. NULL: not recorded."
    )


def test_legacy_value_explanation_migrates_without_text_bounds(database, tmp_path):
    snapshot = build_metadata_service(tmp_path / "ws").build(database)
    old = snapshot.model_dump(mode="json")
    old["schema_version"] = "3.1"
    column = next(
        c for t in old["tables"] for c in t["columns"] if c["name"] == "state"
    )
    column.pop("value_description")
    column["description"] = "Payment state\nValue explanation: P = paid"
    column["profile"].update(min="P", max="P")
    migrated = MetadataSnapshot.model_validate(old)
    current = migrated.objects()["column.items.state"]
    assert current.description == "Payment state"
    assert current.value_description == "P = paid"
    assert "min" not in current.profile.model_dump()
    assert old["schema_version"] == "3.1"
    assert column["profile"]["min"] == "P"


def test_relation_endpoints_migrate_and_disambiguate_dotted_names(database, tmp_path):
    from cortex.capabilities.metadata.references import column_reference

    service = build_metadata_service(tmp_path / "ws")
    snapshot = service.build(database)
    legacy = snapshot.model_dump(mode="json")
    legacy["schema_version"] = "3.2"
    legacy["relations"] = [
        {
            "id": "relation.legacy",
            "type": "join",
            "source_columns": ["column.items.order_id"],
            "target_columns": ["column.orders.id"],
            "expression": "s.order_id=t.id",
            "description": "Legacy join",
            "source_refs": [],
        }
    ]
    migrated = MetadataSnapshot.model_validate(legacy)
    assert migrated.relations[0].source_columns == ["items.order_id"]
    assert migrated.relations[0].target_columns == ["orders.id"]
    assert legacy["relations"][0]["source_columns"] == ["column.items.order_id"]
    assert column_reference("a.b", "c") == '"a.b".c'
    assert column_reference("a", "b.c") == 'a."b.c"'
    with pytest.raises(ValueError, match="unknown relation column"):
        service.propose_relation(
            RelationProposal(
                type="join",
                source_columns=["column.items.order_id"],
                target_columns=["orders.id"],
                expression="s.order_id=t.id",
                description="Wrong format",
            ),
            expected_revision=1,
        )


def test_agent_coverage_is_explicit_when_model_stops_early(database, tmp_path):
    class Stops:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(content="Done")

    service = build_metadata_service(
        tmp_path / "ws", model=Stops(), enrich_database=True
    )
    snapshot = service.build(database)
    result = service.runs[0][1]
    assert result.status == "incomplete"
    assert len(result.trace[-1]["coverage"]["unread_columns"]) == sum(
        len(t.columns) for t in snapshot.tables
    )


def test_agent_continues_to_read_columns_after_early_final(database, tmp_path):
    class ReadsAfterReminder:
        def bind_tools(self, tools):
            self.calls = 0
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="Done")
            if self.calls == 2:
                addresses = json.loads(
                    messages[-1].content.split("Remaining columns: ")[1]
                )
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "read",
                            "name": "inspect_metadata_batch",
                            "args": {"object_ids": addresses},
                        }
                    ],
                )
            return AIMessage(content="Unsupported meanings left null.")

    service = build_metadata_service(
        tmp_path / "ws", model=ReadsAfterReminder(), enrich_database=True
    )
    snapshot = service.build(database)
    result = service.runs[0][1]
    assert result.status == "completed"
    assert not result.trace[-1]["coverage"]["unread_columns"]
    assert all(c.description is None for t in snapshot.tables for c in t.columns)
