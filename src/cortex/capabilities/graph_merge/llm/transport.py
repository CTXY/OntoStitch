"""Caller-managed transport recovery without discarding the Agent dialogue."""

from cortex.infrastructure.llm.stream_audit import UpstreamResponseIncomplete

RETRYABLE_HTTP_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class TransportRecoveryStopped(RuntimeError):
    """A persistent provider failure requires intervention, not another replay."""

    retryable = False


def _connection_failure(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        status = getattr(error, "status_code", None)
        if status is None:
            status = getattr(getattr(error, "response", None), "status_code", None)
        if status in RETRYABLE_HTTP_STATUS:
            return True
        if isinstance(
            error, (UpstreamResponseIncomplete, ConnectionError, TimeoutError)
        ) or type(error).__name__ in {
            "APIConnectionError",
            "OpenAIConnectionError",
            "RemoteProtocolError",
            "ReadError",
            "ReadTimeout",
            "ConnectError",
            "ConnectTimeout",
        }:
            return True
        error = error.__cause__ or error.__context__
    return False


def invoke_with_recovery(
    agent, tools, messages, *, config, tool_choice="required", response_format=None
):
    failures = 0
    while True:
        options = {"strict": True} if response_format is not None else {}
        model = (
            agent.llm.bind_tools(tools, tool_choice=tool_choice, **options)
            if tools
            else agent.llm
        )
        if response_format is not None:
            model = model.bind(response_format=response_format)
        try:
            return model.invoke(messages, config=config)
        except Exception as error:
            failures = failures + 1 if _connection_failure(error) else 0
            if failures >= 3:
                raise TransportRecoveryStopped(
                    "Transport failed for the same logical request three consecutive times. "
                    "Stopped automatic replay; retain the accepted checkpoint and verify service recovery before resuming."
                ) from error
            recover = getattr(agent, "recover_transport", None)
            replacement = recover(error) if recover else None
            if replacement is None:
                raise
            agent.llm = replacement
