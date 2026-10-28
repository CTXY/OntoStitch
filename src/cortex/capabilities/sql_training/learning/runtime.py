"""LangChain owns the episode loop; Python owns the case lifecycle."""

from __future__ import annotations

import json

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_to_dict,
)
from pydantic import ValidationError

from cortex.application.ontology_graph import GraphResult, input_issues, issue
from cortex.capabilities.sql_training.graph.snapshots import fingerprint
from cortex.capabilities.sql_training.learning.inputs import system_prompt
from cortex.capabilities.sql_training.learning.session import (
    EpisodeResult,
    LearningSession,
)
from cortex.capabilities.sql_training.learning.tools import (
    READ_TOOLS,
    WRITE_TOOLS,
    build_tools,
)
from cortex.infrastructure.observability import native_tracing


class EpisodeHooks(AgentMiddleware):
    def __init__(self, session: LearningSession):
        self.session = session
        self.batch_progress = None

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        session = self.session
        if self.batch_progress is not None:
            session.no_progress = 0 if self.batch_progress else session.no_progress + 1
            self.batch_progress = None
        if session.closed or session.exhausted():
            if not session.closed:
                session.stop_reason = (
                    "resource_limit"
                    if session.errors < 3 and session.no_progress < 3
                    else "unable_to_continue"
                )
            return {"jump_to": "end"}
        return None

    def wrap_model_call(self, request, handler):
        session = self.session
        context = session.context()
        system = system_prompt(session.mode, session.graph.schema)
        rendered = json.dumps(context, ensure_ascii=False, default=str)
        system_message = SystemMessage(content=system)
        context_message = HumanMessage(
            content=f"<case_context>\n{rendered}\n</case_context>"
        )
        messages = list(request.messages)
        # Drop only complete old model/tool batches, never leave dangling tool results.
        tools_size = len(
            json.dumps(
                [
                    t.args_schema.model_json_schema()
                    if getattr(t, "args_schema", None)
                    else {}
                    for t in request.tools
                ],
                default=str,
            )
        )
        cap = (
            session.config.agent_context_characters
            - tools_size
            - len(system_message.content)
            - len(json.dumps(messages_to_dict([context_message]), default=str))
        )
        if cap < 1000:
            raise ValueError(
                "essential case source/targets/graph overview exceed configured context limit"
            )
        while len(json.dumps(messages_to_dict(messages), default=str)) > cap:
            next_ai = next(
                (n for n, msg in enumerate(messages[1:], 1) if msg.type == "ai"), None
            )
            if next_ai is None:
                candidates = [
                    (len(str(msg.content)), n)
                    for n, msg in enumerate(messages)
                    if isinstance(msg, ToolMessage) and len(str(msg.content)) > 500
                ]
                if not candidates:
                    raise ValueError(
                        "current model/tool arguments exceed configured context limit"
                    )
                _, index = max(candidates)
                original = messages[index]
                ref = session.evidence.put(
                    "context_tool_result", original.model_dump(mode="json")
                )
                messages[index] = original.model_copy(
                    update={
                        "content": json.dumps(
                            {
                                "status": "externalized",
                                "evidence_ref": ref,
                                "instruction": "Read a smaller page with read_evidence; the full result remains saved.",
                            }
                        )
                    }
                )
                continue
            messages = messages[next_ai:]
        # Reserve the final decision for an explicit, model-authored handoff.
        # The model still chooses ready versus unable; budget exhaustion never
        # promotes an unfinished graph or synthesizes type coverage.
        final_decision = session.model_calls == session.config.agent_max_steps - 1
        # Inject one fresh context per request, outside the trimmable conversation.
        # It is not accumulated in the agent's message history.
        override = {
            "system_message": system_message,
            "messages": [context_message, *messages],
        }
        if final_decision:
            override.update(
                tools=[t for t in request.tools if t.name == "handoff"],
                # Reasoning providers may reject forced named-tool selection.
                # Expose only handoff without fabricating a successful delivery.
                tool_choice="auto",
            )
        session.model_calls += 1
        return handler(request.override(**override))

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        response = state["messages"][-1]
        if getattr(response, "invalid_tool_calls", []):
            self.session.errors += 1
            ref = self.session.evidence.put(
                "invalid_tool_request", response.model_dump(mode="json")
            )
            errors = []
            for call in response.invalid_tool_calls:
                arguments = call.get("args") or ""
                try:
                    json.loads(arguments)
                except json.JSONDecodeError as exc:
                    errors.append(
                        f"{call['name']}: {exc.msg} at line {exc.lineno}, column {exc.colno}; "
                        f"near {arguments[max(0, exc.pos - 40) : exc.pos + 40]!r}"
                    )

            # Replace the rejected assistant batch so the next provider request
            # cannot contain malformed or unanswered tool calls. Nothing executes.
            return {
                "messages": [
                    AIMessage(
                        id=response.id,
                        content=f"Tool batch rejected: malformed JSON arguments. {'; '.join(errors)} Evidence: {ref}",
                    ),
                    HumanMessage(
                        content="No tools in that batch were executed. Reissue the intended "
                        "call with valid JSON matching its tool schema. Follow the tool's "
                        "description and keep the edit small. Call handoff when complete."
                    ),
                ],
                "jump_to": "model",
            }
        calls = getattr(response, "tool_calls", [])
        if not calls:
            return None
        names = [call["name"] for call in calls]
        limit = self.session.config.agent_read_concurrency
        invalid = (len(calls) > 1 and any(n in WRITE_TOOLS for n in names)) or len(
            calls
        ) > limit
        invalid = invalid or any(n not in READ_TOOLS | WRITE_TOOLS for n in names)
        if invalid:
            self.session.errors += 1
            return {
                "messages": [
                    ToolMessage(
                        content="Batch rejected before execution: only up to "
                        f"{limit} read tools may be batched; edit_graph/update_notes/handoff must be called alone.",
                        tool_call_id=c["id"],
                        name=c["name"],
                        status="error",
                    )
                    for c in calls
                ],
                "jump_to": "model",
            }
        # Count one completed model/tool batch, regardless of read concurrency.
        self.batch_progress = False
        return None

    def wrap_tool_call(self, request, handler):
        session = self.session
        call = request.tool_call
        with session.lock:
            if session.closed or session.exhausted(include_model_limit=False):
                return ToolMessage(
                    content="Episode is closed or exhausted.",
                    tool_call_id=call["id"],
                    status="error",
                )
            session.tool_calls += 1
        before = session.graph.working.content_fingerprint
        try:
            result = handler(request)
        except (ValueError, TypeError, KeyError) as exc:
            with session.lock:
                session.errors += 1
            result = ToolMessage(
                content=json.dumps(
                    GraphResult(
                        status="rejected",
                        revision=session.graph.working_revision,
                        errors=(
                            input_issues(exc)
                            if isinstance(exc, ValidationError)
                            else [
                                issue(
                                    "INVALID_TOOL_INPUT",
                                    str(exc),
                                    "Read the tool schema or graph details and correct this request.",
                                )
                            ]
                        ),
                    ).model_dump(mode="json")
                ),
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )
        else:
            if isinstance(result, ToolMessage) and result.status != "error":
                try:
                    payload = (
                        json.loads(result.content)
                        if isinstance(result.content, str)
                        else None
                    )
                except (TypeError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict) and payload.get("status") in {
                    "rejected",
                    "error",
                }:
                    result = result.model_copy(update={"status": "error"})
            if isinstance(result, ToolMessage) and result.status == "error":
                content = str(result.content)
                marker = " with error:\n"
                if marker in content:
                    # ToolNode echoes the entire invalid request by default. Keep
                    # field errors, not another copy of a multi-node graph edit.
                    result = result.model_copy(
                        update={"content": content.rsplit(marker, 1)[1]}
                    )
            with session.lock:
                if getattr(result, "status", None) == "error":
                    session.errors += 1
                else:
                    session.errors = 0
                    signature = fingerprint(
                        {"name": call["name"], "args": call["args"], "graph": before}
                    )
                    new_fact = (
                        call["name"] in READ_TOOLS
                        and signature not in session.observed_reads
                    )
                    session.observed_reads.add(signature)
                    changed = session.graph.working.content_fingerprint != before
                    if new_fact or changed:
                        self.batch_progress = True
        session.evidence.put(
            "tool_call",
            {
                "tool": call,
                "result": result.model_dump(mode="json")
                if hasattr(result, "model_dump")
                else str(result),
            },
        )
        return result


class LearningAgent:
    def __init__(self, model: BaseChatModel):
        self.model = model

    def run(self, session: LearningSession) -> EpisodeResult:
        starting = session.graph.working_revision
        agent = create_agent(
            self.model,
            tools=build_tools(session),
            middleware=[EpisodeHooks(session)],
            name="ontology_learning",
        )
        with native_tracing():
            result = agent.invoke(
                {
                    "messages": [
                        HumanMessage(
                            content="Complete this ontology learning episode using the tools."
                        )
                    ]
                },
                config={
                    "run_name": "learning." + session.mode,
                    "metadata": {
                        "case_id": session.record["source_id"],
                        "mode": session.mode,
                    },
                    "max_concurrency": session.config.agent_read_concurrency,
                    "recursion_limit": session.config.agent_max_steps * 5 + 10,
                },
            )
        transcript_ref = session.evidence.put(
            "episode_transcript", messages_to_dict(result["messages"])
        )
        handoff = session.handoff_result or {}
        coverage = handoff.get("direct_type_coverage", [])
        outcome = (
            handoff.get("outcome") or session.stop_reason or "ready_for_validation"
        )
        return EpisodeResult(
            outcome=outcome,
            starting_revision=starting,
            delivered_revision=session.graph.working_revision,
            direct_completed=session.mode == "direct"
            and handoff.get("outcome") == "ready_for_validation"
            and set(coverage) == {"concept", "metric", "generation_rule"},
            direct_type_coverage=coverage,
            proposed_groups=session.groups,
            summary=handoff.get("summary", "Model ended without explicit handoff."),
            model_calls=session.model_calls,
            tool_calls=session.tool_calls,
            database_calls=session.database_calls,
            transcript_ref=transcript_ref,
        )
