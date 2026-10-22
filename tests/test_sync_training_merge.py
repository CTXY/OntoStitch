"""Exercise the synchronous business boundary with real merge and model adapters."""

import asyncio
import inspect

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel
from test_agent_runtime import ScriptModel

from cortex.application.sql_training import (
    QASqlTrainingRecord,
    SqlTrainingEvaluationReport,
    SqlTrainingService,
)
from cortex.bootstrap.graph_merge import build_graph_merge_operator
from cortex.capabilities.graph_merge.identity import IdentityGrouper
from cortex.capabilities.graph_merge.llm.agents import LLMSemanticIdentityResolver
from cortex.capabilities.graph_merge.mining.base import GraphMiningResult
from cortex.capabilities.graph_merge.models import (
    GroupIdentityView,
    IdentityRepresentationView,
    NodeIdentityView,
    RepresentationRef,
)
from cortex.capabilities.sql_training.graph_merge_evidence import (
    MappingEvidenceRepository,
    SqlTrainingGraphMergeEvidenceResolver,
)
from cortex.capabilities.sql_training.runtime_config import RuntimeValidationConfig
from cortex.capabilities.sql_training.validation.in_process import (
    InProcessOntologySqlGenerator,
)
from cortex.capabilities.sql_training.validation.models import GenerationRequest
from cortex.config import CortexConfig
from cortex.infrastructure.llm.parsing import (
    run_llm_extraction,
    run_llm_extraction_sync,
)
from cortex.infrastructure.llm.providers import (
    LLMInvocationError,
    LLMStructuredOutputError,
)
from cortex.schema import GraphSpace, GraphState, SourceReference, load_root_schema


class Answer(BaseModel):
    value: int


def test_application_runs_real_merge_and_saves_without_event_loop(
    case_graph, monkeypatch
):
    store, _database, _workspace = case_graph
    incoming = GraphState(
        graph=store.validation_graph(store.working_revision), node_conflicts=()
    )
    report = SqlTrainingEvaluationReport(total=1, passed=1, failed=0)
    events = []

    class Engine:
        def train(self, records, schema, existing):
            assert records[0].source_id == "case"
            assert not existing.nodes
            events.append("train")
            return incoming, report

    class Repository:
        def load(self):
            return GraphState(graph=GraphSpace(), node_conflicts=())

        def save(self, state):
            events.append("save")
            self.saved = state

    def no_event_loop(*args, **kwargs):
        raise AssertionError("synchronous training/merge must not start an event loop")

    monkeypatch.setattr(asyncio, "run", no_event_loop)
    repo = Repository()
    service = SqlTrainingService(
        Engine(),
        graph_merge=build_graph_merge_operator(CortexConfig(), live_semantics=False),
        state_repository=repo,
    )
    result = service.run(
        (
            QASqlTrainingRecord(
                source_id="case",
                question="Count orders",
                sql="SELECT COUNT(*) FROM orders",
            ),
        ),
        load_root_schema(),
    )
    assert result.status == "completed"
    assert events == ["train", "save"]
    assert repo.saved == result.state
    assert len(result.state.graph.nodes) == len(incoming.graph.nodes)


def test_merge_deduplicates_exact_nodes_and_calls_miner_synchronously(case_graph):
    store, _database, _workspace = case_graph
    table = next(
        n
        for n in store.validation_graph(store.working_revision).nodes
        if n.node_type == "table"
    )
    incoming = table.model_copy(update={"id": "incoming-table"})
    operator = build_graph_merge_operator(CortexConfig(), live_semantics=False)
    seen = []

    class Miner:
        def propose(self, state, schema):
            seen.append(state)
            return GraphMiningResult()

    operator.graph_miner = Miner()
    existing = GraphState(graph=GraphSpace(nodes=(table,)), node_conflicts=())
    result = operator.merge(
        existing,
        GraphState(graph=GraphSpace(nodes=(incoming,)), node_conflicts=()),
        load_root_schema(),
    )
    assert len(result.state.graph.nodes) == 1
    assert result.node_mappings[0].canonical_id == table.id
    assert seen and seen[0].graph.nodes == result.state.graph.nodes
    assert not inspect.isawaitable(result)


def test_identity_uses_sync_embeddings_and_real_llm_resolver():
    calls = []

    class Embeddings:
        def embed_documents(self, texts):
            calls.append(texts)
            return [[1.0, 0.0] for _ in texts]

        def aembed_documents(self, texts):
            raise AssertionError("async embedding path used")

    model = ScriptModel(
        [
            AIMessage(
                content='{"selected_entry_id":"entry", "query_verdicts":[], "reason":"Same count intent"}'
            )
        ]
    )
    resolver = LLMSemanticIdentityResolver(model)
    grouper = IdentityGrouper(resolver, embeddings=Embeddings())
    assert grouper._embedding_vectors(("count rows", "number of rows")) == {
        "count rows": (1.0, 0.0),
        "number of rows": (1.0, 0.0),
    }
    assert calls == [["count rows", "number of rows"]]
    rep = IdentityRepresentationView(
        ref=RepresentationRef(origin="incoming_node", ref_id="incoming"),
        sources=(),
        identity_fields={"name": "count"},
        key_implementation={},
    )
    result = resolver.resolve(
        NodeIdentityView(
            query_id="incoming", node_type="generation_rule", representation=rep
        ),
        (
            GroupIdentityView(
                entry_id="entry", existing_node_id="existing", representations=(rep,)
            ),
        ),
        (),
        load_root_schema().node_type_index()["generation_rule"].identity_policy,
    )
    assert result.selected_entry_id == "entry"
    assert len(model._calls) == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_structured_call_repairs_once_and_preserves_trace(mode):
    model = ScriptModel(
        [AIMessage(content='{"value":"invalid"}'), AIMessage(content='{"value":7}')]
    )
    trace = {}
    kwargs = {
        "llm": model,
        "prompt": "Return a value",
        "output_model": Answer,
        "human_payload": {"source": "case"},
        "trace_capture": trace,
        "repair_with_context": True,
    }
    result = (
        run_llm_extraction_sync(**kwargs)
        if mode == "sync"
        else asyncio.run(run_llm_extraction(**kwargs))
    )
    assert result.value == 7
    assert len(model._calls) == 2
    assert trace["response"] == '{"value":"invalid"}'
    assert trace["repair_response"] == '{"value":7}'
    assert len(model._calls[1]) == 4


@pytest.mark.parametrize(
    "responses,allow_repair,expected_calls",
    [
        (["bad"], False, 1),
        (["bad", "still bad"], True, 2),
    ],
)
def test_sync_structured_call_bounds_invalid_output(
    responses, allow_repair, expected_calls
):
    model = ScriptModel([AIMessage(content=value) for value in responses])
    with pytest.raises(LLMStructuredOutputError):
        run_llm_extraction_sync(
            llm=model,
            prompt="Return a value",
            output_model=Answer,
            human_payload={},
            allow_repair=allow_repair,
        )
    assert len(model._calls) == expected_calls


def test_sync_structured_call_preserves_invocation_error():
    class BrokenModel:
        def invoke(self, messages):
            raise OSError("unavailable")

    with pytest.raises(LLMInvocationError):
        run_llm_extraction_sync(
            llm=BrokenModel(),
            prompt="Return a value",
            output_model=Answer,
            human_payload={},
        )


def test_training_sql_generator_invokes_sync_and_executes_real_sql(
    case_graph, monkeypatch
):
    store, database, _workspace = case_graph
    model = ScriptModel([AIMessage(content="SELECT COUNT(*) FROM orders")])
    generator = InProcessOntologySqlGenerator(
        llm=model, database=database, config=RuntimeValidationConfig(dialect="sqlite")
    )

    def no_event_loop(*args, **kwargs):
        raise AssertionError("SQL generator must call the synchronous model")

    monkeypatch.setattr(asyncio, "run", no_event_loop)
    result = generator.generate(
        GenerationRequest(
            trace_id="sync",
            question="How many orders?",
            dialect="sqlite",
            graph_space=store.validation_graph(store.working_revision),
            max_retry_count=0,
        )
    )
    assert result.code == 200
    assert result.data[0].query == "SELECT COUNT(*) FROM orders"
    assert len(model._calls) == 1


def test_evidence_resolution_and_cache_are_synchronous():
    from cortex.capabilities.graph_merge.reconciliation.service import (
        CachedEvidenceResolver,
    )

    source = SourceReference(source_id="case")
    resolver = SqlTrainingGraphMergeEvidenceResolver(
        MappingEvidenceRepository(
            {"case": {"question": "Count orders", "sql": "SELECT COUNT(*) FROM orders"}}
        )
    )
    cache = CachedEvidenceResolver(resolver)
    evidence = cache.resolve((source,))
    assert evidence[0].content["question"] == "Count orders"
    assert cache.resolve((source,)) == evidence


def test_reconciliation_preserves_unresolved_members_with_sync_feedback():
    from conftest import make_rule

    from cortex.capabilities.graph_merge.models import (
        ConsolidationProposal,
        ConsolidationReview,
        FinalNodeGroup,
        NodeContent,
        NodeRepresentation,
    )
    from cortex.capabilities.graph_merge.reconciliation.consolidation import (
        active_memory,
    )
    from cortex.capabilities.graph_merge.reconciliation.service import (
        NodeGroupReconciler,
    )

    calls = []

    class Proposer:
        def propose(self, *, group, **kwargs):
            calls.append("propose")
            return ConsolidationProposal(
                candidates=(),
                unresolved_refs=tuple(m.ref for m in group.members),
                reason="Keep both original instructions for review",
            )

    class Checker:
        def check(self, **kwargs):
            calls.append("check")
            return ConsolidationReview(
                verdicts=(),
                generalization={
                    "verdict": "not_applicable",
                    "reason": "Preservation only",
                },
                consistency={"verdict": "acceptable", "reason": "No original is lost"},
                reason="Preserve",
            )

    members = []
    for name, action in (
        ("first", "Count all records."),
        ("second", "Count distinct records."),
    ):
        asset = make_rule(name, action)
        members.append(
            NodeRepresentation(
                ref=RepresentationRef(origin="incoming_node", ref_id=asset.id),
                node_type="generation_rule",
                content=NodeContent(
                    name=asset.name,
                    description=None,
                    aliases=(),
                    tags=(),
                    properties=asset.properties.model_dump(mode="json"),
                ),
                sources=(),
            )
        )
    reconciler = NodeGroupReconciler(
        reconciler=None,
        evidence_resolver=None,
        branch_selector=None,
        generalizer=None,
        group_consolidator=Proposer(),
        consolidation_checker=Checker(),
    )
    result = reconciler.reconcile_group(
        FinalNodeGroup(
            group_id="group",
            node_type="generation_rule",
            existing_node_id=None,
            members=tuple(members),
        ),
        schema=load_root_schema(),
    )
    assert calls == ["propose", "check"]
    assert result.conflict is not None
    assert len(result.conflict.alternatives) == 2
    assert "CONSOLIDATION_PRESERVED_UNRESOLVED" in result.diagnostic_codes
    assert active_memory.get() is None


@pytest.mark.parametrize("fail_close", [False, True])
def test_sync_train_parallel_cases_close_owned_resources_before_return(
    case_graph, fail_close
):
    from contextlib import contextmanager
    from pathlib import Path
    from threading import Barrier, Lock, get_ident

    from test_agent_controller import Validator
    from test_agent_runtime import finish

    from cortex.capabilities.sql_training.engine import SqlTrainingCapability
    from cortex.capabilities.sql_training.runtime_config import (
        RuntimeDatabaseConfig,
        SqlTrainingRuntimeConfig,
    )
    from cortex.config.models import SQLiteDatabaseConfig, SqlTrainingConfig

    _store, database, workspace = case_graph
    rendezvous = Barrier(2)
    lock = Lock()
    lifecycle = []

    class OwnedValidator(Validator):
        def __init__(self):
            super().__init__()
            self.owner = get_ident()
            self.closed = False

        def generate(self, request):
            assert get_ident() == self.owner
            assert not self.closed
            if not self.generate_calls:
                rendezvous.wait(timeout=5)
            return super().generate(request)

    @contextmanager
    def case_resources():
        validator = OwnedValidator()
        with lock:
            lifecycle.append(validator)
            index = len(lifecycle)
        try:
            yield ScriptModel([finish()]), validator
        finally:
            assert get_ident() == validator.owner
            validator.closed = True
            if fail_close and index == 1:
                raise RuntimeError("case resource close failed")

    config = CortexConfig(
        database=SQLiteDatabaseConfig(path=Path(database.engine.url.database))
    )
    runtime = SqlTrainingRuntimeConfig(
        workspace=str(workspace.root),
        database=RuntimeDatabaseConfig(url=database.url, dialect="sqlite"),
        validation=RuntimeValidationConfig(dialect="sqlite"),
        train=SqlTrainingConfig(case_max_workers=2),
    )
    capability = SqlTrainingCapability(
        llm=ScriptModel([]),
        config=config,
        runtime_config=runtime,
        case_llm_factory=case_resources,
        database=database,
    )
    _state, report = capability.train(
        tuple(
            QASqlTrainingRecord(
                source_id=f"case-{index}",
                question="How many orders?",
                sql="SELECT COUNT(*) FROM orders",
            )
            for index in range(2)
        ),
        load_root_schema(),
        GraphSpace(),
    )
    assert len(lifecycle) == 2
    assert all(item.closed and item.generate_calls == 3 for item in lifecycle)
    assert len({item.owner for item in lifecycle}) == 2
    assert get_ident() not in {item.owner for item in lifecycle}
    assert report.passed == (1 if fail_close else 2)
    assert report.failed == int(fail_close)
