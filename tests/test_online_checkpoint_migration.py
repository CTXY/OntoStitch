import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from cortex.application.online_context import OnlineContextService, OnlineCheckpointCompatibilityError
from cortex.application.retrieve_db import RetrieveDbService
from cortex.application.retrieval import RetrievedRepresentation, RetrievalDocument
from cortex.capabilities.online_context.engine import OnlineContextCapability
from cortex.capabilities.online_context.output.guidance import GuidanceSynthesis
from cortex.capabilities.online_context.workflow.migration import migrate_recalled_state, checkpoint_values
from cortex.capabilities.retrieve_db.engine import RetrieveDbCapability
from cortex.capabilities.retrieve_db.retrieval.selection import KeepRecalledSelector
from cortex.schema import GraphNode, GraphSpace, GraphState, load_root_schema


@pytest.mark.parametrize('input_only',[True,False])
def test_cancelled_start_checkpoint_uses_native_replay(tmp_path,monkeypatch,input_only):
    from cortex.capabilities.online_context.workflow import runtime
    workflow=runtime.OnlineWorkflow(None,tmp_path/'online.sqlite',{})
    request={'question':'List a'}
    first=SimpleNamespace(values={'request':request,'identity':workflow.identity},next=(),
        metadata={'source':'input' if input_only else 'loop','step':-1 if input_only else 3},
        tasks=(SimpleNamespace(name='__start__',interrupts=()),) if input_only else ())
    final=SimpleNamespace(values={'result':{'status':'no_match','question':'List a','graph_state_fingerprint':'test'}})
    graph=SimpleNamespace(aget_state=AsyncMock(side_effect=[first,final]),aupdate_state=AsyncMock(),ainvoke=AsyncMock())
    monkeypatch.setattr(runtime,'build_online_graph',lambda *a,**k:graph)
    if input_only:
        result=asyncio.run(workflow.start(request,run_id='startup001'))
        assert result.status=='no_match'
        graph.aupdate_state.assert_not_called()
        assert graph.ainvoke.call_args.args[0] is None
    else:
        with pytest.raises(OnlineCheckpointCompatibilityError,match='no result or resumable task'):
            asyncio.run(workflow.start(request,run_id='startup001'))
        graph.aupdate_state.assert_not_called()
        graph.ainvoke.assert_not_called()


@pytest.mark.parametrize('has_old_result',[False,True])
def test_completed_pending_writes_resume_even_with_empty_next(tmp_path,monkeypatch,has_old_result):
    from cortex.capabilities.online_context.workflow import runtime
    workflow=runtime.OnlineWorkflow(None,tmp_path/'online.sqlite',{})
    request={'question':'List a'}
    values={'request':request,'identity':workflow.identity,'filter_decisions':[]}
    if has_old_result:values['result']={'status':'unavailable','question':'List a','graph_state_fingerprint':'test'}
    first=SimpleNamespace(values=values,next=(),metadata={'source':'loop','step':2},
        tasks=(SimpleNamespace(name='filter_questions',interrupts=()),))
    final=SimpleNamespace(values={'result':{'status':'no_match','question':'List a','graph_state_fingerprint':'test'}})
    graph=SimpleNamespace(aget_state=AsyncMock(side_effect=[first,final]),aupdate_state=AsyncMock(),ainvoke=AsyncMock())
    monkeypatch.setattr(runtime,'build_online_graph',lambda *a,**k:graph)
    result=asyncio.run(workflow.start(request,run_id='pendingwrites001'))
    assert result.status=='no_match'
    assert graph.ainvoke.call_args.args[0] is None
    graph.aupdate_state.assert_not_called()


class Sql:
    calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return "SELECT * FROM a"


class Recall:
    limits = {"column": 1}
    calls = 0

    async def recall(self, *args):
        self.calls += 1
        return (RetrievedRepresentation(document=RetrievalDocument(
            document_id="doc-a", canonical_node_id="table.a", node_type="table",
            representation_kind="canonical_node", searchable_text="a",
        ), score=1),)


class FailingSelector:
    async def select(self, **kwargs):
        raise RuntimeError("old relevance failed")


class Organizer:
    async def organize(self, **kwargs):
        return SimpleNamespace(choice_points=(), intra_node_assessments=())


class Guidance:
    async def synthesize(self, **kwargs):
        return GuidanceSynthesis(guidance_content="", interaction_guidance=())


def build(path, *, failing):
    schema = load_root_schema()
    sql, recall = Sql(), Recall()
    retrieval = RetrieveDbCapability(
        relevance_selector=FailingSelector() if failing else KeepRecalledSelector(),
        initial_sql_generator=sql, typed_recall=recall,
        checkpoint_root=path / "retrieval", ontology_schema=schema,
        context_settings={"policy": "old" if failing else "keep_all"},
    )
    engine = OnlineContextCapability(
        retrieval_service=RetrieveDbService(retrieval, schema=schema),
        choice_organizer=Organizer(), guidance_synthesizer=Guidance(),
        checkpoint_path=path / "online.sqlite", ontology_schema=schema,
    )
    return OnlineContextService(engine, schema=schema), sql, recall


def test_migration_reuses_completed_signals_and_recall_but_not_old_selection(tmp_path):
    asyncio.run(_migration_check(tmp_path))


async def _migration_check(tmp_path):
    old, old_sql, old_recall = build(tmp_path / "old", failing=True)
    graph = GraphState(graph=GraphSpace(nodes=(GraphNode(
        id="table.a", node_type="table", name="a", properties={"table_name": "a"},
    ),)))
    with pytest.raises(RuntimeError, match="old relevance"):
        await old.run(question="List a", graph_state=graph, run_id="old001")
    assert old_sql.calls == old_recall.calls == 1
    old_path = old._engine._workflow.path
    request = checkpoint_values(old_path, "old001")["request"]
    before = hashlib.sha256(old_path.read_bytes()).hexdigest()
    new, new_sql, new_recall = build(tmp_path / "new", failing=False)
    receipt = await migrate_recalled_state(
        source_online_path=old_path, source_retrieval_root=tmp_path / "old/retrieval",
        target_engine=new._engine, source_run_id="old001", target_run_id="new001",
        expected_request=request,
        source_stage_contract={"recall": "unchanged"},
        target_stage_contract={"recall": "unchanged"},
    )
    assert receipt["reused_stages"] == ["initial_sql", "recall"]
    result = await new.run(question="List a", graph_state=graph, run_id="new001")
    assert result.status.startswith("completed")
    assert new_sql.calls == new_recall.calls == 0
    assert result.selected_node_ids == ("table.a",)
    assert hashlib.sha256(old_path.read_bytes()).hexdigest() == before
    with pytest.raises(OnlineCheckpointCompatibilityError, match="recall code"):
        await migrate_recalled_state(
            source_online_path=old_path, source_retrieval_root=tmp_path / "old/retrieval",
            target_engine=new._engine, source_run_id="old001", target_run_id="changedcode001",
            expected_request=request, source_stage_contract={"recall": "old"},
            target_stage_contract={"recall": "changed"},
        )
    with pytest.raises(OnlineCheckpointCompatibilityError, match="source graph"):
        await migrate_recalled_state(
            source_online_path=old_path, source_retrieval_root=tmp_path / "old/retrieval",
            target_engine=new._engine, source_run_id="old001", target_run_id="different001",
            expected_request={**request, "question": "Different question"},
        )
