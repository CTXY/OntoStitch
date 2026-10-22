"""The synchronous retrieval bridge owns one event loop for a case."""

import asyncio
from contextvars import ContextVar
from threading import current_thread
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from cortex.bootstrap.sql_training_retrieval import CortexRetrieveDbGenerationClient
from cortex.schema import GraphSpace


class Result(BaseModel):
    status: str = "completed"
    question: str


def test_case_reuses_loop_preserves_call_context_and_closes_async_resources():
    context = ContextVar("retrieval-call", default="unset")
    loops, contexts, workers, writes, cleanup = [], [], [], [], []

    async def pending_work():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.append("cancelled")

    class Service:
        async def run(self, *, question, **kwargs):
            loops.append(asyncio.get_running_loop())
            contexts.append(context.get())
            workers.append(await asyncio.to_thread(current_thread))
            if len(loops) == 1:
                asyncio.create_task(pending_work())
                await asyncio.sleep(0)
            return Result(question=question)

    client = CortexRetrieveDbGenerationClient(
        retrieve_db=Service(),
        generation_client=SimpleNamespace(),
        artifact_recorder=SimpleNamespace(
            write=lambda result, **kwargs: writes.append(result)
        ),
    )

    def retrieve(question):
        return client.retrieve(
            question=question, graph=GraphSpace(), dialect="sqlite", trace_id=question
        )

    try:
        token = context.set("first-call")
        try:
            first = retrieve("first")
        finally:
            context.reset(token)
        token = context.set("second-call")
        try:
            retrieve("second")
        finally:
            context.reset(token)
        cached = retrieve("first")
        assert cached == first and cached is not first
        assert loops[0] is loops[1]
        assert contexts == ["first-call", "second-call"]
        assert len(writes) == 3
    finally:
        client.close()
    assert cleanup == ["cancelled"]
    assert loops[0].is_closed()
    assert all(not worker.is_alive() for worker in workers)
    client.close()  # repeated resource cleanup is safe
    with pytest.raises(RuntimeError, match="closed"):
        retrieve("first")  # cache hits cannot bypass the closed resource boundary
    with pytest.raises(RuntimeError, match="closed"):
        client.generate(None)


def test_retrieval_failure_keeps_case_loop_usable_until_close():
    loops = []

    class Service:
        async def run(self, *, question, **kwargs):
            loops.append(asyncio.get_running_loop())
            if len(loops) == 1:
                raise RuntimeError("retrieval failed")
            return Result(question=question)

    client = CortexRetrieveDbGenerationClient(
        retrieve_db=Service(),
        generation_client=SimpleNamespace(),
        artifact_recorder=SimpleNamespace(write=lambda *args, **kwargs: None),
    )
    try:
        with pytest.raises(RuntimeError, match="retrieval failed"):
            client.retrieve(
                question="first", graph=GraphSpace(), dialect="sqlite", trace_id="1"
            )
        result = client.retrieve(
            question="second", graph=GraphSpace(), dialect="sqlite", trace_id="2"
        )
        assert result.status == "completed"
        assert loops[0] is loops[1]
    finally:
        client.close()
    assert loops[0].is_closed()


@pytest.mark.parametrize("case_workers", [None, 2])
def test_shared_injected_model_does_not_use_native_async_client_across_cases(
    case_graph, monkeypatch, case_workers
):
    from langchain_core.messages import AIMessage
    from test_agent_runtime import ScriptModel

    from cortex.bootstrap import knowledge
    from cortex.config import CortexConfig

    _store, database, workspace = case_graph

    class SharedModel(ScriptModel):
        async def _agenerate(self, *args, **kwargs):
            raise AssertionError("shared native async client must not cross case loops")

    model = SharedModel([AIMessage(content="first"), AIMessage(content="second")])

    def build_retrieval(*, config, llm):
        class Service:
            async def run(self, *, question, **kwargs):
                await llm.ainvoke(question)
                return Result(question=question)

        return Service()

    monkeypatch.setattr(knowledge, "build_retrieve_db_service", build_retrieval)
    monkeypatch.setattr(
        knowledge,
        "FileSystemRetrieveDbArtifactRecorder",
        lambda *args: SimpleNamespace(write=lambda *args, **kwargs: None),
    )
    config = CortexConfig(
        services={"sql_training": {"case_max_workers": case_workers}}
    )
    service = knowledge._build_sql_service(
        config,
        "test",
        None,
        model,
        database,
        None,
        graph_merge=SimpleNamespace(),
        state_repository=SimpleNamespace(),
        workspace=workspace.root,
    )
    capability = service._engine
    first = capability.validation_client_factory()
    second = capability.validation_client_factory()
    try:
        for index, client in enumerate((first, second)):
            result = client.retrieve(
                question=f"case-{index}",
                graph=GraphSpace(),
                dialect="sqlite",
                trace_id=str(index),
            )
            assert result.status == "completed"
        assert len(model._calls) == 2
        assert first._runner.get_loop() is not second._runner.get_loop()
    finally:
        first.close()
        second.close()
        capability.validation_client.close()
