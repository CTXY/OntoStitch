"""Caller-managed transport recovery without discarding the Agent dialogue."""

from cortex.infrastructure.llm.stream_audit import UpstreamResponseIncomplete


class TransportRecoveryStopped(RuntimeError):
    """A persistent provider failure requires intervention, not another replay."""
    retryable = False


def _connection_failure(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (UpstreamResponseIncomplete, ConnectionError, TimeoutError)) or type(error).__name__ in {
            'APIConnectionError', 'OpenAIConnectionError', 'RemoteProtocolError',
            'ReadError', 'ReadTimeout', 'ConnectError', 'ConnectTimeout',
        }:
            return True
        error = error.__cause__ or error.__context__
    return False


def invoke_with_recovery(agent, tools, messages, *, config, tool_choice="required"):
    truncations = 0
    while True:
        model = agent.llm.bind_tools(tools, tool_choice=tool_choice) if tools else agent.llm
        try:
            return model.invoke(messages, config=config)
        except Exception as error:
            truncations = truncations + 1 if _connection_failure(error) else 0
            if truncations >= 3:
                raise TransportRecoveryStopped(
                    'Transport failed for the same logical request three consecutive times. '
                    'Stopped automatic replay; retain the accepted checkpoint and verify service recovery before resuming.'
                ) from error
            recover = getattr(agent, "recover_transport", None)
            replacement = recover(error) if recover else None
            if replacement is None:
                raise
            agent.llm = replacement
