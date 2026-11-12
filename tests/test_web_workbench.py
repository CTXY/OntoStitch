"""Contract checks for uploads, readonly previews, recovery and archive compatibility."""

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ontology_v2_fixtures import discussion_state

from cortex.application.ontology_graph import GraphManager
from cortex.schema import GraphState, load_root_schema
from cortex.web.app import create_app
from cortex.web.graphs import load_graph

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/ontology"


@pytest.fixture
def app(tmp_path):
    for name in ("financial", "thrombosis_prediction"):
        directory = tmp_path / "databases" / name
        directory.mkdir(parents=True)
        with sqlite3.connect(directory / f"{name}.sqlite") as db:
            db.executescript(
                'CREATE TABLE "order" (id INTEGER PRIMARY KEY, label TEXT); INSERT INTO "order" VALUES (1, \'example\'), (2, NULL);'
            )
    return create_app(
        database_root=tmp_path / "databases",
        storage=tmp_path / "storage",
        examples=EXAMPLES,
    )


def upload(client, records):
    text = "\n".join(json.dumps(r) for r in records)
    response = client.post(
        "/ontology/api/training-files?database_id=financial",
        files={"file": ("pairs.jsonl", text)},
    )
    assert response.status_code == 200
    return response.json()


def test_database_pagination_and_identifier_guard(app):
    with TestClient(app) as client:
        assert len(client.get("/ontology/api/databases").json()["databases"]) == 2
        schema = client.get("/api/databases/financial/schema").json()
        assert schema["tables"][0]["columns"][0]["primary_key"]
        page = client.get(
            "/api/databases/financial/rows", params={"table": "order", "limit": 1}
        ).json()
        assert page["rows"] == [[1, "example"]] and page["has_more"]
        assert (
            client.get(
                "/api/databases/financial/rows",
                params={"table": 'order";DROP TABLE "order";--'},
            ).status_code
            == 404
        )


def test_input_errors_block_import_and_training(app):
    with TestClient(app) as client:
        data = upload(
            client,
            [
                {"question": "q", "sql": "SELECT 1", "source_id": "same"},
                {"NLQ": "q2", "SQL": "SELECT 2", "source_id": "same"},
                {"question": "q", "nlq": "conflict", "sql": "SELECT 1"},
                {"question": "q", "sql": "SELECT 1", "db_id": "other"},
            ],
        )
        assert [e["line"] for e in data["errors"]] == [2, 3, 4]
        assert (
            client.post("/api/imports", json={"file_id": data["id"]}).status_code == 422
        )
        assert (
            client.post(
                "/api/sql-train", json={"import_id": data["id"], "request_id": "bad"}
            ).status_code
            == 409
        )
        empty = client.post(
            "/api/training-files?database_id=financial",
            files={"file": ("empty.jsonl", "\n")},
        ).json()
        assert empty["count"] == 0 and empty["errors"]


def test_demo_build_is_idempotent_readonly_and_explicit(app):
    with TestClient(app) as client:
        data = upload(
            client,
            [
                {"NLQ": "rows?", "SQL": 'SELECT * FROM "order"'},
                {"question": "unsafe", "sql": 'DELETE FROM "order"'},
            ],
        )
        key = data["id"]
        assert client.post("/api/imports", json={"file_id": key}).status_code == 200
        assert client.get(f"/api/imports/{key}/pairs?limit=1").json()["total"] == 2
        request = {"import_id": key, "request_id": "once", "mode": "demo"}
        first = client.post("/api/sql-train", json=request)
        assert first.status_code == 202
        assert (
            client.post("/api/sql-train", json=request).json()["id"]
            == first.json()["id"]
        )
        assert (
            client.post(
                "/api/sql-train", json={**request, "request_id": "second"}
            ).status_code
            == 409
        )
        for _ in range(50):
            result = client.get(f"/api/runs/{first.json()['id']}").json()
            if result["status"] == "completed":
                break
            time.sleep(0.1)
        assert result["status"] == "completed"
        assert [v["executable"] for v in result["validation"]] == [True, False]
        assert "not learned" in result["result"]["note"]
        assert (
            len(client.get("/api/databases/financial/rows?table=order").json()["rows"])
            == 2
        )
        assert client.get(f"/api/runs/{first.json()['id']}/graph").status_code == 200


def test_restart_marks_unfinished_preview_interrupted(app):
    with TestClient(app) as client:
        data = upload(client, [{"question": "q", "sql": "SELECT 1"}])
        client.post("/api/imports", json={"file_id": data["id"]})
        run = client.post(
            "/api/sql-train",
            json={"import_id": data["id"], "request_id": "interrupt", "mode": "demo"},
        ).json()
    with TestClient(app) as client:
        assert client.get(f"/api/runs/{run['id']}").json()["status"] == "interrupted"


@pytest.mark.parametrize(
    "name, expected", [("financial", 245), ("thrombosis_prediction", 284)]
)
def test_historical_bundled_archives_require_regeneration(name, expected):
    envelope = json.loads((EXAMPLES / f"{name}.graph.json").read_text())
    state = GraphState.model_validate(envelope["state"])
    with pytest.raises(ValueError):
        GraphManager(load_root_schema()).validate_state(state)
    assert len(state.graph.nodes) == expected
    assert len(state.node_conflicts) == 4
    assert "node_conflicts" not in envelope["state"]
    assert all(
        "content" not in a
        for n in envelope["state"]["graph"]["nodes"]
        for a in n.get("alternatives", ())
    )
    assert envelope["compatibility"]["changes"]
    with pytest.raises(ValueError):
        load_graph(state.model_dump(mode="json"))


def test_graph_import_roundtrip_and_invalid_graph(app):
    data = discussion_state().model_dump(mode="json")
    with TestClient(app) as client:
        response = client.post(
            "/api/graphs/import", files={"file": ("graph.json", json.dumps(data))}
        )
        assert response.status_code == 200
        imported = response.json()
        actual = client.get(f"/api/graphs/{imported['id']}").json()["state"]
        assert actual == GraphState.model_validate(data).model_dump(mode="json")
        assert "node_conflicts" not in actual
        broken = json.loads(json.dumps(data))
        broken["graph"]["edges"][0]["to_node_id"] = "nonexistent"
        assert (
            client.post(
                "/api/graphs/import", files={"file": ("bad.json", json.dumps(broken))}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/graphs/import", files={"file": ("bad.json", "<html>bad</html>")}
            ).status_code
            == 422
        )


def test_legacy_archive_is_rejected_without_silent_semantic_conversion():
    def node(key, kind, properties):
        return {"id": key, "node_type": kind, "name": key, "properties": properties}

    nodes = [
        node("table.t", "table", {"table_name": "t"}),
        node(
            "column.t.id",
            "column",
            {"column_name": "id", "table_ref": "table.t", "data_type": "INTEGER"},
        ),
        node(
            "concept.id",
            "concept",
            {
                "targets": [
                    {
                        "id": "target.old",
                        "sql_expression": "t.id",
                        "references": ["column.t.id"],
                        "binding_operator": "exact",
                    }
                ]
            },
        ),
        node(
            "metric.sum",
            "metric",
            {
                "expression": {"expr": "SUM(${id})", "refs": {"id": "concept.id"}},
                "references": ["concept.id"],
                "calculation_policy": {"archived": True},
            },
        ),
        node("generation_rule.order", "generation_rule", {}),
    ]
    alternatives = []
    for name in ("ascending", "descending"):
        alternatives.append(
            {
                "alternative_id": name,
                "content": {
                    "name": name,
                    "description": None,
                    "aliases": [],
                    "tags": [],
                    "properties": {
                        "condition": "When sorting",
                        "action": f"Sort {name}",
                        "reason": "Match requested direction",
                        "references": ["column.t.id"],
                        "triggers": {"question": {"phrases": [name]}},
                    },
                },
                "sources": [],
            }
        )
    legacy = {
        "graph": {
            "nodes": nodes,
            "edges": [
                {
                    "id": "mapping",
                    "edge_type": "maps_to_column",
                    "from_node_id": "concept.id",
                    "to_node_id": "column.t.id",
                    "properties": {
                        "target_id": "target.old",
                        "binding_operator": "exact",
                    },
                }
            ],
        },
        "node_conflicts": [
            {"node_id": "generation_rule.order", "alternatives": alternatives}
        ],
    }
    # Historical refs/targets and full-content alternatives are not current-contract
    # inputs. Regeneration is explicit; no semantic inlining adapter is introduced.
    with pytest.raises(ValueError):
        load_graph(legacy)
    assert "calculation_policy" in legacy["graph"]["nodes"][3]["properties"]


def test_live_dispatch_limits_cases_and_publishes_only_run_result(tmp_path):
    class Runner:
        async def run(self, run, pairs, database, update):
            assert len(pairs) == 2
            assert database.is_file()
            update(
                status="running",
                phase="learning",
                pairs_completed=1,
                current_case=pairs[0]["source_id"],
            )
            update(
                status="completed",
                phase="completed",
                pairs_completed=2,
                elapsed_seconds=120,
                result={"origin": "sql_train", "nodes": 0, "edges": 0},
            )

        def graph(self, run_id):
            return {
                "run_id": run_id,
                "origin": "sql_train",
                "state": {"graph": {"nodes": [], "edges": []}, "node_conflicts": []},
            }

    directory = tmp_path / "databases/financial"
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "financial.sqlite") as conn:
        conn.execute("CREATE TABLE sample (id INTEGER)")
    app = create_app(
        database_root=tmp_path / "databases",
        storage=tmp_path / "storage",
        examples=EXAMPLES,
        training_runner=Runner(),
    )
    with TestClient(app) as client:
        data = upload(
            client, [{"question": f"q{i}", "sql": "SELECT 1"} for i in range(3)]
        )
        client.post("/api/imports", json={"file_id": data["id"]})
        request = {"import_id": data["id"], "request_id": "live-two"}
        response = client.post("/api/sql-train", json=request)
        assert response.status_code == 202
        run_id = response.json()["id"]
        for _ in range(50):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] == "completed":
                break
            time.sleep(0.01)
        assert run["mode"] == "live" and run["imported_total"] == 3
        assert run["pairs_total"] == run["pairs_completed"] == 2
        assert client.get(f"/api/runs/{run_id}/graph").json()["run_id"] == run_id
        assert client.post("/api/sql-train", json=request).json()["id"] == run_id
        assert (
            client.post("/api/sql-train", json={**request, "case_limit": 1}).status_code
            == 409
        )
        next_run = client.post(
            "/api/sql-train", json={**request, "request_id": "next-live-two"}
        ).json()
        assert next_run["eta_case_seconds"] == 60
        assert next_run["eta_sample_cases"] == 2
        assert next_run["eta_basis"] == "historical_completed_runs"


def test_live_preflight_rejects_postgres_without_exposing_url(monkeypatch):
    from cortex.web import training

    monkeypatch.setattr(
        training,
        "training_environment",
        lambda: {
            "CORTEX_LLM_MODEL": "test",
            "CORTEX_LLM_BASE_URL": "https://example.com",
            "CORTEX_LLM_API_KEY": "test",
            "GRAPH_MERGE_CHECKPOINT_URL": "postgresql://user:private-password@localhost/cortex",
        },
    )
    with pytest.raises(ValueError, match="file-backed sqlite") as error:
        training.check_training_configuration()
    assert "private-password" not in str(error.value)
