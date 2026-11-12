"""The public portal prefix must work for JSON, uploads and SPA deep links."""
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from cortex.web.app import create_app


@pytest.mark.parametrize('prefix', ['/api', '/ontology/api', '/ontomosaic-text2sql/api'])
def test_portal_prefix_preserves_upload_and_database_binding(tmp_path, prefix):
    database = tmp_path / 'source.sqlite'
    with sqlite3.connect(database) as db:
        db.executescript('CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);'
                         "INSERT INTO customers VALUES (1, 'Alice');")
    app = create_app(storage=tmp_path / 'runtime', database_root=tmp_path / 'bundled')
    with TestClient(app) as client:
        assert client.get(prefix + '/health').json()['status'] == 'ok'
        result = client.post(prefix + '/databases/upload', files={
            'file': ('customers.sqlite', database.read_bytes())})
        assert result.status_code == 201
        key = result.json()['id']
        assert client.get(prefix + f'/databases/{key}/schema').json()['tables'][0]['name'] == 'customers'
        assert client.get(prefix + f'/databases/{key}/rows?table=customers').json()['rows'] == [[1, 'Alice']]
        pair = json.dumps({'question': 'List customers', 'sql': 'SELECT name FROM customers'})
        uploaded = client.post(prefix + '/training-files', params={'database_id': key},
                               files={'file': ('pairs.jsonl', pair)}).json()
        assert uploaded['count'] == 1
        imported = client.post(prefix + '/imports', json={'file_id': uploaded['id']})
        assert imported.json()['imported']
        assert client.get(prefix + f"/imports/{uploaded['id']}/pairs").json()['total'] == 1
        assert client.get(prefix + '/does-not-exist').status_code == 404


def test_spa_deep_links_do_not_swallow_unknown_api_routes(tmp_path):
    frontend = tmp_path / 'dist'
    frontend.mkdir()
    (frontend / 'index.html').write_text('<html>portal</html>')
    app = create_app(frontend=frontend, storage=tmp_path / 'runtime')
    with TestClient(app) as client:
        for path in ['/', '/ontomosaic-text2sql', '/ontomosaic-text2sql/', '/ontology/']:
            assert client.get(path).text == '<html>portal</html>'
        for path in ['/api/missing', '/ontology/api/missing', '/ontomosaic-text2sql/api/missing']:
            response = client.get(path)
            assert response.status_code == 404
            assert response.headers['content-type'] == 'application/json'
