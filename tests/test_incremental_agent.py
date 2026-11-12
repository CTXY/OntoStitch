"""Protocol repairs preserve provider tool IDs without an extra completion turn."""

from langchain_core.messages import AIMessage, ToolMessage
from test_graph_integration import state

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
from cortex.schema import load_root_schema


class Chat:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        return self

    def invoke(self, messages, config=None):
        self.requests.append(list(messages))
        return next(self.responses)


def test_valid_plain_json_is_validated_without_repeating_the_model():
    model = Chat([AIMessage(content='{"groups":[],"distinctions":[]}')])
    ctx = IntegrationContext(state(), state(), load_root_schema())
    agent = IncrementalIntegrationAgent(model)
    assert not agent.invoke("identity", ctx, {}).groups
    assert len(model.requests) == 1


def test_malformed_tool_json_receives_matching_tool_error_before_retry():
    malformed = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "IdentityDecision",
                "args": '{"groups": ]',
                "id": "broken",
                "error": "Invalid JSON",
            }
        ],
    )
    model = Chat([malformed, AIMessage(content='{"groups":[],"distinctions":[]}')])
    ctx = IntegrationContext(state(), state(), load_root_schema())
    IncrementalIntegrationAgent(model).invoke("identity", ctx, {})
    assert len(model.requests) == 2
    replies = [m for m in model.requests[1] if isinstance(m, ToolMessage)]
    assert len(replies) == 1 and replies[0].tool_call_id == "broken"
    assert "plain JSON" in model.requests[1][-1].content
    assert all(getattr(t, "__name__", None) != "IdentityDecision" for t in model.tools)


def test_acceptance_requires_atomic_decisions_and_exact_rule_coverage():
    import pytest
    from pydantic import ValidationError

    from cortex.capabilities.graph_merge.llm.incremental import NodeReview

    with pytest.raises(ValidationError, match="Acceptance"):
        NodeReview(
            verdict="accept",
            decision_checks=[
                {
                    "node_id": "r",
                    "independent_decisions": ["choose rows", "choose fields"],
                }
            ],
        )
    with pytest.raises(ValidationError, match="Acceptance"):
        NodeReview(
            verdict="accept",
            decision_checks=[
                {
                    "node_id": "r",
                    "independent_decisions": ["choose fields"],
                    "issues": [
                        "Unscoped ordinary policy bypasses a competing saved branch"
                    ],
                }
            ],
        )
    ctx = IntegrationContext(state(), state(), load_root_schema())
    with pytest.raises(ValueError, match="each supplied Rule decision"):
        IncrementalIntegrationAgent._validate(
            NodeReview(verdict="accept"),
            ctx,
            {"decision_review_ids": ["r"]},
            "node_assess",
        )


def test_empty_pending_tool_is_not_advertised():
    model = Chat([AIMessage(content='{"edits":[]}')])
    ctx = IntegrationContext(state(), state(), load_root_schema())
    IncrementalIntegrationAgent(model).invoke("reconcile", ctx, {})
    assert "inspect_pending" not in [getattr(t, "name", None) for t in model.tools]
