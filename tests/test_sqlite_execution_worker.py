import sqlite3
import sys

from cortex.infrastructure.database.execution import execute_query


def test_worker_matches_native_types_columns_nulls_and_readonly(tmp_path, monkeypatch):
    path = tmp_path / 'db with spaces.sqlite'
    db = sqlite3.connect(path)
    db.execute('create table items(id integer, amount real, label text, data blob)')
    db.execute('insert into items values(1, 1.5, NULL, ?)', (b'\x00\xff',))
    db.commit(); db.close()
    url = 'sqlite:///' + str(path)
    sql = 'select id as value, amount as value, label, data from items'
    native = execute_query(sql, database_url=url)
    monkeypatch.setenv('CORTEX_SQLITE_PYTHON', sys.executable)
    worker = execute_query(sql, database_url=url)
    assert worker == native
    assert not execute_query('delete from items', database_url=url).success
    assert execute_query('select count(*) from items', database_url=url).result['rows'] == [[1]]


def test_worker_timeout_is_not_success(tmp_path, monkeypatch):
    path = tmp_path / 'db.sqlite'
    sqlite3.connect(path).close()
    monkeypatch.setenv('CORTEX_SQLITE_PYTHON', sys.executable)
    result = execute_query('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n',
                           database_url='sqlite:///' + str(path), timeout=0.05)
    assert not result.success and result.result is None
    assert 'timed out' in result.error
