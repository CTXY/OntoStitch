"""Caller-managed transport recovery without discarding the Agent dialogue."""


def invoke_with_recovery(agent, tools, messages, *, config, tool_choice="required"):
    while True:
        model = agent.llm.bind_tools(tools, tool_choice=tool_choice) if tools else agent.llm
        try:
            return model.invoke(messages, config=config)
        except Exception as error:
            recover = getattr(agent, "recover_transport", None)
            replacement = recover(error) if recover else None
            if replacement is None:
                raise
            agent.llm = replacement
