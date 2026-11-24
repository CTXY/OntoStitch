from cortex.capabilities.graph_merge.llm.packing import bounded_items
from test_graph_integration_local_context import event, evidence
from graph_merge_fixtures import catalog, concept, edge, state, contexts
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.identity.tools import build_tools
from cortex.schema import load_root_schema
"""Controlled cost reductions must retain graph content, provenance and review scope."""


def test_preloaded_evidence_is_local_and_large_items_remain_readable():
    incoming = state([event("local")])
    huge = evidence("local").model_copy(update={"content": {"sql": "x" * 13000}})
    ctx = IntegrationContext(
        state([event("remote")]),
        incoming,
        load_root_schema(),
        evidence=[huge, evidence("local")],
        top_k=1,
    )
    payload = ctx.payload("propose")
    assert payload["evidence"] == [ctx.evidence_rows()[1]]
    full = {tool.name: tool for tool in build_tools(ctx)}['read_evidence'].invoke({"ids": ["local"]})
    assert len(full["items"]) == 2
    assert bounded_items([{"x": "x" * 100}, {"x": "small"}], budget=30) == [
        {"x": "small"}
    ]


def test_direct_binding_facts_are_preloaded_without_entire_catalog():
    ctx = contexts(state(), state([*catalog(), concept("A", "a")]))
    payload = ctx.payload("propose")
    assert {n["id"] for n in payload["physical_facts"]} == {ctx.input_node_map["a"]}
    assert build_tools(ctx)[0].invoke(
        {"mode": "nodes", "ids": [ctx.input_node_map["b"]]}
    )["items"]


def test_batched_candidates_equal_independent_queries():
    ctx = contexts(state(), state([*catalog(), concept("A", "a"), concept("B", "b")]))
    ids = [ctx.input_node_map[x] for x in ("A", "B")]
    inspect = build_tools(ctx, visible_source_ids=ctx.read_ids)[0]
    singles = {key: inspect.invoke({"mode": "candidates", "ids": [key]}) for key in ids}
    assert inspect.invoke({"mode": "candidates", "ids": ids}) == {"pages": singles}


def test_size_pagination_preserves_every_edge_and_batches_small_results():
    nodes = [
        *catalog(),
        concept("root", "a"),
        *[concept(f"c{i}", "b") for i in range(30)],
    ]
    edges = [edge(f"e{i}", "root", f"c{i}", "has_attribute") for i in range(30)]
    ctx = contexts(state(), state(nodes, edges))
    inspect = build_tools(ctx)[0]
    assert (
        len(
            inspect.invoke({"mode": "relations", "ids": [ctx.input_node_map["root"]]})[
                "items"
            ]
        )
        == 30
    )
    edges = [
        e.model_copy(update={"properties": {"description": "Evidence " * 600}})
        for e in edges
    ]
    ctx = contexts(state(), state(nodes, edges))
    inspect = build_tools(ctx)[0]
    offset, found = 0, []
    while offset is not None:
        page = inspect.invoke(
            {"mode": "relations", "ids": [ctx.input_node_map["root"]], "offset": offset}
        )
        found.extend(e["id"] for e in page["items"])
        offset = page["next_offset"]
    assert len(found) == len(set(found)) == 30
