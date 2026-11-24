"""Targeted reads preserve complete objects, exact scope and prompt information."""

from test_graph_integration import catalog, concept, contexts, edge, state

from cortex.capabilities.graph_merge.identity.catalog import select_catalog_nodes
from cortex.capabilities.graph_merge.graph.publication import compile_proposal
from cortex.capabilities.graph_merge.graph.edit_contract import (
    IdentityDecision,
    IntegrationProposal,
)
from cortex.capabilities.graph_merge.llm.packing import (
    expand_payload,
    pack_payload,
    prompt_json,
)
from cortex.capabilities.graph_merge.identity.tools import build_tools
from cortex.schema import GraphNode


def test_catalog_lookup_returns_complete_column_and_explicit_missing_names():
    ctx = contexts(state(), state([*catalog(), concept("asset", "a")]))
    inspect = build_tools(ctx)[0]
    result = inspect.invoke(
        {"mode": "nodes", "table": "objects", "column_names": ["a", "absent"]}
    )
    key = ctx.input_node_map["a"]
    assert result["items"] == [ctx.nodes[key].model_dump(mode="json")]
    assert result["missing_columns"] == ["absent"]
    assert result["total"] == 1 and result["next_offset"] is None
    assert result["table_id"] == ctx.input_node_map["table"]
    assert ctx.tool_log[-1]["args"]["column_names"] == ["a", "absent"]
    assert (
        inspect.invoke({"mode": "nodes", "table": "objects"})["items"][0]["node_type"]
        == "table"
    )
    # These strings are exact catalogue labels, never interpreted as SQL.
    odd = inspect.invoke(
        {"mode": "nodes", "table": "objects", "column_names": ["a; DROP TABLE objects"]}
    )
    assert odd["items"] == [] and odd["missing_columns"] == ["a; DROP TABLE objects"]
    assert inspect.invoke({"mode": "nodes", "ids": [key], "table": "objects"})["error"]
    assert inspect.invoke({"mode": "nodes", "column_names": ["a"]})["error"]
    assert inspect.invoke({"mode": "nodes", "table": "objects", "column_names": []})[
        "error"
    ]


def test_catalog_name_ambiguity_requires_id_and_does_not_pick_one_table():
    tables = [
        GraphNode(
            id=k, name="shared", node_type="table", properties={"table_name": "shared"}
        )
        for k in ["left", "right"]
    ]
    fields = [
        GraphNode(
            id=k + ".field",
            name="field",
            node_type="column",
            properties={"table_ref": k, "column_name": "field"},
        )
        for k in ["left", "right"]
    ]
    nodes = {n.id: n for n in [*tables, *fields]}
    selected, info = select_catalog_nodes(nodes, "shared", ["field"])
    assert selected == [] and info["matching_table_ids"] == ["left", "right"]
    selected, info = select_catalog_nodes(nodes, "right", ["field"])
    assert [n.id for n in selected] == ["right.field"]
    assert info["missing_columns"] == []


def test_catalog_lookup_respects_candidate_view_and_pagination():
    ctx = contexts(state(), state(catalog()))
    preview = compile_proposal(ctx, IdentityDecision(), IntegrationProposal())
    # Tool-view isolation fixture; no production physical mutation is allowed.
    ctx.candidate = preview.state.model_copy(
        update={
            "graph": preview.state.graph.model_copy(
                update={
                    "nodes": tuple(
                        n
                        for n in preview.state.graph.nodes
                        if n.id != ctx.input_node_map["a"]
                    )
                }
            )
        }
    )
    inspect = build_tools(ctx)[0]
    assert inspect.invoke(
        {
            "mode": "nodes",
            "table": "objects",
            "column_names": ["a"],
            "view": "candidate",
        }
    )["missing_columns"] == ["a"]
    first = inspect.invoke(
        {
            "mode": "nodes",
            "table": "objects",
            "column_names": ["a", "b", "c"],
            "limit": 1,
        }
    )
    second = inspect.invoke(
        {
            "mode": "nodes",
            "table": "objects",
            "column_names": ["a", "b", "c"],
            "limit": 2,
            "offset": first["next_offset"],
        }
    )
    assert len(first["items"] + second["items"]) == 3 and second["next_offset"] is None
    assert first["missing_columns"] == []


def test_relation_type_filter_precedes_pagination_and_keeps_unfiltered_access():
    ctx = contexts(
        state(),
        state(
            [*catalog(), concept("A", "a"), concept("B", "b")],
            [edge("g", "A", "a"), edge("business", "A", "B", "has_attribute")],
        ),
    )
    inspect = build_tools(ctx)[0]
    key = ctx.input_node_map["A"]
    result = inspect.invoke(
        {"mode": "relations", "ids": [key], "edge_types": ["has_attribute"], "limit": 1}
    )
    assert len(result["items"]) == result["total"] == 1
    assert result["items"][0]["edge_type"] == "has_attribute"
    assert result["next_offset"] is None
    assert inspect.invoke({"mode": "relations", "ids": [key]})["total"] == 2
    assert (
        inspect.invoke({"mode": "relations", "ids": [key], "edge_types": ["unknown"]})[
            "total"
        ]
        == 0
    )
    assert inspect.invoke(
        {"mode": "nodes", "ids": [key], "edge_types": ["has_attribute"]}
    )["error"]


def test_contract_sharing_is_lossless_and_keeps_different_versions_explicit():
    original = {"id": "role", "description": "original", "fields": {}}
    changed = {**original, "description": "new scope"}
    p = {
        "local_contracts": [original],
        "active_contracts": [
            {"contract": original, "used_by": ["edge1"]},
            {"contract": changed, "used_by": ["edge2"]},
        ],
    }
    packed = pack_payload(p)
    assert packed["active_contracts"][0] == {
        "contract_ref": "role",
        "used_by": ["edge1"],
    }
    assert packed["active_contracts"][1]["contract"] == changed
    assert expand_payload(packed) == p
    assert len(prompt_json(packed)) < len(prompt_json(p))
    assert p["active_contracts"][0]["contract"] == original


def test_review_rule_is_complete_without_duplicating_reason_in_a_new_field():
    rule = GraphNode(
        id="rule",
        node_type="generation_rule",
        name="Atomic selection",
        properties={
            "condition": "Select a requested leading record",
            "action": "Order by the requested key and limit",
            "reason": "The supplied task specifies the leading record",
        },
    ).model_dump(mode="json")
    p = {"nodes": [rule], "candidate_nodes": [rule], "rationale_review_ids": ["rule"]}
    packed = pack_payload(p)
    assert packed["candidate_nodes"] == [rule]
    assert expand_payload(packed) == p
    # Other nodes still use existing lossless sharing.
    p["rationale_review_ids"] = []
    assert pack_payload(p)["candidate_nodes"] == [{"input_ref": "rule"}]
