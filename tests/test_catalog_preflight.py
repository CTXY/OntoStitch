"""Physical database initialization must succeed before case workers run."""
from pathlib import Path
from types import SimpleNamespace as N

import pytest
from sqlalchemy import create_engine

from cortex.capabilities.sql_training.builders import build_catalog_assets
from cortex.capabilities.sql_training.graph.nodes import build_graph
from cortex.capabilities.sql_training.graph.join_edges import gold_join_edge
from cortex.capabilities.sql_training.pipeline.catalog_preflight import prepare_physical_catalog, validate_prepared_files
from cortex.capabilities.sql_training.runtime_config import SqlTrainingRuntimeConfig, RuntimeDatabaseConfig
from cortex.infrastructure.database import SQLAlchemyDatabase
from cortex.schema import load_root_schema, SourceReference
from cortex.schema.physical_joins import join_edge_key, merge_join_edges


def database(tmp_path):
    path = tmp_path / 'db.sqlite'
    engine = create_engine('sqlite:///' + str(path))
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE Country (ID INTEGER PRIMARY KEY)')
        c.exec_driver_sql('CREATE TABLE Item (ID INTEGER PRIMARY KEY, ParentID INTEGER, CountryID INTEGER, FOREIGN KEY(ParentID) REFERENCES item(id), FOREIGN KEY(CountryID) REFERENCES country(id))')
    return SQLAlchemyDatabase('sqlite:///' + str(path), engine=engine)


def test_sqlite_fk_names_resolve_to_actual_spelling_and_self_join_validates(tmp_path):
    db = database(tmp_path)
    catalog = db.introspect_catalog(sample_values=False)
    assert {(j.from_table,j.to_table,tuple(j.column_pairs)) for j in catalog.joins} == {
        ('Item','Country',(('CountryID','ID'),)),('Item','Item',(('ParentID','ID'),))}
    assets = build_catalog_assets(catalog.tables,catalog.columns,catalog.joins)
    graph = build_graph([*assets.tables,*assets.columns],assets.join_edges,load_root_schema())
    self_edge = next(e for e in graph.edges if e.edge_type=='joins_with' and e.from_node_id==e.to_node_id)
    assert self_edge.properties['cardinality']=='N:1'
    assert self_edge.properties['column_pairs']==[{'from_column_ref':'column.Item.ParentID','to_column_ref':'column.Item.ID'}]
    assert self_edge.sources
    assert db.introspect_catalog(include_tables=['country'],sample_values=False).tables[0].name=='Country'


def test_self_join_reversal_preserves_cardinality_and_sources():
    first=gold_join_edge(from_table='item',to_table='item',column_pairs=[('parent','id')],source_id='a').model_copy(update={'properties':{'column_pairs':[{'from_column_ref':'column.item.parent','to_column_ref':'column.item.id'}],'cardinality':'N:1'}})
    second=gold_join_edge(from_table='item',to_table='item',column_pairs=[('id','parent')],source_id='b').model_copy(update={'properties':{'column_pairs':[{'from_column_ref':'column.item.id','to_column_ref':'column.item.parent'}],'cardinality':'1:N'}})
    assert join_edge_key(first)==join_edge_key(second)
    merged,=merge_join_edges([first],[second])
    assert merged.properties['cardinality']=='N:1'
    assert {s.source_id for s in merged.sources}=={'a','b'}


def test_composite_self_join_identity_reverses_the_whole_key():
    def edge(pairs):return gold_join_edge(from_table='t',to_table='t',column_pairs=pairs,source_id='s')
    first=edge([('a','b'),('c','d')])
    assert join_edge_key(first)==join_edge_key(edge([('b','a'),('d','c')]))
    assert join_edge_key(first)!=join_edge_key(edge([('b','a'),('c','d')]))


def test_preflight_reuses_catalog_and_detects_tampering_without_sampling_again(tmp_path,monkeypatch):
    db=database(tmp_path)
    config=SqlTrainingRuntimeConfig(workspace=str(tmp_path/'case'),database=RuntimeDatabaseConfig(url=db.url,dialect='sqlite'))
    root=tmp_path/'physical';schema=load_root_schema()
    first=prepare_physical_catalog(config,db,schema,root)
    assert not first['reused'] and first['summary']['tables']==2
    monkeypatch.setattr(db,'sample_column_values',lambda *a,**k:pytest.fail('No repeat row sampling'))
    assert prepare_physical_catalog(config,db,schema,root)['reused']
    validate_prepared_files(root)
    (root/'assets/draft/columns.json').write_text('[]')
    with pytest.raises(ValueError,match='catalog changed'):
        validate_prepared_files(root)


def test_preflight_structural_error_stops_before_row_sampling(tmp_path,monkeypatch):
    db=database(tmp_path)
    # A structurally broken external FK is a database-level failure, not an LLM task.
    with db.engine.begin() as c:c.exec_driver_sql('CREATE TABLE broken (x INTEGER REFERENCES absent(id))')
    monkeypatch.setattr(db,'sample_column_values',lambda *a,**k:pytest.fail('Should fail before sampling'))
    config=SqlTrainingRuntimeConfig(workspace=str(tmp_path/'case'),database=RuntimeDatabaseConfig(url=db.url,dialect='sqlite'))
    with pytest.raises(ValueError,match='Unresolved database identifier'):
        prepare_physical_catalog(config,db,load_root_schema(),tmp_path/'physical')
    assert not (tmp_path/'physical/catalog_manifest.json').exists()


def test_preflight_checks_published_graph_checksum(tmp_path):
    db = database(tmp_path)
    config = SqlTrainingRuntimeConfig(workspace=str(tmp_path / 'case'),
                                     database=RuntimeDatabaseConfig(url=db.url, dialect='sqlite'))
    root = tmp_path / 'physical'
    manifest = prepare_physical_catalog(config, db, load_root_schema(), root)
    assert 'physical_graph.json' in manifest['files']
    (root / 'physical_graph.json').write_text('{}')
    with pytest.raises(ValueError, match='catalog changed'):
        validate_prepared_files(root)
