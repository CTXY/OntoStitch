"""Large coupled identity groups retain complete evidence when budgets grow."""
import pytest
from test_graph_integration import catalog, concept, state

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.node_review import ReviewInputTooLarge, review_packets
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

    from cortex.capabilities.graph_merge.llm.input_budget import RequestInputTooLarge
    from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent

    ctx = IntegrationContext(state(), state([*catalog(), concept("N", "a")]), load_root_schema())
    ctx.candidate = ctx.union_state()
    model = Chat([AIMessage(content='{"verdict":"accept"}')])
    agent = IncrementalIntegrationAgent(model, max_calls=1, request_max_characters=budget)
    payload = {"nodes": [], "edges": [], "evidence": [], "complete_source_detail": "x" * 211000}
    if budget == 210000:
        with pytest.raises(RequestInputTooLarge):
            agent.invoke("graph_assess", ctx, payload)
        assert not model.requests
    else:
        assert agent.invoke("graph_assess", ctx, payload).verdict == "accept"
        assert len(model.requests) == 1
