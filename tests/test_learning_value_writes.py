"""Value convenience inputs retain verified physical identity."""
import pytest

from cortex.capabilities.sql_training.graph.values import (
    ValueWrite,
    value_asset,
    verify_value,
)


def test_value_input_allocates_shared_identity_and_verifies_database(case_graph):
    store, database, _ = case_graph
    value = value_asset(ValueWrite(column_id="column.orders.region", value="North", aliases=["Northern"]), store.working.assets)
    assert value.id == "dimension_value.orders.region.North"
    assert value.properties.references == ["column.orders.region"]
    assert value.aliases == ["Northern"]
    verify_value(value, store.working.assets, database)


@pytest.mark.parametrize("literal", ["Missing", "north"])
def test_value_must_match_exact_stored_value(case_graph, literal):
    store, database, _ = case_graph
    value = value_asset(ValueWrite(column_id="column.orders.region", value=literal), store.working.assets)
    with pytest.raises(ValueError, match="exactly one typed"):
        verify_value(value, store.working.assets, database)


def test_value_input_cannot_guess_identity_or_owner(case_graph):
    store, database, _ = case_graph
    with pytest.raises(ValueError, match="existing Column"):
        value_asset(ValueWrite(column_id="table.orders", value="North"), store.working.assets)
    value = value_asset(ValueWrite(column_id="column.orders.region", value="North"), store.working.assets)
    with pytest.raises(ValueError, match="canonical"):
        verify_value(value.model_copy(update={"id":"dimension_value.guessed"}), store.working.assets, database)


def test_learning_value_tool_and_snapshot_roundtrip(case_graph):
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.graph.snapshots import GraphStore
    from cortex.capabilities.sql_training.learning.tools import build_tools
    store, _, workspace = case_graph
    session = session_for(case_graph)
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    result = edit.invoke({"edit_id":"value", "expected_revision":store.working_revision,
                          "values":[{"column_id":"column.orders.region","value":"North"}]})
    assert result["status"] == "ok", result
    value_id = result["value_ids"][0]
    assert any(e.edge_type == "value_of" and e.from_node_id == value_id for e in store.working.graph.edges)
    reopened = GraphStore(store.root, workspace=workspace, schema=store.schema)
    reopened.restore(store.working_revision, store.accepted_revision)
    assert value_id in {a.id for a in reopened.working.assets}
    assert value_id not in {a.id for a in reopened.accepted.assets}


def test_missing_value_rejects_whole_learning_batch(case_graph):
    from conftest import make_rule
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools
    store, _, _ = case_graph
    before = store.working_revision
    edit = next(t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph")
    result = edit.invoke({"edit_id":"bad-value", "expected_revision":before,
                          "values":[{"column_id":"column.orders.region","value":"Missing"}],
                          "upserts":[make_rule().model_dump(mode="json")]})
    assert result["status"] == "rejected"
    assert store.working_revision == before
    assert not store.working.semantic_assets


def test_new_value_is_assessed_and_coupled_to_dependent_rule(case_graph):
    from conftest import make_rule
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.graph.mutations import (
        ChangeGroup,
        normalize_groups,
    )
    from cortex.capabilities.sql_training.graph.snapshots import graph_diff
    from cortex.capabilities.sql_training.learning.tools import build_tools
    store, _, _ = case_graph
    baseline = store.accepted
    edit = next(t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph")
    result = edit.invoke({"edit_id":"value", "expected_revision":store.working_revision,
                          "values":[{"column_id":"column.orders.region","value":"North"}]})
    value_id = result["value_ids"][0]
    rule = make_rule()
    relation_id = "edge.rule_region"
    relation = {"id": relation_id, "edge_type": "grounded_on", "from_node_id": rule.id, "to_node_id": value_id, "properties": {"description": "This rule uses the verified regional category."}}
    assert edit.invoke({"edit_id":"rule", "expected_revision":store.working_revision,
                        "upserts":[rule.model_dump(mode="json")], "edge_operations": [{"operation": "create_edge", "edge": relation}]})["status"] == "ok"
    assert set(graph_diff(baseline,store.working)) == {value_id,rule.id,relation_id}
    groups=normalize_groups([ChangeGroup(group_id="v",member_ids=[value_id]),
                             ChangeGroup(group_id="r",member_ids=[rule.id])],baseline,store.working)
    assert len(groups)==1 and set(groups[0].member_ids)=={value_id,rule.id,relation_id}
    with pytest.raises(ValueError,match="unaccepted"):
        store.select(assessed_revision=store.working_revision,accepted_before=baseline.revision,member_ids=[rule.id])
    store.retain()
    assert value_id not in {a.id for a in store.working.assets}


def test_value_diff_exposes_complete_before_and_after(case_graph):
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools
    session = session_for(case_graph)
    tools = {tool.name: tool for tool in build_tools(session)}
    result = tools["edit_graph"].invoke({
        "edit_id": "value-diff", "expected_revision": session.graph.working_revision,
        "values": [{"column_id": "column.orders.region", "value": "North"}],
    })
    value_id = result["value_ids"][0]
    diff = tools["inspect_graph"].invoke({"mode": "diff"})["result"]
    entry = next(item for item in diff if item["id"] == value_id)
    assert entry["before"] is None
    assert entry["after"]["properties"]["value"] == "North"
    assert entry["after"]["node_type"] == "dimension_value"


def test_learning_agent_can_deliver_value_and_explicit_relation(case_graph):
    import json

    from test_agent_runtime import ScriptModel, call, finish, message, session_for
    from test_explicit_relations import concept

    from cortex.capabilities.sql_training.learning.runtime import LearningAgent

    session = session_for(case_graph)
    value_ids = []

    def learn_interfaces(messages):
        assert "Value" in messages[0].content
        assert "edge_operations" in messages[0].content
        assert "operation registry" in messages[0].content
        return message(
            call("inspect_graph", {"mode": "guide", "node_type": "dimension_value"}, "value-guide"),
            call("inspect_graph", {"mode": "relations", "edge_type": "has_region_context"}, "edge-guide"),
            call("inspect_graph", {"mode": "guide", "node_type": "generation_rule"}, "rule-guide"),
        )

    def propose_value(messages):
        results = {m.tool_call_id: json.loads(m.content) for m in messages if m.type == "tool"}
        assert "value_schema" in results["value-guide"]
        assert "count_aggregation" in results["rule-guide"]["operation_registry"]
        assert "edge_operation_schema" in results["edge-guide"]
        return message(call("edit_graph", {
            "edit_id": "value", "expected_revision": session.graph.working_revision,
            "values": [{"column_id": "column.orders.region", "value": "North"}],
        }, "value-write"))

    def connect(messages):
        result = json.loads(messages[-1].content)
        value_ids.extend(result["value_ids"])
        return message(call("edit_graph", {
            "edit_id": "node-and-edge", "expected_revision": result["working_revision"],
            "upserts": [concept("amount")],
            "relation_contracts": [{"id": "has_region_context", "name": "Has region context",
                "description": "A Concept has an evidenced geographical context supplied by a governed region value.",
                "from_node_types": ["concept"], "to_node_types": ["dimension_value"],
                "direction_semantics": "Business concept toward its region context.",
                "evidence_requirement": "The region is verified and the geographical association is supported."}],
            "edge_operations": [{"operation": "create_edge", "edge": {
                "id": "edge.amount_region", "edge_type": "has_region_context",
                "from_node_id": "concept.amount", "to_node_id": value_ids[0],
                "properties": {"description": "The verified region supplies geographical context for this amount concept."},
            }}],
        }, "joint-write"))

    def deliver(messages):
        assert json.loads(messages[-1].content)["status"] == "ok", messages[-1].content
        return finish()

    result = LearningAgent(ScriptModel([learn_interfaces, propose_value, connect, deliver])).run(session)
    assert result.direct_completed
    assert value_ids[0] in {a.id for a in session.graph.working.editable_assets}
    assert session.graph.working.explicit_edges[0].id == "edge.amount_region"
    assert not session.graph.accepted.explicit_edges
    assert value_ids[0] not in {a.id for a in session.graph.accepted.assets}


def test_native_tool_schema_exposes_values_and_explicit_edge_writes(case_graph):
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools

    edit = next(t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph")
    parameters = convert_to_openai_tool(edit)["function"]["parameters"]["properties"]
    value = parameters["values"]["anyOf"][0]["items"]
    assert set(value["required"]) == {"column_id", "value"}
    edge = parameters["edge_operations"]["anyOf"][0]["items"]
    assert set(edge["properties"]["operation"]["enum"]) == {"create_edge", "update_edge", "delete_edge"}
    assert set(edge["properties"]["edge"]["properties"]) == {"id", "edge_type", "from_node_id", "to_node_id", "properties"}


def test_agent_reads_registry_and_refs_mapping_then_refines_value_relation(case_graph):
    import json

    from test_agent_runtime import ScriptModel, call, finish, message, session_for

    from cortex.capabilities.sql_training.learning.runtime import LearningAgent
    from cortex.capabilities.sql_training.learning.tools import build_tools

    session = session_for(case_graph)
    session.record["question"] = "How many orders are in North?"
    session.record["sql"] = "SELECT COUNT(*) FROM orders WHERE region = 'North'"
    value_ids = []
    registry_intents = []

    def discover(messages):
        assert "operation registry" in messages[0].content and "GenerationRule" in messages[0].content
        return message(
            call("inspect_graph", {"mode": "guide", "node_type": "generation_rule"}, "rule-guide"),
            call("inspect_graph", {"mode": "guide", "node_type": "dimension_value"}, "value-guide"),
            call("inspect_graph", {"mode": "relations", "edge_type": "uses_region"}, "relation-guide"),
        )

    def write_value(messages):
        responses = {m.tool_call_id: json.loads(m.content) for m in messages if m.type == "tool"}
        guide = responses["rule-guide"]
        registry_intents.extend(guide["operation_registry"])
        assert guide["reference_relations"] == []
        assert "applies_to" in guide["explicit_relation_types"]
        assert "edge_operations" in guide["relation_writes"]
        assert responses["value-guide"]["reference_relations"][0]["type"] == "value_of"
        return message(call("edit_graph", {"edit_id": "north", "expected_revision": session.graph.working_revision, "values": [{"column_id": "column.orders.region", "value": "North"}]}))

    def write_rule_and_override(messages):
        result = json.loads(messages[-1].content)
        value_ids.extend(result["value_ids"])
        assert "count_aggregation" in registry_intents
        return message(call("edit_graph", {
            "edit_id": "regional-count", "expected_revision": result["working_revision"],
            "relation_contracts": [{"id": "uses_region", "name": "Uses region",
                "description": "A GenerationRule uses a governed regional category within its stated applicability condition.",
                "from_node_types": ["generation_rule"], "to_node_types": ["dimension_value"],
                "direction_semantics": "Generation behavior toward the regional input it uses.",
                "evidence_requirement": "The rule condition and action explicitly use the verified regional category."}],
            "upserts": [{"id": "generation_rule.regional_count", "node_type": "generation_rule", "name": "count orders in a governed region", "properties": {
                "operation_intents": ["count_aggregation"],
                "condition": "The request asks how many orders belong to the governed region.",
                "action": "Apply the governed region selection, then count order rows with COUNT(*).",
                "reason": "Counting all orders without the region selection includes orders from other regions.",
                "triggers": {"question": {"phrases": ["How many orders"]}, "sql": {"columns": [], "operations": []}},
            }}],
            "edge_operations": [{"operation": "create_edge", "edge": {"id": "edge.regional_count", "edge_type": "uses_region", "from_node_id": "generation_rule.regional_count", "to_node_id": value_ids[0], "properties": {"description": "The verified North category supplies the governed region selection."}}}, {"operation": "create_edge", "edge": {"id": "edge.regional_count_source", "edge_type": "grounded_on", "from_node_id": "generation_rule.regional_count", "to_node_id": "table.orders", "properties": {"description": "Order records are the rows counted by this behavior."}}}],
        }))

    def deliver(messages):
        assert json.loads(messages[-1].content)["status"] == "ok"
        return finish()

    result = LearningAgent(ScriptModel([discover, write_value, write_rule_and_override, deliver])).run(session)
    assert result.direct_completed
    edges = session.graph.manager().get_edges("generation_rule.regional_count")
    value_edges = [e for e in edges if e.to_node_id == value_ids[0]]
    assert [(e.edge_type, e.reference_origin) for e in value_edges] == [("uses_region", None)]
    # Every relation is authored; deleting a business edge never restores a default.
    assert any(e.edge_type == "grounded_on" and e.to_node_id == "table.orders" for e in edges)
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    deleted = edit.invoke({"edit_id": "restore-default", "expected_revision": session.graph.working_revision, "edge_operations": [{"operation": "delete_edge", "edge_id": "edge.regional_count"}]})
    assert deleted["status"] == "ok"
    edges = session.graph.manager().get_edges("generation_rule.regional_count")
    assert [(e.edge_type, e.reference_origin) for e in edges if e.to_node_id == value_ids[0]] == []


@pytest.mark.parametrize("exists", [True, False])
def test_value_guide_existing_lookup_path_reuses_or_creates_as_needed(case_graph, exists):
    import json

    from conftest import make_rule
    from test_agent_runtime import ScriptModel, call, finish, message, session_for

    from cortex.capabilities.sql_training.learning.runtime import LearningAgent
    from cortex.capabilities.sql_training.learning.tools import build_tools

    session = session_for(case_graph)
    session.record["question"] = "How many orders are in North?"
    session.record["sql"] = "SELECT COUNT(*) FROM orders WHERE region = 'North'"
    if exists:
        edit = next(t for t in build_tools(session) if t.name == "edit_graph")
        assert edit.invoke({"edit_id": "seed-value", "expected_revision": session.graph.working_revision, "values": [{"column_id": "column.orders.region", "value": "North"}]})["status"] == "ok"
    database_calls_before = session.database_calls
    used_ids = []

    def start(messages):
        return message(
            call("inspect_graph", {"mode": "guide", "node_type": "dimension_value"}, "value-guide"),
            call("inspect_graph", {"mode": "guide", "node_type": "generation_rule"}, "rule-guide"),
        )

    def inspect_owner(messages):
        guide = next(json.loads(m.content) for m in messages if m.type == "tool" and m.tool_call_id == "value-guide")
        assert "Look up and reuse first" in guide["guidance"]
        assert "incoming and outgoing relations" in guide["guidance"]
        return message(call("inspect_graph", {"mode": "node", "node_id": "column.orders.region"}, "owner"))

    def inspect_or_create(messages):
        column = json.loads(messages[-1].content)["result"]
        candidates = [e["from_node_id"] for e in column["relations"] if e["to_node_id"] == column["id"] and e["from_node_id"].startswith("dimension_value.")]
        assert session.database_calls == database_calls_before
        if exists:
            assert len(candidates) == 1
            return message(call("inspect_graph", {"mode": "node", "node_id": candidates[0]}, "candidate"))
        assert candidates == []
        return message(call("edit_graph", {"edit_id": "missing-value", "expected_revision": session.graph.working_revision, "values": [{"column_id": column["id"], "value": "North"}]}, "new-value"))

    def bind(messages):
        response = json.loads(messages[-1].content)
        if exists:
            value = response["result"]
            assert value["node_type"] == "dimension_value"
            assert value["properties"]["references"] == ["column.orders.region"]
            assert type(value["properties"]["value"]) is str and value["properties"]["value"] == "North"
            used_ids.append(value["id"])
        else:
            assert response["status"] == "ok"
            used_ids.extend(response["value_ids"])
        return message(call("edit_graph", {"edit_id": "bind-existing-or-new", "expected_revision": session.graph.working_revision, "upserts": [make_rule(references=used_ids).model_dump(mode="json")]}))

    result = LearningAgent(ScriptModel([start, inspect_owner, inspect_or_create, bind, finish()])).run(session)
    assert result.direct_completed
    values = [a for a in session.graph.working.assets if a.node_type.value == "dimension_value"]
    assert len(values) == 1 and values[0].id == used_ids[0]
    assert session.database_calls == database_calls_before + (0 if exists else 1)


def test_verified_numeric_value_binding_survives_concept_normalization(case_graph):
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools
    session = session_for(case_graph)
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    created = edit.invoke({"edit_id": "verified-numeric", "expected_revision": session.graph.working_revision, "values": [{"column_id": "column.orders.amount", "value": 10.0}]})
    assert created["status"] == "ok", created
    value_id = created["value_ids"][0]
    concept = {"id": "concept.ten_unit_order", "node_type": "concept", "name": "ten-unit order", "properties": {"targets": [[value_id]], "target_expressions": {"0": "orders.amount = 10.0"}}}
    result = edit.invoke({"edit_id": "bind-numeric", "expected_revision": session.graph.working_revision, "upserts": [concept], "edge_operations": [{"operation": "create_edge", "edge": {"id": "edge.ten_unit_value", "edge_type": "grounded_on", "from_node_id": concept["id"], "to_node_id": value_id, "properties": {"description": "The exact verified amount identifies ten-unit order records."}}}]})
    assert result["status"] == "ok", result
    saved = next(a for a in session.graph.working.semantic_assets if a.id == concept["id"])
    assert value_id in saved.properties.targets[0]
    assert any(e.to_node_id == value_id and e.edge_type == "grounded_on" for e in session.graph.manager().get_edges(concept["id"]))
    assert edit.invoke({"edit_id": "replay-binding", "expected_revision": session.graph.working_revision, "upserts": [concept]})["status"] == "ok"
    assert value_id in session.graph.working.semantic_assets[0].properties.targets[0]
