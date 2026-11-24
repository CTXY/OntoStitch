"""Large coupled identity groups retain complete evidence when budgets grow."""
import pytest
from test_graph_integration import catalog, concept, state

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.node_review import (
    ReviewInputTooLarge,
    review_packets,
)
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.config.models import GraphMergeConfig
from cortex.schema import load_root_schema


def test_larger_review_budget_keeps_a_coupled_group_whole_without_truncation():
    details = {"A": "First supported source: " + "x" * 65000,
               "B": "Second supported source: " + "y" * 65000}
    nodes = [concept(key, "a").model_copy(update={"description": details[key]}) for key in ("A", "B")]
    ctx = IntegrationContext(state(), state([*catalog(), *nodes]), load_root_schema())
    candidate = ctx.union_state()
    ids = {ctx.input_node_map[key] for key in ("A", "B")}
    groups = [{"members": sorted(ids), "meaning": "Same referent", "reason": "Same evidenced object"}]
    task = LocalTask("review", "node_assess", ids, groups=groups)
    origins = LineageIndex(ctx)
    with pytest.raises(ReviewInputTooLarge, match="Indivisible"):
        list(review_packets(ctx, candidate, task, origins=origins, before=candidate))
    packets = list(review_packets(ctx, candidate, task, origins=origins, before=candidate,
                                  max_characters=200000))
    assert len(packets) == 1
    assert packets[0]["matched_groups"] == groups
    assert {node["id"] for node in packets[0]["candidate_nodes"]} >= ids
    expected = {ctx.input_node_map[key]: value for key, value in details.items()}
    assert all(node["description"] == expected[node["id"]]
               for node in packets[0]["candidate_nodes"] if node["id"] in ids)


def test_character_budgets_are_configurable_and_positive():
    defaults = GraphMergeConfig()
    assert defaults.node_context_max_characters == 110000
    assert defaults.request_max_characters == 210000
    configured = GraphMergeConfig(node_context_max_characters=200000, request_max_characters=400000)
    assert configured.node_context_max_characters == 200000
    with pytest.raises(ValueError):
        GraphMergeConfig(node_context_max_characters=0)


@pytest.mark.parametrize("budget", [210000, 400000])
def test_relation_agent_checks_the_configured_complete_request_budget(budget):
    from langchain_core.messages import AIMessage
    from test_incremental_agent import Chat
    from test_reconciliation_context import node_context, runtime

    from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
    from cortex.capabilities.graph_merge.llm.input_budget import RequestInputTooLarge, check_request

    class BudgetChat(Chat):
        def bind(self, **kwargs):
            return self

    class Trace:
        def __init__(self):
            self.events = []

        def record(self, event):
            self.events.append(event)

    ctx = node_context()
    full_description = "Complete evidenced meaning: " + "x" * 211000
    ctx.task_before = ctx.task_before.model_copy(update={"graph": ctx.task_before.graph.model_copy(update={
        "nodes": tuple(node.model_copy(update={"description": full_description}) if node.id == "N1" else node
                       for node in ctx.task_before.graph.nodes),
    })})
    ctx.candidate = ctx.task_before
    original = ctx.task_before.model_dump(mode="json")
    # This supplies a fixed before, a real candidate and a supported explicit
    # check. Its complete selected node body must survive request construction.
    payload = runtime(ctx=ctx, phase="graph_assess").payload
    model = BudgetChat([AIMessage(content='{"explanation":"The complete local result preserves supported knowledge.","feedback":[]}')])
    trace = Trace()
    agent = IncrementalIntegrationAgent(model, max_calls=1, request_max_characters=budget, trace_sink=trace)
    if budget == 210000:
        with pytest.raises(RequestInputTooLarge) as caught:
            agent.invoke("graph_assess", ctx, payload)
        assert caught.value.details["characters"] > budget
        assert caught.value.retryable is False
        assert not model.requests
    else:
        assert agent.invoke("graph_assess", ctx, payload).verdict == "accept"
        assert len(model.requests) == 1
        assert full_description in model.requests[0][1].content
        actual_size = check_request(model.requests[0], model.tools, max_characters=budget)
        assert 210000 < actual_size <= budget
    measurement, = [event for event in trace.events if event["event_type"] == "request_budget"]
    assert measurement["budget"] == budget
    assert measurement["components"]["fixed_task_and_before_graph"] > len(full_description)
    assert measurement["components"]["tool_schemas"] > 0
    if model.requests:
        assert measurement["characters"] == actual_size
    else:
        assert measurement["characters"] == caught.value.details["characters"]
    assert ctx.task_before.model_dump(mode="json") == original
    assert ctx.candidate == ctx.task_before


def test_oversized_request_reports_components_and_never_retries_or_modifies_the_input():
    import json
    from copy import deepcopy

    from langchain_core.messages import HumanMessage, SystemMessage

    from cortex.capabilities.graph_merge.llm.input_budget import (
        RequestInputTooLarge,
        check_request,
    )
    payload = {'draft': {'explanation': 'x' * 500}, 'nodes': [{'id': 'N', 'name': 'Exact original content'}],
               'semantic_issue_basis': [{'record': {'name': 'Original source meaning'}}]}
    original = deepcopy(payload)
    measurements = []
    with pytest.raises(RequestInputTooLarge) as caught:
        check_request([SystemMessage('Contract'), HumanMessage(json.dumps(payload))], [],
                      max_characters=100, on_measure=measurements.append)
    assert caught.value.retryable is False
    assert caught.value.details == measurements[0]
    assert caught.value.details['components']['draft'] > 500
    assert caught.value.details['components']['diagnostics_and_basis'] > 0
    assert payload == original
