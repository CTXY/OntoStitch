"""Small startup contract and complete, type-scoped progressive disclosure."""

import json
import re

import pytest
from test_agent_runtime import (
    ScriptModel,
    call,
    finish,
    message,
    read_case_context,
    session_for,
)

from cortex.capabilities.sql_training.learning.inputs import (
    AUTHORING_MODELS,
    PROPERTY_MODELS,
    ontology_contract,
    system_prompt,
)
from cortex.capabilities.sql_training.learning.prompt_rendering import (
    compact_authoring_schema,
    render_type_guide,
)
from cortex.capabilities.sql_training.learning.runtime import LearningAgent
from cortex.capabilities.sql_training.learning.tools import build_tools
from cortex.capabilities.sql_training.models import NodeAsset


def node_example(guide):
    return json.loads(re.search(r"```json\n(.*?)\n```", guide["guidance"], re.DOTALL)[1])


def test_guide_contract_uses_runtime_schema_and_output_models(case_graph):
    session = session_for(case_graph)
    contract = ontology_contract(session.graph.schema)
    for kind, model in AUTHORING_MODELS.items():
        guide = render_type_guide(contract, kind)
        assert guide["properties_schema"] == compact_authoring_schema(model.model_json_schema())
        assert guide["asset_envelope"] == compact_authoring_schema(contract["output_schema"]["asset_envelope"])
        assert guide["reference_relations"] == []
        assert "explicit_relation_types" in guide
        assert "description" in guide["relation_writes"]
        assert "direct_rule_registry" not in guide
        assert ("operation_registry" in guide) == (kind == "generation_rule")
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    schema = edit.args_schema.model_json_schema()["properties"]["upserts"]
    assert "inspect_graph(mode='guide'" in json.dumps(schema)


def test_count_star_has_table_binding_without_browsing_columns(case_graph):
    session = session_for(case_graph)
    context = session.context()
    assert context["gold_bindings"]["tables"] == [{"id": "table.orders", "table_name": "orders"}]
    assert context["database_dialect"] == session.dialect
    assert context["gold_bindings"]["columns"] == []
    assert "not the complete catalog" in context["physical_input_scope"]
    assert "graph_info" not in context
    assert session.database_calls == session.tool_calls == 0


def test_gold_bindings_resolve_aliases_ctes_and_exact_catalog_ids(case_graph):
    session = session_for(case_graph)
    session.record["sql"] = "WITH q AS (SELECT o.amount AS total FROM orders AS o) SELECT q.total FROM q"
    bindings = session.context()["gold_bindings"]
    assert bindings["tables"] == [{"id": "table.orders", "table_name": "orders"}]
    assert [c["id"] for c in bindings["columns"]] == ["column.orders.amount"]
    assert bindings["columns"][0]["column_name"] == "amount"
    assert session.database_calls == 0


@pytest.mark.parametrize("kind", PROPERTY_MODELS)
def test_each_guide_example_is_a_complete_valid_edit(case_graph, kind):
    session = session_for(case_graph)
    tools = {t.name: t for t in build_tools(session)}
    guide = tools["inspect_graph"].invoke({"mode": "guide", "node_type": kind})
    assert guide.get("status") != "truncated"
    assert "Complete node example" in guide["guidance"]
    example = node_example(guide)
    assert NodeAsset.model_validate(example).node_type.value == kind
    if kind == "generation_rule":
        session.record["question"] = "Return the leading groups."
    prerequisites = []
    if kind == "metric":
        prerequisites = [
            {"id": "concept.net_profit", "node_type": "concept", "name": "net profit",
             "description": "Stored net profit in this illustrative fixture.",
             "properties": {"targets": [["column.orders.amount"]]}},
            {"id": "concept.revenue", "node_type": "concept", "name": "revenue",
             "description": "Stored revenue in this illustrative fixture.",
             "properties": {"targets": [["column.orders.id"]]}},
        ]
    result = tools["edit_graph"].invoke({
        "edit_id": "example", "expected_revision": session.graph.working_revision,
        "upserts": [*prerequisites, example],
        "edge_operations": (json.loads(re.findall(r"```json\n(.*?)\n```", guide["guidance"], re.DOTALL)[1]) if kind == "metric" else []),
    })
    assert result["status"] == "ok", json.dumps(result)
    assert {a.id for a in session.graph.working.semantic_assets} == {example["id"], *(a["id"] for a in prerequisites)}
    assert not session.graph.accepted.semantic_assets


def test_agent_gets_only_the_needed_guide_before_writing(case_graph):
    session = session_for(case_graph)
    session.record["question"] = "Return the leading groups."

    def choose(messages):
        system = messages[0].content
        assert system.startswith("# Task background")
        assert "**Event:**" in system and "no type or node-count quota" in system
        assert len(system) < 9000
        assert "MetricExpression" not in system
        assert "ordinary join-fact Rules" in system
        context = read_case_context(messages)
        assert context["gold_bindings"]["tables"][0]["id"] == "table.orders"
        return message(call("inspect_graph", {"mode": "guide", "node_type": "generation_rule"}))

    def write(messages):
        guide = json.loads(messages[-1].content)
        assert "properties_schema" in guide and "guidance" in guide
        assert "MetricExpression" not in json.dumps(guide)
        return message(call("edit_graph", {
            "edit_id": "count", "expected_revision": session.graph.working_revision,
            "upserts": [node_example(guide)],
        }))

    result = LearningAgent(ScriptModel([choose, write, finish()])).run(session)
    assert result.direct_completed
    assert result.database_calls == 0 and result.tool_calls == 3
    assert not session.graph.accepted.semantic_assets


def test_startup_has_unambiguous_delivery_and_no_full_contract_dump(case_graph):
    session = session_for(case_graph)
    session.record["question"] = "INPUT_CONTEXT_SENTINEL"
    system = system_prompt("direct", session.graph.schema)
    assert "INPUT_CONTEXT_SENTINEL" not in system
    assert "INPUT_CONTEXT_SENTINEL" in json.dumps(session.context())
    headings = ["# Task background", "# Shared ontology contract", "# Workflow and tools", "# Direct extraction", "# Delivery"]
    assert [system.index(h) for h in headings] == sorted(system.index(h) for h in headings)
    assert "case-level ontology knowledge" in system
    assert "**Event:**" in system and "Tables, Columns and governed Values" in system
    assert "no type or node-count quota" in system
    assert "edge_operations" in system
    assert "{stage_task}" not in system
    assert "```json" not in system
    assert "conflict_operations" not in system
    assert "edit_graph" in system and "handoff" in system
    # Parameter structure is available in the independently bound native tools.
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    assert {"upserts", "values", "edge_operations"} <= edit.args_schema.model_json_schema()["properties"].keys()
    for detail in ("MetricExpression", "SemanticMappingTarget", "is_primary_key", "count_aggregation"):
        assert detail not in system


def test_mandatory_inputs_still_count_toward_context_budget(case_graph):
    session = session_for(case_graph)
    system = system_prompt(session.mode, session.graph.schema)
    session.config = session.config.model_copy(update={"agent_context_characters": len(system)})
    with pytest.raises(ValueError, match="essential case source"):
        LearningAgent(ScriptModel([])).run(session)


def test_type_guide_change_invalidates_resume_context(case_graph, monkeypatch):
    from test_case_workflow import EditingAgent, Validator, assessment, make_workflow

    from cortex.capabilities.sql_training.training import services
    _, before, _ = make_workflow(case_graph, EditingAgent([]), Validator(), assessment)
    original = services.render_type_guide
    def changed(contract, kind):
        result = original(contract, kind)
        if kind == "metric":
            result["guidance"] += "\nChanged guide assembly."
        return result
    monkeypatch.setattr(services, "render_type_guide", changed)
    _, after, _ = make_workflow(case_graph, EditingAgent([]), Validator(), assessment)
    assert before.context_id != after.context_id


def test_trimmed_type_guide_can_be_loaded_again_before_editing(case_graph):
    from langchain_core.messages import HumanMessage, ToolMessage, messages_to_dict
    session = session_for(case_graph)
    session.record["question"] = "Return the leading groups."
    context = HumanMessage(content="<case_context>\n" + json.dumps(session.context(), ensure_ascii=False) + "\n</case_context>")
    tools_size = len(json.dumps([t.args_schema.model_json_schema() for t in build_tools(session)]))
    guide = build_tools(session)[0].invoke({"mode": "guide", "node_type": "generation_rule"})
    guide_size = len(json.dumps(messages_to_dict([ToolMessage(content=json.dumps(guide, ensure_ascii=False), tool_call_id="new-guide")])))
    session.config = session.config.model_copy(update={
        "agent_context_characters": len(system_prompt("direct", session.graph.schema))
        + len(json.dumps(messages_to_dict([context]))) + tools_size + guide_size + 2200,
    })
    first = message(call("inspect_graph", {"mode": "guide", "node_type": "generation_rule"}, "old-guide"))
    noise = message(call("inspect_graph", {"mode": "diff"}, "later-read"))
    noise.content = "Later investigation: " + "x" * 5500

    def reload(messages):
        assert not any(isinstance(m, ToolMessage) and m.tool_call_id == "old-guide" for m in messages)
        assert read_case_context(messages)["database_dialect"] == session.dialect
        return message(call("inspect_graph", {"mode": "guide", "node_type": "generation_rule"}, "new-guide"))

    def write(messages):
        guide = json.loads(next(m.content for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "new-guide"))
        return message(call("edit_graph", {"edit_id": "after-trim", "expected_revision": session.graph.working_revision, "upserts": [node_example(guide)]}))

    result = LearningAgent(ScriptModel([first, noise, reload, write, finish()])).run(session)
    assert result.direct_completed
    assert session.graph.working.semantic_assets[0].id == "generation_rule.select_ranked_groups"


def test_direct_invented_trigger_rejects_entire_batch(case_graph):
    from conftest import make_rule
    session = session_for(case_graph)
    session.record["question"] = "Return the total number of orders."
    before = session.graph.working_revision
    concept = node_example(render_type_guide(ontology_contract(session.graph.schema), "concept"))
    result = next(t for t in build_tools(session) if t.name == "edit_graph").invoke({
        "edit_id": "bad-trigger", "expected_revision": before,
        "upserts": [concept, make_rule().model_dump(mode="json")],
    })
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "QUESTION_TRIGGER_NOT_IN_SOURCE"
    assert result["errors"][0]["path"] == "upserts.1.properties.triggers.question.phrases.0"
    assert session.graph.working_revision == before
    assert not session.graph.working.semantic_assets
    assert not session.edit_receipts


def test_direct_trigger_matching_ignores_only_case_and_whitespace(case_graph):
    from conftest import make_rule
    session = session_for(case_graph)
    session.record["question"] = "HOW\n  MANY orders?"
    result = next(t for t in build_tools(session) if t.name == "edit_graph").invoke({
        "edit_id": "matching-trigger", "expected_revision": session.graph.working_revision,
        "upserts": [make_rule().model_dump(mode="json")],
    })
    assert result["status"] == "ok"


def test_accepted_trigger_is_preserved_but_new_invented_trigger_is_rejected(case_graph):
    from conftest import make_rule, seed_accepted
    session = session_for(case_graph)
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    rule = make_rule().model_dump(mode="json")
    assert edit.invoke({"edit_id": "first", "expected_revision": session.graph.working_revision, "upserts": [rule]})["status"] == "ok"
    seed_accepted(session.graph)
    session.record["question"] = "Return total orders."
    rule["properties"]["action"] = "Count all requested order rows."
    assert edit.invoke({"edit_id": "keep-history", "expected_revision": session.graph.working_revision, "upserts": [rule]})["status"] == "ok"
    before = session.graph.working_revision
    rule["properties"]["triggers"]["question"]["phrases"].append("unseen phrase")
    assert edit.invoke({"edit_id": "invent-new", "expected_revision": before, "upserts": [rule]})["status"] == "rejected"
    assert session.graph.working_revision == before


def test_repair_trigger_policy_is_not_changed(case_graph):
    from conftest import make_rule
    session = session_for(case_graph)
    session.mode = "repair"
    session.record["question"] = "Return total orders."
    result = next(t for t in build_tools(session) if t.name == "edit_graph").invoke({
        "edit_id": "repair-trigger", "expected_revision": session.graph.working_revision,
        "upserts": [make_rule().model_dump(mode="json")],
    })
    assert result["status"] == "ok"


def test_case_context_whitelists_source_and_omits_empty_runtime_fields(case_graph):
    session = session_for(case_graph)
    session.record.update({
        "evidence": "Stored order rows.",
        "gold_column_metadata": {"column.orders.amount": {"sample_values": [10]}},
        "routing_join_paths": ["INTERNAL_ROUTING_SENTINEL"],
        "future_internal_field": "DO_NOT_INJECT",
    })
    context = session.context()
    assert context["source"] == {key: session.record[key] for key in ("question", "sql", "evidence")}
    for key in ("graph_info", "baseline_revision", "targets", "latest_feedback", "working_notes"):
        assert key not in context
    assert "INTERNAL_ROUTING_SENTINEL" not in json.dumps(context)
    assert "DO_NOT_INJECT" not in json.dumps(context)
    # Filtering the model input must not remove internal evidence used by Diagnosis.
    assert "gold_column_metadata" in session.record
    session.notes.current_plan = "Check the stored meaning."
    assert session.context()["working_notes"] == {"current_plan": "Check the stored meaning."}
    session.mode = "repair"
    session.targets = [{"target_id": "error-1", "instruction": "Repair the predicate."}]
    session.feedback = {"assessment": "revise"}
    repaired = session.context()
    assert repaired["targets"] == session.targets
    assert repaired["latest_feedback"] == session.feedback


def test_physical_bindings_keep_one_copy_of_names_type_and_description(case_graph):
    session = session_for(case_graph)
    session.record["sql"] = "SELECT amount FROM orders"
    # Use the saved catalog description directly, without copying internal metadata.
    from cortex.capabilities.sql_training.learning.inputs import graph_context
    snapshot = session.graph.working
    column = next(a for a in snapshot.assets if a.id == "column.orders.amount")
    described = column.model_copy(update={"description": "Stored order amount."})
    snapshot = snapshot.model_copy(update={"assets": [described if a.id == column.id else a for a in snapshot.assets]})
    bindings = graph_context(snapshot, session.record, session.dialect)["gold_bindings"]
    assert bindings["columns"] == [{
        "id": column.id, "table_id": "table.orders", "column_name": "amount",
        "data_type": column.properties.data_type, "description": "Stored order amount.",
    }]


def test_physical_bindings_distinguish_node_ids_from_sql_names(case_graph):
    from cortex.capabilities.sql_training.learning.inputs import graph_context
    session = session_for(case_graph)
    snapshot = session.graph.working
    column = next(a for a in snapshot.assets if a.id == "column.orders.amount")
    renamed = column.model_copy(update={"properties": column.properties.model_copy(update={"column_name": "Total (USD)"})})
    snapshot = snapshot.model_copy(update={"assets": [renamed if a.id == column.id else a for a in snapshot.assets]})
    record = {**session.record, "sql": 'SELECT "Total (USD)" FROM orders'}
    binding = graph_context(snapshot, record, session.dialect)["gold_bindings"]["columns"][0]
    assert binding["id"] == "column.orders.amount"
    assert binding["column_name"] == "Total (USD)"
    prompt = system_prompt("direct", session.graph.schema)
    assert "Copy exact graph IDs into targets, refs and endpoints" in prompt
    assert "SQL uses catalog identifiers" in prompt


def test_semantic_index_is_relevant_bounded_and_preserves_pinned_nodes(case_graph):
    from conftest import make_rule

    from cortex.capabilities.sql_training.learning.inputs import semantic_overview
    session = session_for(case_graph)
    bindings = session.context()["gold_bindings"]
    relevant = [make_rule(name=f"orders_{i:02}", references=["table.orders"]) for i in range(25)]
    unrelated = make_rule(name="unrelated", references=["table.customers"])
    named = make_rule(name="special meaning")
    from cortex.schema import GraphEdge
    discovery_edges = tuple(GraphEdge(
        id=f"edge.discovery_{i}", edge_type="uses_source",
        from_node_id=asset.id, to_node_id="table.orders",
        properties={"description": "The rule uses the orders source."},
    ) for i, asset in enumerate(relevant))
    snapshot = session.graph.working.model_copy(update={
        "assets": [*session.graph.working.assets, *relevant, unrelated, named],
        "graph": session.graph.working.graph.model_copy(update={
            "edges": (*session.graph.working.graph.edges, *discovery_edges),
        }),
    })
    rows = semantic_overview(snapshot, bindings, "How many orders?", set())
    assert len(rows) == 20
    assert {row["id"] for row in rows} == {a.id for a in relevant[:20]}
    assert all(set(row) == {"id", "type", "name"} for row in rows)
    pinned = semantic_overview(snapshot, bindings, "How many orders?", {unrelated.id, relevant[-1].id})
    assert len(pinned) == 22
    assert {unrelated.id, relevant[-1].id} <= {row["id"] for row in pinned}
    assert named.id in {row["id"] for row in semantic_overview(snapshot, bindings, "special meaning", set())}
    assert semantic_overview(snapshot, bindings, "How many orders?", set()) == rows


def test_assessment_response_schema_change_invalidates_resume_context(case_graph, monkeypatch):
    from test_case_workflow import EditingAgent, Validator, assessment, make_workflow

    from cortex.capabilities.sql_training.training.feedback import (
        AssessmentFeedback,
        assessment_policy_id,
    )
    _, before, _ = make_workflow(case_graph, EditingAgent([]), Validator(), assessment)
    old_policy = assessment_policy_id()
    monkeypatch.setattr(AssessmentFeedback, "model_json_schema", classmethod(lambda cls: {"changed_response_contract": True}))
    _, after, _ = make_workflow(case_graph, EditingAgent([]), Validator(), assessment)
    assert old_policy != assessment_policy_id()
    assert before.context_id != after.context_id


def test_atomic_metric_definition_is_shared_by_authoring_and_assessment(case_graph):
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    shared = load_prompt("learning_system").split("# Shared ontology contract", 1)[1].split("# Workflow and tools", 1)[0]
    assert shared in load_prompt("learning_assessment")
    assert "independently meaningful units" in shared
    assert "formula expansion" in shared
    guide = render_type_guide(ontology_contract(case_graph[0].schema), "metric")
    assert "indirect paths do not replace operands" in guide["guidance"]
    assert node_example(guide)["id"] == "metric.profit_margin_percentage"
    assert "a calculated average can be a Metric" not in load_prompt("learning_type_concept")


def test_direct_and_repair_share_registry_and_taxonomy(case_graph):
    direct = session_for(case_graph)
    tools = {t.name: t for t in build_tools(direct)}
    guide = tools["inspect_graph"].invoke({"mode": "guide", "node_type": "generation_rule"})
    assert "direct_rule_registry" not in guide
    assert {"joinability", "column_binding_choice", "aggregation_grain", "temporal_alignment"} <= guide["operation_registry"].keys()
    taxonomy = tools["inspect_graph"].invoke({"mode": "guide", "guide_section": "taxonomy"})
    direct.mode = "repair"
    repair_guide = tools["inspect_graph"].invoke({"mode": "guide", "node_type": "generation_rule"})
    assert repair_guide["operation_registry"] == guide["operation_registry"]
    repair_taxonomy = tools["inspect_graph"].invoke({"mode": "guide", "guide_section": "taxonomy"})
    assert repair_taxonomy == taxonomy
    assert '## `temporal_alignment`' in taxonomy["taxonomy"]


def test_direct_accepts_shared_registry_intents_without_stage_gate(case_graph):
    from conftest import make_rule
    session = session_for(case_graph)
    session.record["question"] = "How many orders?"
    rule = make_rule("join", references=["table.orders"])
    rule.properties.operation_intents = ["joinability"]
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    result = edit.invoke({"edit_id": "direct-join", "expected_revision": session.graph.working_revision,
                         "upserts": [rule.model_dump(mode="json")]})
    assert result["status"] == "ok"


def test_error_driven_column_correction_rule_can_coexist_with_concept(case_graph):
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    session = session_for(case_graph)
    session.mode = "repair"
    concept = {"id": "concept.order_amount", "node_type": "concept", "name": "order amount",
               "properties": {"targets": [["column.orders.amount"]]}}
    rule = {"id": "generation_rule.correct_amount_binding", "node_type": "generation_rule", "name": "use amount rather than id for requested order amount",
            "properties": {"condition": "Order amount is requested but SQL uses the order identifier.",
                           "action": "Use the order amount Concept binding rather than the identifier.",
                           "reason": "The identifier labels an order and does not store its monetary amount.",
                           "triggers": {"sql": {"columns": ["column.orders.id"]}}}}
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    result = edit.invoke({"edit_id": "repair-binding", "expected_revision": session.graph.working_revision, "upserts": [concept, rule]})
    assert result["status"] == "ok", result
    assert {a.id for a in session.graph.working.semantic_assets} == {concept["id"], rule["id"]}
    assert "confirmed error signals" in load_prompt("learning_repair")


def test_repair_has_targeted_investigation_and_visible_unchanged_read_budget(case_graph):
    session = session_for(case_graph)
    session.mode = "repair"
    session.no_progress = 2
    assert session.context()["budget"]["repeated_read_decisions_left"] == 1
    assert not session.exhausted()
    prompt = system_prompt("repair", session.graph.schema)
    assert "fixed Diagnosis targets" in prompt
    assert "material" in prompt and "read-only" in prompt
    session.no_progress = 3
    assert session.exhausted()


def test_concept_definition_is_shared_without_case_specific_owner_exceptions(case_graph):
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    learning = system_prompt("direct", case_graph[0].schema)
    assessment = load_prompt("learning_assessment")
    shared = learning.split("# Shared ontology contract", 1)[1].split("# Workflow and tools", 1)[0]
    assert shared in assessment
    for text in (learning, assessment):
        assert "Stored quantities are Concepts" in text
        assert "never Tables" in text
        assert "display name or full-name group" in text
    guide = render_type_guide(ontology_contract(case_graph[0].schema), "concept")
    assert "Illustration: School" in guide["guidance"]
    assert "Illustration: School" not in assessment
    for obsolete in ("outside that phrase", "Metric is the sole owner", "has no ontology owner", "enrollment", "frpm", "DOCType"):
        assert obsolete not in learning + assessment


def test_concept_expression_normalization_preserves_literals_and_aliases(case_graph):
    session = session_for(case_graph)
    concept = node_example(render_type_guide(ontology_contract(session.graph.schema), "concept"))
    concept["aliases"] = ["kept alias"]
    concept["properties"]["target_expressions"] = {"0": "[orders].[amount]"}
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    result = edit.invoke({"edit_id": "quoted", "expected_revision": session.graph.working_revision, "upserts": [concept]})
    assert result["status"] == "ok", result
    saved = next(a for a in session.graph.working.assets if a.id == concept["id"])
    import sqlglot
    column = sqlglot.parse_one(saved.properties.target_expressions["0"], dialect="sqlite")
    assert column.table == "orders" and column.name == "amount"
    assert saved.aliases == ["kept alias"]


def test_rule_atomicity_is_shared_by_authoring_and_assessment():
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    for prompt in (load_prompt("learning_system"), load_prompt("learning_assessment")):
        assert "Rule atomicity concerns its action" in prompt
        assert "independently applicable complete decisions" in prompt
        assert "hypothetical alternatives" in prompt
    guide = load_prompt("learning_type_generation_rule")
    assert "Multiple" not in guide or "multiple labels" in guide.lower()
    assert "field suffixes" in guide


def test_case_metadata_is_scoped_and_full_text_remains_on_node(case_graph):
    from cortex.capabilities.sql_training.learning.inputs import graph_context
    session = session_for(case_graph)
    snapshot = session.graph.working
    column = next(a for a in snapshot.assets if a.id == "column.orders.amount")
    metadata = {"external:orders.csv": {"business_name": "Amount", "description": "Stored charge", "value_description": "Detailed encoding"}}
    replacement = column.model_copy(update={"properties": column.properties.model_copy(update={"source_metadata": metadata})})
    snapshot = snapshot.model_copy(update={"assets": [replacement if a.id == column.id else a for a in snapshot.assets]})
    context = graph_context(snapshot, {**session.record, "sql": "SELECT amount FROM orders"}, session.dialect)
    assert len(context["gold_bindings"]["columns"]) == 1
    summary = context["gold_bindings"]["columns"][0]["source_metadata"]["external:orders.csv"]
    assert summary == {"business_name": "Amount", "description": "Stored charge"}
    from cortex.capabilities.sql_training.graph.nodes import build_node
    assert build_node(replacement).properties["source_metadata"] == metadata


def test_normalizing_concept_predicate_keeps_literal_case(case_graph):
    session = session_for(case_graph)
    concept = node_example(render_type_guide(ontology_contract(session.graph.schema), "concept"))
    # A local representation expression exercises string preservation without
    # requiring a new governed category to exist in the database.
    concept["properties"]["target_expressions"] = {"0": "CAST([orders].[amount] AS TEXT) || 'MiXeD'"}
    result = next(t for t in build_tools(session) if t.name == "edit_graph").invoke({"edit_id": "literal-case", "expected_revision": session.graph.working_revision, "upserts": [concept]})
    assert result["status"] == "ok", result
    saved = next(a for a in session.graph.working.assets if a.id == concept["id"])
    assert "'MiXeD'" in saved.properties.target_expressions["0"]
    import sqlglot
    columns = list(sqlglot.parse_one(saved.properties.target_expressions["0"], dialect="sqlite").find_all(sqlglot.exp.Column))
    assert [(c.table, c.name) for c in columns] == [("orders", "amount")]


@pytest.mark.parametrize("expression", ["orders.amount FROM orders", "orders.amount WHERE 1 = 1", "orders.amount ORDER BY orders.amount"])
def test_normalization_does_not_hide_query_clauses(case_graph, expression):
    session = session_for(case_graph)
    concept = node_example(render_type_guide(ontology_contract(session.graph.schema), "concept"))
    concept["properties"]["target_expressions"] = {"0": expression}
    before = session.graph.working_revision
    result = next(t for t in build_tools(session) if t.name == "edit_graph").invoke({"edit_id": "query-clause", "expected_revision": before, "upserts": [concept]})
    assert result["status"] == "rejected"
    assert session.graph.working_revision == before


def test_concept_normalization_uses_requested_dialect_for_required_quotes():
    from cortex.capabilities.sql_training.contracts.semantic_mapping import (
        normalize_semantic_mapping_candidate,
    )
    from cortex.capabilities.sql_training.core.ids import make_node_id
    from cortex.capabilities.sql_training.models import NodeAsset
    ref = make_node_id("column", "orders", "gross amount")
    asset = NodeAsset.model_validate({"id": "concept.amount", "node_type": "concept", "name": "amount", "properties": {"targets": [[ref]], "target_expressions": {"0": "`orders`.`gross amount`"}}})
    schema = {"orders": "- gross amount: REAL"}
    mysql = normalize_semantic_mapping_candidate(asset, table_schema=schema, dialect="mysql")
    sqlite = normalize_semantic_mapping_candidate(asset, table_schema=schema, dialect="sqlite")
    import sqlglot
    for candidate, dialect in ((mysql, "mysql"), (sqlite, "sqlite")):
        expression = sqlglot.parse_one(candidate.properties.target_expressions["0"], dialect=dialect)
        assert expression.table == "orders" and expression.name == "gross amount"
        assert expression.args["this"].args["quoted"] is True


def test_business_scope_checks_are_shared_by_authoring_and_assessment():
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    for prompt in (load_prompt("learning_system"), load_prompt("learning_assessment")):
        assert "business edges do not automatically transfer" in prompt
        assert "JOINs and dates alone do not prove workflow" in prompt
        assert "participating objects/roles" in prompt
    event = load_prompt("learning_type_event")
    assert "isolated timestamp" in event
    assert "empty-shell Event" in event
    assert "historical location" in event


def test_quantity_atomicity_and_count_contract_reach_author_and_assessor():
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    for prompt in (load_prompt("learning_system"), load_prompt("learning_assessment")):
        assert "defining coupled calculations" in prompt
        assert "do not invent helper Metrics" in prompt
        assert "current result equality" in prompt
    metric = load_prompt("learning_type_metric")
    assert "COUNT(nullable_field)" in metric and "COUNT(*)" in metric
    assert "zero-denominator policies" in metric


def test_rule_scope_and_count_labels_preserve_generalization_boundaries():
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    guide = load_prompt("learning_type_generation_rule")
    assert "multiple labels" in guide
    assert "An arbitrary maximum does not imply COUNT" in guide
    assert "parameterize incidental request values and K" in guide
    for prompt in (load_prompt("learning_system"), load_prompt("learning_assessment")):
        assert "intrinsic constants" in prompt
        assert "NULL behavior" in prompt


def test_event_requires_related_participants_and_occurrence_context(case_graph):
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    for prompt in (system_prompt("direct", case_graph[0].schema), load_prompt("learning_assessment")):
        assert "participating objects/roles" in prompt
        assert "occurrence time or place" in prompt
    guide = render_type_guide(ontology_contract(case_graph[0].schema), "event")
    example = node_example(guide)
    assert example["properties"]["targets"] == []
    assert example["description"]


def test_compact_authoring_schema_preserves_property_names_and_validation():
    """Annotation compression must not erase fields or values named like keywords."""
    original = {
        "title": "Example", "description": "Long documentation", "default": {}, "examples": [{}],
        "type": "object", "additionalProperties": False,
        "required": ["description", "default", "examples", "title"],
        "properties": {
            "description": {"type": "string", "minLength": 1, "description": "Annotation"},
            "default": {"type": "integer", "minimum": 0, "maximum": 5, "default": 1},
            "examples": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "pattern": "^[a-z]+$"}},
            "title": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/description"}]},
        },
        "$defs": {"description": {"type": "object", "properties": {"default": {"type": "boolean"}}, "required": ["default"], "additionalProperties": False}},
        "allOf": [{"if": {"properties": {"default": {"const": 1}}}, "then": {"required": ["title"]}}],
        "const": {"description": "literal", "default": {"title": "data"}},
        "enum": [{"examples": [1], "title": "literal"}],
    }
    before = json.loads(json.dumps(original))
    result = compact_authoring_schema(original)
    assert original == before
    assert set(result["properties"]) == {"description", "default", "examples", "title"}
    assert result["properties"]["description"] == {"type": "string", "minLength": 1}
    assert result["properties"]["default"] == {"type": "integer", "minimum": 0, "maximum": 5}
    assert result["properties"]["examples"] == original["properties"]["examples"]
    assert result["properties"]["title"] == original["properties"]["title"]
    assert result["required"] == original["required"]
    assert result["$defs"] == original["$defs"]
    assert result["allOf"] == original["allOf"]
    assert result["const"] == original["const"] and result["enum"] == original["enum"]
    assert result["additionalProperties"] is False
    assert compact_authoring_schema(False) is False


def test_compact_guides_reduce_context_without_changing_runtime_contract(case_graph):
    contract = ontology_contract(case_graph[0].schema)
    before = json.loads(json.dumps(contract))
    raw_total = compact_total = 0
    for kind, model in AUTHORING_MODELS.items():
        guide = render_type_guide(contract, kind)
        compact_total += len(json.dumps(guide))
        expanded = dict(guide, asset_envelope=contract["output_schema"]["asset_envelope"], properties_schema=model.model_json_schema())
        raw_total += len(json.dumps(expanded))
        assert len(json.dumps(guide)) < 8000
    assert compact_total < raw_total - 5000
    assert contract == before


def test_nested_matching_error_path_points_to_properties(case_graph):
    session = session_for(case_graph)
    tool = next(t for t in build_tools(session) if t.name == "edit_graph")
    result = tool.invoke({"edit_id": "missing-targets", "expected_revision": session.graph.working_revision,
        "upserts": [{"id": "concept.bad", "node_type": "concept", "name": "bad", "properties": {}}]})
    assert result["status"] == "rejected"
    assert result["errors"][0]["path"] == "upserts.0.properties.targets"


def test_edge_extra_field_error_identifies_edge_operation(case_graph):
    session = session_for(case_graph)
    tool = next(t for t in build_tools(session) if t.name == "edit_graph")
    before = session.graph.working_revision
    result = tool.invoke({"edit_id": "misplaced-description", "expected_revision": before,
        "upserts": [{"id": "concept.amount", "node_type": "concept", "name": "amount", "properties": {"targets": [["column.orders.amount"]]}}],
        "edge_operations": [{"operation": "create_edge", "edge": {
            "id": "edge.amount", "edge_type": "grounded_on", "from_node_id": "concept.amount", "to_node_id": "column.orders.amount",
            "description": "Wrong nesting", "properties": {"description": "The stored order amount."}}}]})
    assert result["status"] == "rejected"
    assert result["errors"][0]["path"] == "edge_operations.0.edge.description"
    assert session.graph.working_revision == before


def test_case_semantics_contract_reaches_learning_and_assessment(case_graph):
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    from cortex.capabilities.sql_training.training.quality_contract import (
        QUALITY_CRITERIA,
    )
    learning = system_prompt("direct", case_graph[0].schema)
    assessment = load_prompt("learning_assessment")
    shared = learning.split("# Shared ontology contract", 1)[1].split("# Workflow and tools", 1)[0]
    assert shared in assessment
    for text in (learning, assessment):
        assert "task-relevant specific objects" in text
        assert "an unselected endpoint" in text
        assert "same-scope contradictions" in text
    assert "specific material gap" in assessment
    assert "Source differences" not in assessment
    assert "lack of an indivisibility proof is not failure" in QUALITY_CRITERIA["minimality"]
    assert len(assessment) < 12500
