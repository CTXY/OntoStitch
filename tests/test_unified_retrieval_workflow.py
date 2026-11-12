"""Shared retrieval preserves source evidence and replays completed graph nodes."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from cortex.application.ontology_graph import GraphManager
from cortex.application.retrieval import RelevanceDecision, RetrievedRepresentation
from cortex.application.retrieve_db import RetrieveDbService
from cortex.capabilities.retrieve_db.engine import RetrieveDbCapability
from cortex.infrastructure.retrieve.graph import FileSystemGraphSpaceLoader
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    NodeContent,
    SourceReference,
    load_root_schema,
)


@pytest.fixture
def source():
    schema = load_root_schema()
    manager = GraphManager(schema)
    table = GraphNode(
        id="table.t", node_type="table", name="t", properties={"table_name": "t"}
    )
    a = GraphNode(
        id="column.t.a",
        node_type="column",
        name="a",
        properties={"table_ref": table.id, "column_name": "a"},
    )
    b = GraphNode(
        id="column.t.b",
        node_type="column",
        name="b",
        properties={"table_ref": table.id, "column_name": "b"},
    )
    common = GraphNode(
        id="concept.amount",
        node_type="concept",
        name="amount",
        sources=(
            SourceReference(source_id="case.a"),
            SourceReference(source_id="case.b"),
        ),
    )
    contents = [
        NodeContent(
            name="amount",
            description=None,
            aliases=(),
            tags=(),
            properties={
                "targets": [[ref]],
                "target_expressions": {"0": f"t.{name}"},
            },
        )
        for name, ref in (("a", a.id), ("b", b.id))
    ]
    node, conflict = manager.compact_conflict(
        common,
        [
            ("option_a", contents[0], (SourceReference(source_id="case.a"),)),
            ("option_b", contents[1], (SourceReference(source_id="case.b"),)),
        ],
    )
    state, _ = manager.prepare_state(
        GraphState(
            graph=GraphSpace(
                nodes=(table, a, b, node),
                edges=tuple(
                    GraphEdge(
                        id=f"edge.amount.{key}",
                        edge_type="grounded_on",
                        from_node_id=node.id,
                        to_node_id=column.id,
                        properties={
                            "description": "This field supplies the amount matching.",
                            "alternative_ids": [key],
                        },
                    )
                    for key, column in (("option_a", a), ("option_b", b))
                ),
            ),
            node_conflicts=(conflict,),
        ),
        strict=True,
    )
    return state


class Recall:
    limits: ClassVar = {"concept": 12}

    def __init__(self, empty=False):
        self.calls = 0
        self.empty = empty

    async def recall(self, index, question, sql):
        self.calls += 1
        return (
            ()
            if self.empty
            else tuple(
                RetrievedRepresentation(
                    document=doc, score=0.8, channels=("semantic_embedding",)
                )
                for doc in index.documents
                if doc.canonical_node_id == "concept.amount"
                and doc.representation_kind == "canonical_node"
            )
        )


class Selector:
    async def select(self, *, representations, **kwargs):
        return tuple(
            RelevanceDecision(
                document_id=item.document.document_id,
                status="relevant",
                reason="matches the requested amount",
            )
            for item in representations
        )


def service(tmp_path, recall):
    return RetrieveDbService(
        RetrieveDbCapability(
            relevance_selector=Selector(), typed_recall=recall, checkpoint_root=tmp_path
        ),
        schema=load_root_schema(),
    )


def test_full_conflict_snapshot_and_durable_replay(tmp_path, source):
    recall = Recall()
    retrieval = service(tmp_path, recall)

    async def run():
        first = await retrieval.run(
            question="amount", graph_state=source, run_id="same-request"
        )
        second = await retrieval.run(
            question="amount", graph_state=source, run_id="same-request"
        )
        assert first == second
        assert first.retrieved_graph_state.node_conflicts == source.node_conflicts
        assert {node.id for node in first.subgraph.nodes} == {
            node.id for node in source.graph.nodes
        }
        assert first.representations and first.relevance
        inline = next(
            node
            for node in first.relevant_graph["nodes"]
            if node["id"] == "concept.amount"
        )
        assert inline["merge_status"] == "conflict"
        assert first.relevant_semantic_node_ids == ("concept.amount",)
        assert recall.calls == 1
        with pytest.raises(ValueError, match="cannot resume"):
            await retrieval.run(
                question="a different question",
                graph_state=source,
                run_id="same-request",
            )

    asyncio.run(run())


def test_no_match_and_legacy_plain_graph_call(tmp_path):
    retrieval = service(tmp_path, Recall(empty=True))
    result = asyncio.run(retrieval.run(question="nothing", graph=GraphSpace()))
    assert result.status == "no_match"
    assert result.relevant_graph is None
    assert not result.representations


def test_loader_keeps_conflicts(tmp_path, source):
    path = tmp_path / "graph_state.json"
    path.write_text(source.model_dump_json())
    loaded = FileSystemGraphSpaceLoader().load_state(path)
    assert loaded == source


def test_failed_selection_resumes_without_repeating_recall(tmp_path, source):
    class RetrySelector(Selector):
        def __init__(self):
            self.failed = False

        async def select(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("transient selection failure")
            return await super().select(**kwargs)

    recall = Recall()
    capability = RetrieveDbCapability(
        relevance_selector=RetrySelector(),
        typed_recall=recall,
        checkpoint_root=tmp_path,
    )
    retrieval = RetrieveDbService(capability, schema=load_root_schema())

    async def run():
        with pytest.raises(RuntimeError, match="transient"):
            await retrieval.run(question="amount", graph_state=source, run_id="retry")
        result = await retrieval.run(
            question="amount", graph_state=source, run_id="retry"
        )
        assert result.status == "completed"
        assert recall.calls == 1

    asyncio.run(run())


def test_required_historical_value_is_preserved_without_direct_recall(tmp_path, source):
    manager = GraphManager(load_root_schema())
    physical = tuple(
        node for node in source.graph.nodes if node.node_type in {"table", "column"}
    )
    value = GraphNode(
        id="dimension_value.historical",
        node_type="dimension_value",
        name="historical literal",
        properties={"column_ref": "column.t.a", "value": "x"},
        sources=(SourceReference(source_id="case.a", locator="qa"),),
    )
    concept = GraphNode(
        id="concept.amount",
        node_type="concept",
        name="amount",
        properties={
            "targets": [[value.id]],
            "target_expressions": {"0": "t.a = 'x'"},
        },
    )
    state, _ = manager.prepare_state(
        GraphState(
            graph=GraphSpace(
                nodes=(*physical, value, concept),
                edges=(
                    GraphEdge(
                        id="edge.historical",
                        edge_type="grounded_on",
                        from_node_id=concept.id,
                        to_node_id=value.id,
                        properties={
                            "description": "This category is identified by the historical value."
                        },
                    ),
                ),
            ),
            node_conflicts=(),
        ),
        strict=True,
    )
    result = asyncio.run(
        service(tmp_path, Recall()).run(question="amount", graph_state=state)
    )
    assert value.id in {node.id for node in result.subgraph.nodes}
    assert value.id not in {
        item.document.canonical_node_id for item in result.representations
    }
    assert value.id not in result.relevant_grounding_node_ids


def test_same_run_has_only_one_active_writer(tmp_path, source):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowRecall(Recall):
            async def recall(self, *args):
                entered.set()
                await release.wait()
                return await super().recall(*args)

        retrieval = service(tmp_path, SlowRecall())
        first = asyncio.create_task(
            retrieval.run(question="amount", graph_state=source, run_id="locked")
        )
        await entered.wait()
        try:
            with pytest.raises(ValueError, match="active writer"):
                await retrieval.run(
                    question="amount", graph_state=source, run_id="locked"
                )
        finally:
            release.set()
        assert (await first).status == "completed"

    asyncio.run(run())
