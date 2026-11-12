"""Public bootstrap contracts: one extraction assembly, optional full orchestration."""

import pytest
from test_offline_training_contract import RECORDS, SCHEMA, Engine, Merge, Repository

from cortex.bootstrap import build_sql_training_service, knowledge
from cortex.config import CortexConfig, config_fingerprint


@pytest.mark.parametrize("config_source", ["path", "object"])
@pytest.mark.parametrize("injected_engine", [False, True])
def test_public_service_accepts_both_config_forms_and_reuses_engine(
    tmp_path, monkeypatch, config_source, injected_engine
):
    config = CortexConfig()
    path = tmp_path / "config.yaml"
    path.write_text("{}\n")
    workspace = tmp_path / "training"
    loads, constructions, merge_constructions = [], [], []
    engine = Engine()
    repository = Repository()

    def load_config(value):
        loads.append(value)
        return config

    def build_engine(value, **kwargs):
        constructions.append((value, kwargs))
        return engine

    def build_merge(value, **kwargs):
        # Full service construction must not initialize merge before extraction.
        assert engine.calls == 1
        merge_constructions.append((value, kwargs))
        return Merge()

    monkeypatch.setattr(knowledge, "load_config", load_config)
    monkeypatch.setattr(knowledge, "build_sql_extraction", build_engine)
    monkeypatch.setattr(knowledge, "build_graph_merge_operator", build_merge)
    service = build_sql_training_service(
        path if config_source == "path" else config,
        workspace=workspace,
        engine=engine if injected_engine else None,
        state_repository=repository,
    )
    assert loads == ([path] if config_source == "path" else [])
    if injected_engine:
        assert not constructions
    else:
        assert constructions == [
            (
                config,
                {
                    "workspace": workspace,
                    "llm": None,
                    "database": None,
                    "validation_client": None,
                },
            )
        ]
    assert not merge_constructions
    result = service.run(RECORDS, SCHEMA)
    assert result.status == "completed"
    assert engine.calls == 1 and len(merge_constructions) == 1
    assert merge_constructions[0][0] is config
    merge_options = merge_constructions[0][1]
    assert merge_options.get("live_semantics", True) is True
    assert merge_options["evidence_resolver"].repository.workspace.root == workspace
    assert result.diagnostics.config_fingerprint == config_fingerprint(config)
    assert repository.saves == [result.state]


def test_explicit_engine_and_repository_are_retained_even_when_falsy(monkeypatch):
    class FalsyEngine(Engine):
        def __bool__(self):
            return False

    class FalsyRepository(Repository):
        def __bool__(self):
            return False

    def forbidden(*args, **kwargs):
        pytest.fail("an injected dependency was reconstructed")

    monkeypatch.setattr(knowledge, "build_sql_extraction", forbidden)
    monkeypatch.setattr(knowledge, "FileSystemGraphStateRepository", forbidden)
    engine, repository = FalsyEngine(), FalsyRepository()
    result = build_sql_training_service(
        CortexConfig(),
        engine=engine,
        graph_merge=Merge(),
        state_repository=repository,
    ).run(RECORDS, SCHEMA)
    assert result.status == "completed"
    assert engine.calls == 1 and repository.saves == [result.state]
