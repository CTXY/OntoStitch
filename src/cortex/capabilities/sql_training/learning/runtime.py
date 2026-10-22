"""LangChain owns the episode loop; Python owns the case lifecycle."""

from __future__ import annotations

import json

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_to_dict,
)
from langsmith import tracing_context

from cortex.capabilities.sql_training.graph.snapshots import fingerprint
from cortex.capabilities.sql_training.learning.session import (
    EpisodeResult,
    LearningSession,
)
from cortex.capabilities.sql_training.learning.tools import (
    READ_TOOLS,
    WRITE_TOOLS,
    build_tools,
)
from cortex.capabilities.sql_training.llm.prompts import load_prompt
from cortex.capabilities.sql_training.training.observability import SummaryTracer


class EpisodeHooks(AgentMiddleware):
    def __init__(self, session: LearningSession):
        self.session = session

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        session = self.session
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
        system = (
            load_prompt("learning_system")
            + "\n\n"
            + load_prompt(f"learning_{session.mode}")
        )
        rendered = json.dumps(context, ensure_ascii=False, default=str)
        system_message = SystemMessage(
            content=f"{system}\n\n<case_context>\n{rendered}\n</case_context>"
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
        session.model_calls += 1
        with SummaryTracer().span(
            "extract.learning_model",
            case_id=session.record["source_id"],
            model_calls=session.model_calls,
            mode=session.mode,
            prompt_fingerprint=fingerprint(system),
        ):
            return handler(
                request.override(system_message=system_message, messages=messages)
            )

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        calls = getattr(state["messages"][-1], "tool_calls", [])
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
            with SummaryTracer().span(
                "extract.tool", case_id=session.record["source_id"], tool=call["name"]
            ):
                result = handler(request)
        except (ValueError, TypeError, KeyError) as exc:
            with session.lock:
                session.errors += 1
            result = ToolMessage(
                content=str(exc),
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )
        else:
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
                    session.no_progress = (
                        0 if new_fact or changed else session.no_progress + 1
                    )
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
        # Automatic nested tracing is disabled here; explicit summary spans live in
        # observability.py so full prompts/results remain only in local evidence.
        with tracing_context(enabled=False):
            result = agent.invoke(
                {
                    "messages": [
                        HumanMessage(
                            content="Complete this ontology learning episode using the tools."
                        )
                    ]
                },
                config={
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
