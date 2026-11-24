"""Offline model doubles; bound tool options never mutate the base model."""
from copy import deepcopy
from langchain_core.utils.function_calling import convert_to_openai_tool

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


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.tools = []

    def bind(self, **kwargs):
        return BoundModel(self, [])

    def bind_tools(self, tools, **kwargs):
        return BoundModel(self, [convert_to_openai_tool(tool) for tool in tools])

    def invoke(self, messages, config=None):
        self.requests.append((deepcopy(messages), deepcopy(self.tools)))
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply(self, messages) if callable(reply) else reply


class BoundModel:
    def __init__(self, model, tools):
        self.model, self.tools = model, tools

    def bind(self, **kwargs):
        return self

    def invoke(self, messages, config=None):
        self.model.tools = self.tools
        return self.model.invoke(messages, config=config)
