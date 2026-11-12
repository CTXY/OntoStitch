"""Uploaded databases share the normal readonly preview and training workflow."""
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from cortex.web.app import create_app
from cortex.web import database_files


@pytest.fixture
def database_bytes(tmp_path):
    path = tmp_path / 'source.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO customers VALUES (1, "Alice");')
    return path.read_bytes()


def test_upload_persists_and_exposes_schema_rows_without_overwriting(tmp_path, database_bytes):
    storage = tmp_path / 'runtime'
    def app():
        return create_app(database_root=tmp_path / 'bundled', storage=storage)
    with TestClient(app()) as client:
        first = client.post('/ontology/api/databases/upload', files={'file': ('../../customers.db', database_bytes)})
        assert first.status_code == 201
        item = first.json()
        assert item['filename'] == 'customers.db' and item['source'] == 'uploaded'
        assert item['tables'] == 1 and item['columns'] == 2 and item['available']
        assert not item['has_archive'] and not item['has_example_pairs']
        second = client.post('/api/databases/upload', files={'file': ('customers.db', database_bytes)}).json()
        assert second['id'] != item['id']
        assert client.get(f"/api/databases/{item['id']}/schema").json()['tables'][0]['name'] == 'customers'
        assert client.get(f"/api/databases/{item['id']}/rows?table=customers").json()['rows'] == [[1, 'Alice']]
        assert client.get(f"/api/databases/{item['id']}/graph").status_code == 404
    with TestClient(app()) as client:
        assert any(d['id'] == item['id'] for d in client.get('/api/databases').json()['databases'])
        assert client.get(f"/api/databases/{item['id']}/schema").status_code == 200
    assert (storage / 'databases' / item['id'] / 'database.sqlite').read_bytes() == database_bytes


@pytest.mark.parametrize('filename,content', [('wrong.json', b'{}'), ('wrong.db', b'SELECT 1;'), ('corrupt.sqlite', b'SQLite format 3\x00' + b'broken' * 100), ('empty.db', b'')])
def test_invalid_uploads_are_not_registered_or_left_on_disk(tmp_path, filename, content):
    storage = tmp_path / 'runtime'
    with TestClient(create_app(database_root=tmp_path / 'bundled', storage=storage)) as client:
        assert client.post('/api/databases/upload', files={'file': (filename, content)}).status_code == 422
        assert len(client.get('/api/databases').json()['databases']) == 2
    assert not list((storage / 'databases').glob('*'))


def test_oversized_database_is_removed(tmp_path, monkeypatch, database_bytes):
    monkeypatch.setattr(database_files, 'DATABASE_UPLOAD_LIMIT', 100)
    storage = tmp_path / 'runtime'
    with TestClient(create_app(storage=storage)) as client:
        assert client.post('/api/databases/upload', files={'file': ('large.sqlite', database_bytes)}).status_code == 413
    assert not list((storage / 'databases').glob('*'))


def test_uploaded_database_can_be_used_for_real_training_dispatch(tmp_path, database_bytes):
    captured = []
    class Runner:
        async def run(self, run, pairs, database, update):
            captured.append(database)
            assert len(pairs) == 1
            with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
                assert db.execute('SELECT name FROM customers').fetchone() == ('Alice',)
            update(status='completed', phase='completed', pairs_completed=1)
    storage = tmp_path / 'runtime'
    with TestClient(create_app(storage=storage, training_runner=Runner())) as client:
        item = client.post('/api/databases/upload', files={'file': ('customers.sqlite3', database_bytes)}).json()
        pairs = client.post('/api/training-files', params={'database_id': item['id']}, files={'file': ('pairs.jsonl', '{"question":"List customers","sql":"SELECT name FROM customers"}\n')}).json()
        assert client.post('/api/imports', json={'file_id': pairs['id']}).status_code == 200
        result = client.post('/api/sql-train', json={'import_id': pairs['id'], 'request_id': 'uploaded-test', 'case_limit': 1})
        assert result.status_code == 202 and result.json()['mode'] == 'live'
        for _ in range(50):
            if client.get('/api/runs/' + result.json()['id']).json()['status'] == 'completed':
                break
            time.sleep(.01)
        assert captured == [storage / 'databases' / item['id'] / 'database.sqlite']
