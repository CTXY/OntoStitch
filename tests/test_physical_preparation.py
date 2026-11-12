"""Regression coverage for physical facts and their online projection."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from cortex.capabilities.retrieve_db.join_paths import join_path_candidates
from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.builders import (
    build_catalog_assets,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.extractors.sql_normalizer import (
    collect_physical_column_references,
)
from cortex.capabilities.sql_training.graph.join_edges import (
    constraint_cardinality,
    extract_gold_join_edges,
    gold_join_edge,
    join_edge_key,
    merge_join_edges,
)
from cortex.capabilities.sql_training.graph.nodes import build_graph
from cortex.capabilities.sql_training.graph.values import merge_verified_values
from cortex.capabilities.sql_training.pipeline.grounding.bootstrap import (
    TrainingDimensionValueBootstrap,
)
from cortex.capabilities.sql_training.pipeline.physical import PhysicalPreparation
from cortex.capabilities.sql_training.training.trace import TrainTraceLogger
from cortex.infrastructure.database import SQLAlchemyDatabase
from cortex.schema import GraphSchemaError, load_root_schema, validate_graph_space


@pytest.fixture
def physical(tmp_path):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for statement in [
            "CREATE TABLE parent (tenant TEXT, id INTEGER, kind TEXT, PRIMARY KEY (tenant,id))",
            "CREATE TABLE child (tenant TEXT, pid INTEGER, kind TEXT, FOREIGN KEY(tenant,pid) REFERENCES parent(tenant,id))",
            "CREATE TABLE unique_child (tenant TEXT, pid INTEGER, UNIQUE(tenant,pid), FOREIGN KEY(tenant,pid) REFERENCES parent(tenant,id))",
            "CREATE TABLE loose (tenant TEXT, pid INTEGER, kind TEXT)",
            "INSERT INTO parent VALUES ('t',1,'inner')",
            "INSERT INTO child VALUES ('t',1,'outer')",
            "INSERT INTO loose VALUES ('t',1,'A/B'),('t',2,'A B'),('t',3,'A+B'),('t',4,' A/B ')",
        ]:
            connection.exec_driver_sql(statement)
    database = SQLAlchemyDatabase("sqlite://", engine=engine)
    catalog = database.introspect_catalog()
    assets = build_catalog_assets(catalog.tables, catalog.columns, catalog.joins)
    workspace = Workspace(tmp_path)
    workspace.write_draft_assets(NodeType.TABLE, assets.tables)
    workspace.write_draft_assets(NodeType.COLUMN, assets.columns)
    workspace.write_catalog_edges(assets.join_edges)
    config = SimpleNamespace(database=SimpleNamespace(dialect="sqlite"))
    yield config, workspace, database, assets
    engine.dispose()


TABLE_COLUMNS = {
    "a": {"id", "tenant", "kind", "name"},
    "b": {"id", "tenant", "kind", "a_id"},
}


def extract(sql):
    return extract_gold_join_edges(
        sql=sql, dialect="sqlite", source_id="q", table_columns=TABLE_COLUMNS
    )


def test_catalog_uses_only_declared_relationships_and_constraints(physical):
    _, _, _, assets = physical
    assert len(assets.join_edges) == 2
    by_table = {edge.from_node_id: edge for edge in assets.join_edges}
    assert by_table["table.child"].properties["cardinality"] == "N:1"
    assert by_table["table.unique_child"].properties["cardinality"] == "1:1"
    assert all(
        set(edge.properties) == {"column_pairs", "cardinality", "description"}
        for edge in assets.join_edges
    )
    assert len(by_table["table.child"].properties["column_pairs"]) == 2


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ([], [], None),
        ([["x"]], [], "1:N"),
        ([], [["y"]], "N:1"),
        ([["x"]], [["y"]], "1:1"),
        ([["x", "missing"]], [["y", "missing"]], None),
    ],
)
def test_cardinality_requires_complete_unique_key(left, right, expected):
    assert constraint_cardinality({"x"}, {"y"}, left, right) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM a JOIN b ON a.id=b.id AND a.tenant=b.tenant",
        "SELECT * FROM a LEFT JOIN b ON a.tenant=b.tenant AND a.id=b.id",
        "SELECT * FROM b RIGHT JOIN a ON b.id=a.id AND b.tenant=a.tenant",
    ],
)
def test_sql_join_operator_does_not_change_physical_identity(sql):
    (edge,) = extract(sql)
    assert len(edge.properties["column_pairs"]) == 2
    assert set(edge.properties) == {"column_pairs", "description"}
    (reference,) = extract("SELECT * FROM a JOIN b ON a.id=b.id AND a.tenant=b.tenant")
    assert join_edge_key(edge) == join_edge_key(reference)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM a WHERE id IN (SELECT a_id FROM b)",
        "SELECT a.id FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.a_id=a.id)",
        "WITH c AS (SELECT a_id AS k FROM b) SELECT a.id FROM a JOIN c ON a.id=c.k",
        "SELECT a.id FROM a JOIN (SELECT a_id AS k FROM b) c ON a.id=c.k",
    ],
)
def test_subqueries_do_not_create_physical_relationships(sql):
    assert extract(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM a JOIN b ON a.id=b.id OR a.tenant=b.tenant",
        "SELECT * FROM a JOIN b ON a.id=b.id AND a.tenant>b.tenant",
        "SELECT * FROM a JOIN b ON a.id=b.id AND a.tenant=LOWER(b.tenant)",
        "SELECT * FROM a JOIN a x ON a.id=x.id",
        "SELECT id FROM a WHERE id IN (SELECT MAX(a_id) FROM b)",
        "SELECT * FROM a JOIN (SELECT MAX(a_id) AS k FROM b) c ON a.id=c.k",
    ],
)
def test_unproven_or_partial_relationships_are_not_published(sql):
    assert extract(sql) == []


def test_join_llm_prompt_and_dispatch_are_removed():
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    from cortex.capabilities.sql_training.llm.provider import NodeLLMProvider

    with pytest.raises(ValueError, match="Unknown prompt"):
        load_prompt("gold_join_edges")
    with pytest.raises(ValueError, match="Unsupported extraction prompt"):
        NodeLLMProvider(object()).extract_assets(
            {}, context={"prompt_name": "gold_join_edges", "prompt": "unused"}
        )


def test_merge_reversed_constraint_and_unknown_provenance():
    forward = gold_join_edge(
        from_table="a", to_table="b", column_pairs=[("id", "a_id")], source_id="q"
    )
    reverse = gold_join_edge(
        from_table="b", to_table="a", column_pairs=[("a_id", "id")], source_id="db"
    )
    reverse = reverse.model_copy(
        update={"properties": {**reverse.properties, "cardinality": "N:1"}}
    )
    (merged,) = merge_join_edges([forward], [reverse])
    assert merged.id == forward.id
    assert merged.properties["cardinality"] == "1:N"
    assert len(merged.sources) == 2
    (reverse_result,) = merge_join_edges([reverse], [forward])
    assert reverse_result.properties["cardinality"] == "N:1"
    conflict = forward.model_copy(
        update={"properties": {**forward.properties, "cardinality": "N:1"}}
    )
    with pytest.raises(ValueError, match="conflicting"):
        merge_join_edges([conflict], [reverse])


def test_scope_shadowing_and_value_bootstrap(physical):
    config, workspace, database, _ = physical
    sql = "SELECT x.kind FROM child x WHERE x.kind='outer' AND EXISTS (SELECT 1 FROM parent x WHERE x.kind='inner')"
    prep = PhysicalPreparation(config, workspace, database)
    record = prep.add_gold_column_metadata({"sql": sql, "source_id": "q"})
    assert set(record["gold_column_metadata"]) == {
        "column.child.kind",
        "column.parent.kind",
    }
    result = TrainingDimensionValueBootstrap(config, workspace, database).run([record])
    assert result.dimension_value_count == 2
    actual = {
        (a.properties.references[0], a.properties.value)
        for a in workspace.read_draft_assets(NodeType.DIMENSION_VALUE)
    }
    assert actual == {("column.child.kind", "outer"), ("column.parent.kind", "inner")}


def test_renamed_passthrough_preserves_original_column():
    sql = "WITH c(k) AS (SELECT a.id FROM a) SELECT c.k FROM c"
    refs = collect_physical_column_references(
        sql, available_column_refs={"column.a.id", "column.a.k"}
    )
    assert refs == {"column.a.id"}


def test_dimension_value_identity_preserves_full_value_and_provenance(physical):
    config, workspace, database, _ = physical
    records = [
        {
            "source_id": str(i),
            "sql": "SELECT kind FROM loose WHERE kind IN ('A/B','A B','A+B',' A/B ')",
        }
        for i in range(2)
    ]
    calls = []
    original = database.find_exact_column_values

    def track(*args):
        calls.append(args)
        return original(*args)

    database.find_exact_column_values = track
    result = TrainingDimensionValueBootstrap(config, workspace, database).run(records)
    values = workspace.read_draft_assets(NodeType.DIMENSION_VALUE)
    assert result.dimension_value_count == 4
    assert len(calls) == 4
    assert len({asset.id for asset in values}) == 4
    assert all(len(asset.sources) == 3 for asset in values)
    assert make_node_id(
        NodeType.DIMENSION_VALUE, "t", "c", "x" * 121 + "a"
    ) != make_node_id(NodeType.DIMENSION_VALUE, "t", "c", "x" * 121 + "b")
    assert make_node_id(NodeType.DIMENSION_VALUE, "t", "c", "") != make_node_id(
        NodeType.DIMENSION_VALUE, "t", "c", " "
    )
    forged = values[0].model_copy(
        update={
            "properties": values[0].properties.model_copy(update={"value": "other"})
        }
    )
    with pytest.raises(ValueError, match="collides"):
        merge_verified_values(values, [forged])


def test_preparation_online_path_and_schema_boundary(physical):
    config, workspace, database, assets = physical
    records = [
        {
            "source_id": "q",
            "sql": "SELECT loose.pid FROM loose JOIN parent ON loose.pid=parent.id",
        }
    ]
    prep = PhysicalPreparation(config, workspace, database)
    # Physical preparation has no LLM/provider dependency.
    prep.prepare_gold_join_edges(records, TrainTraceLogger(workspace.root, "test"))
    joins = workspace.read_catalog_edges()
    loose = next(
        edge for edge in joins if "table.loose" in {edge.from_node_id, edge.to_node_id}
    )
    # A subset of composite PK is not unique.
    assert "cardinality" not in loose.properties
    graph = build_graph([*assets.tables, *assets.columns], joins, load_root_schema())
    paths = join_path_candidates(graph, {"table.child", "table.parent"})
    (step,) = next(p for p in paths if p["kind"] == "direct")["joins"]
    assert step["cardinality"] == "N:1"
    assert "join_type" not in step
    reverse = next(edge for edge in joins if edge.from_node_id == "table.child")
    from cortex.capabilities.retrieve_db.join_paths import _path

    assert (
        _path("table.parent", "table.child", (reverse,), None)["joins"][0][
            "cardinality"
        ]
        == "1:N"
    )
    broken = reverse.model_copy(
        update={
            "properties": {
                "description": "Deliberately invalid ownership test.",
                "column_pairs": [
                    {
                        "from_column_ref": "column.loose.pid",
                        "to_column_ref": "column.parent.id",
                    }
                ]
            }
        }
    )
    with pytest.raises(GraphSchemaError, match="belong"):
        validate_graph_space(
            graph.model_copy(update={"edges": (broken,)}), load_root_schema()
        )
    old = reverse.model_copy(
        update={"properties": {"default_join_type": "LEFT", "conditions": []}}
    )
    with pytest.raises(ValueError, match="rebuild"):
        merge_join_edges([], [old])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM a JOIN b ON (a.id=b.id AND a.tenant=b.tenant)",
        "SELECT * FROM a JOIN b USING (id,tenant)",
    ],
)
def test_complete_composite_keys_across_supported_constructs(sql):
    (edge,) = extract(sql)
    assert len(edge.properties["column_pairs"]) == 2


def test_different_alias_roles_are_not_combined_as_composite_key():
    edges = extract(
        "SELECT * FROM a JOIN b x ON a.id=x.id JOIN b y ON a.tenant=y.tenant"
    )
    assert len(edges) == 2
    assert all(len(edge.properties["column_pairs"]) == 1 for edge in edges)


def test_retrieval_closure_keeps_all_key_columns(physical):
    from cortex.capabilities.online_context.discovery.join_paths import render_join_path
    from cortex.capabilities.retrieve_db.closure import TypedGraphClosure
    from cortex.capabilities.retrieve_db.graph_index import GraphIndex

    _, _workspace, _, assets = physical
    graph = build_graph(
        [*assets.tables, *assets.columns], assets.join_edges, load_root_schema()
    )
    candidates = join_path_candidates(graph, {"table.child", "table.parent"})
    for paths in (None, candidates):
        result = TypedGraphClosure().expand(
            GraphIndex.build(graph), ("table.child", "table.parent"), paths
        )
        validate_graph_space(result.graph, load_root_schema())
        assert {
            "column.child.tenant",
            "column.child.pid",
            "column.parent.tenant",
            "column.parent.id",
        } <= {node.id for node in result.graph.nodes}
    text = render_join_path(candidates[0])
    assert "=" in text and "N:1" in text and " JOIN " not in text


def test_graph_merge_remaps_pairs_merges_reversed_constraints_and_sources(physical):
    from cortex.capabilities.graph_merge.edges import normalize_edges

    _, _workspace, _, assets = physical
    graph = build_graph(
        [*assets.tables, *assets.columns], assets.join_edges, load_root_schema()
    )
    existing = next(
        edge
        for edge in graph.edges
        if edge.edge_type == "joins_with" and edge.from_node_id == "table.child"
    )
    incoming = existing.model_copy(
        update={
            "id": "incoming-join",
            "from_node_id": "new-parent",
            "to_node_id": "new-child",
            "properties": {
                "description": "Deliberately invalid ownership test.",
                "column_pairs": [
                    {
                        "from_column_ref": "new-" + pair["to_column_ref"],
                        "to_column_ref": "new-" + pair["from_column_ref"],
                    }
                    for pair in reversed(existing.properties["column_pairs"])
                ]
            },
        }
    )
    mapping = {
        "new-parent": "table.parent",
        "new-child": "table.child",
        **{
            "new-" + pair[key]: pair[key]
            for pair in existing.properties["column_pairs"]
            for key in pair
        },
    }
    result = normalize_edges(
        existing=(existing,),
        incoming=(incoming,),
        nodes=graph.nodes,
        node_mappings=mapping,
        schema=load_root_schema(),
    )
    (edge,) = result.edges
    assert edge.id == existing.id
    assert edge.properties["cardinality"] == "N:1"
    assert result.mappings == (("incoming-join", existing.id),)
    validate_graph_space(
        graph.model_copy(update={"edges": result.edges}), load_root_schema()
    )


def test_physical_names_with_spaces_and_nested_aliases(tmp_path):
    from cortex.infrastructure.database.models import ColumnProfile, TableProfile

    tables = [TableProfile(name="Table A"), TableProfile(name="Table B")]
    columns = [
        ColumnProfile(
            table_name=table.table_name, column_name="Join ID", data_type="TEXT"
        )
        for table in tables
    ]
    catalog = build_catalog_assets(tables, columns)
    workspace = Workspace(tmp_path)
    workspace.write_draft_assets(NodeType.TABLE, catalog.tables)
    workspace.write_draft_assets(NodeType.COLUMN, catalog.columns)
    config = SimpleNamespace(database=SimpleNamespace(dialect="sqlite"))
    prep = PhysicalPreparation(config, workspace, None)
    record = {
        "source_id": "q",
        "sql": 'SELECT x."Join ID" FROM "Table A" x JOIN "Table B" y ON x."Join ID"=y."Join ID"',
    }
    metadata = prep.add_gold_column_metadata(record)
    assert set(metadata["gold_column_metadata"]) == {
        column.id for column in catalog.columns
    }
    prep.prepare_gold_join_edges([record], TrainTraceLogger(tmp_path, "test"))
    graph = build_graph(
        [*catalog.tables, *catalog.columns],
        workspace.read_catalog_edges(),
        load_root_schema(),
    )
    validate_graph_space(graph, load_root_schema())


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM a JOIN b ON a.id=b.id AND a.tenant=b.missing",
        "SELECT * FROM a JOIN b ON a.id=b.id AND a.tenant=LOWER(b.missing)",
    ],
)
def test_unresolved_composite_condition_does_not_publish_partial_edge(sql):
    assert extract(sql) == []


def test_derived_output_alias_does_not_bind_to_same_named_storage_column():
    refs = collect_physical_column_references(
        "SELECT COUNT(*) AS id FROM a ORDER BY id",
        available_column_refs={"column.a.id"},
    )
    assert refs == set()


def test_same_lexical_identity_cannot_hide_different_stored_types():
    from cortex.capabilities.sql_training.builders import build_dimension_value_assets

    values = build_dimension_value_assets(
        [
            {"table_name": "t", "column_name": "c", "value": 1},
            {"table_name": "t", "column_name": "c", "value": "1"},
        ]
    )
    with pytest.raises(ValueError, match="collides"):
        merge_verified_values([], values)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM a,b WHERE a.id=b.id AND a.tenant=b.tenant",
        "SELECT * FROM a WHERE a.id IN (SELECT b.a_id FROM b WHERE b.tenant=a.tenant)",
        "SELECT * FROM a WHERE EXISTS (SELECT 1 FROM b WHERE a.id=b.a_id AND a.tenant=b.tenant)",
        "SELECT * FROM a JOIN b ON a.id=b.id WHERE a.tenant=b.tenant",
        "SELECT * FROM a JOIN b ON a.id=b.id AND b.kind='active'",
        "SELECT * FROM a JOIN b ON a.id=b.id AND EXISTS (SELECT 1 FROM a)",
        "SELECT * FROM a JOIN b ON a.id IN (SELECT b.a_id FROM b)",
        "SELECT * FROM a WHERE EXISTS (SELECT 1 FROM a x JOIN b ON x.id=b.id)",
        "SELECT * FROM a NATURAL JOIN b",
        "SELECT * FROM a CROSS JOIN b",
    ],
)
def test_only_explicit_complete_equality_keys_are_published(sql):
    skipped = []
    assert (
        extract_gold_join_edges(
            sql=sql,
            dialect="sqlite",
            source_id="q",
            table_columns=TABLE_COLUMNS,
            skipped=skipped,
        )
        == []
    )
    if " JOIN " in sql:
        assert skipped


def test_mixed_qualified_columns_share_one_source_identity():
    columns = {"a": {"id", "tenant_a"}, "b": {"a_id", "tenant_b"}}
    edges = extract_gold_join_edges(
        sql="SELECT * FROM a x JOIN b y ON x.id=y.a_id AND tenant_a=y.tenant_b",
        dialect="sqlite",
        source_id="q",
        table_columns=columns,
    )
    (edge,) = edges
    assert len(edge.properties["column_pairs"]) == 2
    assert {pair["from_column_ref"] for pair in edge.properties["column_pairs"]} == {
        "column.a.id",
        "column.a.tenant_a",
    }


def test_derived_local_output_shadows_outer_physical_column(physical):
    config, workspace, database, _ = physical
    sql = "SELECT p.kind FROM parent p WHERE EXISTS (SELECT 1 FROM (SELECT UPPER(c.kind) AS kind FROM child c) d WHERE kind='OUTER')"
    record = PhysicalPreparation(config, workspace, database).add_gold_column_metadata(
        {"source_id": "q", "sql": sql}
    )
    assert set(record["gold_column_metadata"]) == {
        "column.parent.kind",
        "column.child.kind",
    }
    result = TrainingDimensionValueBootstrap(config, workspace, database).run([record])
    # The predicate is on computed d.kind; it is not evidence for p.kind or c.kind.
    assert result.evidence == ()
    assert result.dimension_value_count == 0
    from cortex.capabilities.sql_training.pipeline.grounding.expansion import (
        analyze_referenced_string_columns,
    )

    columns = workspace.read_draft_assets(NodeType.COLUMN)
    tables = workspace.read_draft_assets(NodeType.TABLE)
    _, ledger = analyze_referenced_string_columns(
        [record], column_assets=columns, table_assets=tables, dialect="sqlite"
    )
    local = [item for item in ledger if item.raw_reference == "kind"]
    assert len(local) == 1 and local[0].resolved_column_ref is None


def test_local_computed_column_never_falls_back_to_outer_scope():
    sql = "SELECT a.name FROM a WHERE EXISTS (SELECT 1 FROM (SELECT COUNT(*) AS id FROM b) d WHERE id=1)"
    assert collect_physical_column_references(
        sql, available_column_refs={"column.a.name", "column.a.id", "column.b.id"}
    ) == {"column.a.name"}


def test_remapped_graph_ids_work_in_paths_rules_and_closure(physical):
    from cortex.capabilities.graph_merge.edges import remap_property_node_ids
    from cortex.capabilities.retrieve_db.closure import TypedGraphClosure
    from cortex.capabilities.retrieve_db.graph_index import GraphIndex
    from cortex.capabilities.retrieve_db.join_paths import semantic_tables_from_sql
    from cortex.schema import GraphEdge, GraphNode, GraphSpace

    _, _workspace, _, assets = physical
    graph = build_graph(
        [*assets.tables, *assets.columns], assets.join_edges, load_root_schema()
    )
    mapping = {node.id: f"n{index}" for index, node in enumerate(graph.nodes)}
    nodes = tuple(
        node.model_copy(
            update={
                "id": mapping[node.id],
                "properties": remap_property_node_ids(node.properties, mapping),
            }
        )
        for node in graph.nodes
    )
    edges = tuple(
        edge.model_copy(
            update={
                "from_node_id": mapping[edge.from_node_id],
                "to_node_id": mapping[edge.to_node_id],
                "properties": remap_property_node_ids(edge.properties, mapping),
            }
        )
        for edge in graph.edges
    )
    rule = GraphNode(
        id="rule",
        node_type="generation_rule",
        name="association",
        properties={
            "operation_intents": ["joinability"],
            "triggers": {"sql": {"operations": ["join"]}},
            "condition": "Associated child and parent records requested",
            "action": "Join on the complete composite key",
            "reason": "Avoid cross-tenant matches",

        },
    )
    rule_edges = tuple(GraphEdge(id=f"rule_operand_{i}", edge_type="grounded_on", from_node_id="rule", to_node_id=mapping[ref], properties={"description": "This key field participates in the complete composite join condition."}) for i, ref in enumerate(("column.child.pid", "column.child.tenant", "column.parent.id", "column.parent.tenant")))
    renamed = GraphSpace(nodes=(*nodes, rule), edges=(*edges, *rule_edges))
    from cortex.application.ontology_graph import GraphManager
    from cortex.schema import GraphState

    renamed = (
        GraphManager(load_root_schema())
        .prepare_state(GraphState(graph=renamed, node_conflicts=()))[0]
        .graph
    )
    validate_graph_space(renamed, load_root_schema())
    endpoints = {mapping["table.child"], mapping["table.parent"]}
    candidates = join_path_candidates(renamed, endpoints)
    assert len(candidates) == 1 and candidates[0]["joinability_rule_id"] == "rule"
    assert (
        semantic_tables_from_sql(
            "SELECT c.kind,p.kind FROM child c JOIN parent p ON c.pid=p.id", renamed
        )
        == endpoints
    )
    closure = TypedGraphClosure().expand(
        GraphIndex.build(renamed), tuple(endpoints), candidates
    )
    validate_graph_space(closure.graph, load_root_schema())
    assert {
        mapping[ref]
        for ref in (
            "column.child.pid",
            "column.child.tenant",
            "column.parent.id",
            "column.parent.tenant",
        )
    } <= {node.id for node in closure.graph.nodes}


def test_stored_value_identity_is_shared_by_bootstrap_edits_and_reload(tmp_path):
    from test_agent_graph import edit

    from cortex.capabilities.sql_training.graph.snapshots import GraphStore
    from cortex.capabilities.sql_training.models import NodeAsset

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as c:
            c.exec_driver_sql(
                "CREATE TABLE codes (status_code INTEGER, name TEXT COLLATE NOCASE)"
            )
            c.exec_driver_sql("INSERT INTO codes VALUES (1,'North')")
        database = SQLAlchemyDatabase("sqlite://", engine=engine)
        catalog = database.introspect_catalog()
        assets = build_catalog_assets(catalog.tables, catalog.columns, catalog.joins)
        workspace = Workspace(tmp_path / "workspace")
        workspace.write_draft_assets(NodeType.TABLE, assets.tables)
        workspace.write_draft_assets(NodeType.COLUMN, assets.columns)
        config = SimpleNamespace(database=SimpleNamespace(dialect="sqlite"))
        TrainingDimensionValueBootstrap(config, workspace, database).run(
            [
                {
                    "source_id": "q",
                    "sql": "SELECT * FROM codes WHERE status_code='01' AND name='north'",
                }
            ]
        )
        initial = workspace.read_draft_assets(NodeType.DIMENSION_VALUE)
        expected = {
            make_node_id(NodeType.DIMENSION_VALUE, "codes", "status_code", 1),
            make_node_id(NodeType.DIMENSION_VALUE, "codes", "name", "North"),
        }
        assert {asset.id for asset in initial} == expected
        store = GraphStore(
            tmp_path / "ontology", workspace=workspace, schema=load_root_schema()
        )
        store.initialize([*assets.tables, *assets.columns, *initial], [])
        concept = NodeAsset.model_validate(
            {
                "id": "concept.active",
                "node_type": "concept",
                "name": "active",
                "properties": {
                    "targets": [[make_node_id(NodeType.DIMENSION_VALUE, "codes", "status_code", 1)], [make_node_id(NodeType.DIMENSION_VALUE, "codes", "name", "North")]],
                    "target_expressions": {"0": "codes.status_code = '01'", "1": "codes.name = 'north'"},
                },
            }
        )
        snap = edit(store, database, concept)
        values = [
            asset
            for asset in snap.assets
            if asset.node_type == NodeType.DIMENSION_VALUE
        ]
        assert {asset.id for asset in values} == expected
        assert len(values) == 2
        stored_concept = snap.semantic_assets[0]
        assert expected <= set(stored_concept.references())
        assert (
            stored_concept.properties.target_expressions["0"]
            == "codes.status_code = '01'"
        )
        restored = GraphStore(
            tmp_path / "ontology", workspace=workspace, schema=load_root_schema()
        )
        restored.restore(snap.revision, snap.revision)
        restored.validation_graph(snap.revision)
        again = edit(restored, database, stored_concept, "update", "second")
        assert (
            len(
                [
                    asset
                    for asset in again.assets
                    if asset.node_type == NodeType.DIMENSION_VALUE
                ]
            )
            == 2
        )
    finally:
        engine.dispose()


def test_original_metadata_survives_graph_and_case_projection(physical, tmp_path):
    import csv
    config, workspace, database, _ = physical
    catalog = database.introspect_catalog()
    columns = [c.model_copy(update={"comment": "Database meaning"}) if (c.table_name, c.column_name) == ("parent", "kind") else c for c in catalog.columns]
    descriptions = tmp_path / "descriptions"
    descriptions.mkdir()
    with (descriptions / "parent.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["original_column_name", "column_name", "column_description", "value_description"])
        writer.writeheader()
        writer.writerow({"original_column_name": "kind", "column_name": "Business category", "column_description": "External meaning", "value_description": "A: first; B: second"})
    assets = build_catalog_assets(catalog.tables, columns, catalog.joins, description_directory=descriptions)
    column = next(a for a in assets.columns if a.id == "column.parent.kind")
    assert column.description == "Database meaning"
    assert column.properties.source_metadata["external:parent.csv"]["description"] == "External meaning"
    graph = build_graph([*assets.tables, *assets.columns], assets.join_edges, load_root_schema())
    physical_node = next(n for n in graph.nodes if n.id == column.id)
    assert physical_node.properties["source_metadata"] == column.properties.source_metadata
    workspace.write_draft_assets(NodeType.COLUMN, assets.columns)
    workspace.write_draft_assets(NodeType.TABLE, assets.tables)
    record = PhysicalPreparation(config, workspace, database).add_gold_column_metadata({"sql": "SELECT kind FROM parent"})
    assert record["gold_column_metadata"][column.id]["source_metadata"] == column.properties.source_metadata


def test_compact_metadata_marks_truncation_and_omits_long_value_catalog():
    from cortex.capabilities.sql_training.builders.metadata import (
        compact_column_metadata,
    )
    source = {"external:items.csv": {"business_name": "Category", "description": "x" * 500, "value_description": "many codes"}}
    summary = compact_column_metadata(source)["external:items.csv"]
    assert len(summary["description"]) == 400
    assert "detail" in summary
    assert "value_description" not in summary


def test_description_path_resolves_and_reaches_training_runtime(tmp_path):
    from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig
    from cortex.config import load_config
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("database:\n  type: sqlite\n  path: data.sqlite\n  description_path: descriptions\n")
    config = load_config(config_path)
    assert config.database.description_path == tmp_path / "descriptions"
    assert SqlTrainingRuntimeConfig.from_cortex(config).database.description_path == str(tmp_path / "descriptions")
