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


def test_connection_retry_preserves_messages_and_is_bounded():
    import httpx
    import pytest
    from langchain_openai.chat_models.base import OpenAIConnectionError

    class Disconnected(Chat):
        def __init__(self, failures):
            super().__init__([AIMessage(content='{"groups":[],"distinctions":[]}')])
            self.failures = failures

        def invoke(self, messages, config=None):
            if self.failures:
                self.requests.append(list(messages))
                self.failures -= 1
                raise OpenAIConnectionError(
                    message="connection dropped",
                    request=httpx.Request("POST", "https://example.invalid"),
                )
            return super().invoke(messages, config=config)

    ctx = IntegrationContext(state(), state(), load_root_schema())
    model = Disconnected(1)
    assert not IncrementalIntegrationAgent(model).invoke("identity", ctx, {}).groups
    assert len(model.requests) == 2
    assert model.requests[0] == model.requests[1]
    model = Disconnected(2)
    with pytest.raises(OpenAIConnectionError):
        IncrementalIntegrationAgent(model).invoke("identity", ctx, {})
    assert len(model.requests) == 2


def test_upstream_retry_excludes_balance_or_auth_errors():
    import httpx
    import pytest
    from langchain_openai.chat_models.base import OpenAIAPIError

    class Failed(Chat):
        def __init__(self, status):
            super().__init__([AIMessage(content='{"groups":[],"distinctions":[]}')])
            self.status = status
            self.failed = False

        def invoke(self, messages, config=None):
            if not self.failed:
                self.failed = True
                self.requests.append(list(messages))
                response = httpx.Response(
                    self.status,
                    request=httpx.Request("POST", "https://example.invalid"),
                )
                raise OpenAIAPIError(
                    message="upstream failed", response=response, body={}
                )
            return super().invoke(messages, config=config)

    ctx = IntegrationContext(state(), state(), load_root_schema())
    model = Failed(502)
    IncrementalIntegrationAgent(model).invoke("identity", ctx, {})
    assert len(model.requests) == 2
    for status in [401, 402]:
        model = Failed(status)
        with pytest.raises(OpenAIAPIError):
            IncrementalIntegrationAgent(model).invoke("identity", ctx, {})
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
    IncrementalIntegrationAgent(model).invoke("consolidate", ctx, {})
    assert "inspect_pending" not in [getattr(t, "name", None) for t in model.tools]


def test_reason_finding_cannot_quote_a_retired_source_version():
    import pytest

    from cortex.capabilities.graph_merge.llm.incremental import NodeReview
    from cortex.schema import GraphNode, GraphSpace, GraphState

    ctx = IntegrationContext(state(), state(), load_root_schema())
    ctx.candidate = GraphState(
        graph=GraphSpace(
            nodes=(
                GraphNode(
                    id="r",
                    node_type="generation_rule",
                    name="order outputs",
                    properties={
                        "reason": "Gold orders the requested columns; uniqueness is guaranteed."
                    },
                ),
            )
        )
    )
    payload = {"rationale_review_ids": ["r"]}
    stale = NodeReview(
        verdict="revise",
        issues=["A claimed observation lacks evidence"],
        issue_refs=[{"kind":"node","id":"r","fields":["properties.reason"]}],
        rationale_checks=[
            {
                "node_id": "r",
                "supported": False,
                "explanation": "The quoted observed-history claim has no supplied evidence.",
                "unsupported_claims": ["The validated attempt returned eight rows."],
            }
        ],
    )
    with pytest.raises(ValueError, match="actual CURRENT reason"):
        IncrementalIntegrationAgent._validate(stale, ctx, payload, "node_assess")
    current = stale.model_copy(
        update={
            "rationale_checks": (
                stale.rationale_checks[0].model_copy(
                    update={"unsupported_claims": ("uniqueness is guaranteed",)}
                ),
            )
        }
    )
    IncrementalIntegrationAgent._validate(current, ctx, payload, "node_assess")
