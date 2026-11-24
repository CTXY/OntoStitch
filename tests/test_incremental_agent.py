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
    agent = IncrementalIntegrationAgent(model)
    agent.recover_transport = lambda error: model
    assert not agent.invoke("identity", ctx, {}).groups
    assert len(model.requests) == 2
    assert model.requests[0] == model.requests[1]
    model = Disconnected(3)
    agent = IncrementalIntegrationAgent(model)
    agent.recover_transport = lambda error: model
    from cortex.capabilities.graph_merge.llm.transport import TransportRecoveryStopped
    with pytest.raises(TransportRecoveryStopped):
        agent.invoke("identity", ctx, {})
    assert len(model.requests) == 3


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
    agent = IncrementalIntegrationAgent(model)
    agent.recover_transport = lambda error: model if error.response.status_code == 502 else None
    agent.invoke("identity", ctx, {})
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
    assert not replies
    assert "Malformed tool JSON" in model.requests[1][1].content
    assert "plain JSON" in model.requests[1][-1].content
    assert all(getattr(t, "__name__", None) != "IdentityDecision" for t in model.tools)


def test_node_writer_exposes_only_evidence_and_submission_without_legacy_draft_tools():
    from types import SimpleNamespace
    from test_node_local_context import context

    ctx = context()
    compiled = SimpleNamespace(state=ctx.task_before)
    drafts = []
    def compile_draft(value):
        drafts.append(value)
        return compiled
    ctx.compile_task_patch = compile_draft
    model = Chat([AIMessage(content='', tool_calls=[{'name':'submit_patch','id':'done',
        'args':{'keep_separate_ids':['N1','N2'], 'explanation':'Keep both supplied meanings unchanged.'}}])])
    result = IncrementalIntegrationAgent(model).invoke('consolidate', ctx, {'task_id':'node-group'})
    assert result.keep_separate_ids == ('N1', 'N2') and not result.integrate_groups
    assert drafts == [result] and ctx.last_compiled_patch is compiled
    assert ctx.candidate == ctx.task_before
    assert len(model.requests) == 1
    names = {getattr(tool, 'name', getattr(tool, '__name__', None)) for tool in model.tools}
    assert names == {'read_evidence', 'submit_patch'}


def test_review_repairs_exact_reference_paths_without_rule_checklists():
    import json

    from test_node_local_context import context
    ctx = context()
    base, a = ctx.task_before, 'N1'
    issue = {'refs':[{'kind':'node','id':a,'fields':['meaning']}],
             'basis':[{'kind':'node','id':a,'view':'before'}],
             'problem':'The recorded definition was distorted.',
             'required_outcome':'Preserve the original object meaning.'}
    corrected = {**issue, 'refs':[{'kind':'node','id':a,'fields':['properties.definition']}]}
    model = Chat([
        AIMessage(content='', tool_calls=[{'name':'Review','id':'bad','args':{'verdict':'revise','issues':[issue]}}]),
        AIMessage(content='', tool_calls=[{'name':'Review','id':'fixed','args':{'verdict':'revise','issues':[corrected]}}]),
    ])
    result = IncrementalIntegrationAgent(model).invoke('node_assess', ctx, {'evidence':[]})
    assert result.verdict == 'revise'
    assert len(model.requests) == 2
    reply = json.loads(model.requests[1][-1].content.split('\n', 1)[1])
    assert "Unknown review field 'meaning'" in reply['issues'][0]['error']
    assert reply['draft']['issues'][0]['refs'][0]['fields'] == ['meaning']
    assert result.issues[0].refs[0].fields == ('properties.definition',)
    assert ctx.candidate == base
