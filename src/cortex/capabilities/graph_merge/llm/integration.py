"""One merge role with explicit identity/integration phases and an independent review."""

from hashlib import sha256
from importlib.resources import files

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    after_model,
    wrap_model_call,
    wrap_tool_call,
)
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage

from cortex.capabilities.graph_merge.graph.edit_contract import (
    IdentityDecision,
    IntegrationProposal,
    IntegrationReview,
    review_output_model,
)
from cortex.capabilities.graph_merge.llm.packing import (
    pack_payload,
    prompt_json,
)
from cortex.capabilities.graph_merge.identity.tools import build_tools
from cortex.capabilities.graph_merge.llm.agents import (
    _AutomaticToolChoice,
)
from cortex.capabilities.graph_merge.llm.submission import submission_error
from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace

OUTPUTS = {
    "identity": IdentityDecision,
    "propose": IntegrationProposal,
    "consolidate": IntegrationProposal,
    "reconcile": IntegrationProposal,
    "assess": IntegrationReview,
}


def prompt(phase):
    return (
        files("cortex.capabilities.graph_merge.llm.prompts")
        .joinpath(f"integration_{phase}.md")
        .read_text(encoding="utf-8")
    )


class IntegrationAgent:
    def __init__(self, llm, *, max_calls=18, trace_sink=None):
        self.llm, self.max_calls, self.trace_sink = llm, max_calls, trace_sink

    def invoke(self, phase, context, payload):
        output_model = (
            review_output_model(payload.get("rationale_review_ids", []))
            if phase in {"assess", "node_assess"}
            else OUTPUTS[phase]
        )

        buffer = getattr(context, "proposal_buffer", None)
        if buffer is not None:
            from pydantic import create_model, model_validator

            from cortex.capabilities.graph_merge.graph.edit_contract import (
                StageCompletion,
            )

            @model_validator(mode="after")
            def require_valid_buffer(value):
                if value.verdict == "complete" and not buffer.valid:
                    raise ValueError(
                        "Repair submit_edits errors before finishing. Submit empty lists to validate a no-change result."
                    )
                if phase == "reconcile" and value.verdict == "complete":
                    expected = {s["id"] for s in payload.get("inspection_signals", [])}
                    found = [c.inspection_id for c in value.inspection_checks]
                    if set(found) != expected or len(found) != len(set(found)):
                        raise ValueError(
                            "Return one inspection_checks entry for every supplied inspection signal ID; compare actual semantics, do not infer edits from topology."
                        )
                    current_nodes = {
                        n.id: n for n in buffer.context.candidate.graph.nodes
                    }
                    for check in value.inspection_checks:
                        kind = next(
                            s["kind"]
                            for s in payload["inspection_signals"]
                            if s["id"] == check.inspection_id
                        )
                        if (
                            kind in {"related_decisions", "source_selection_variants"}
                            and check.decision_relation is None
                        ):
                            raise ValueError(
                                "Decision inspections must state compatible/conditional/conflict/different_decision"
                            )
                        if check.decision_relation == "conflict" and not any(
                            key in current_nodes
                            and len(current_nodes[key].alternatives) > 1
                            for key in check.result_ids
                        ):
                            raise ValueError(
                                "A declared conflict needs result_ids identifying complete saved alternatives; ordinary competing nodes are not conflict preservation"
                            )
                    expected_rules = {
                        n.id
                        for n in current_nodes.values()
                        if n.node_type == "generation_rule"
                    }
                    reviewed = [r.node_id for r in value.rationale_checks]
                    if set(reviewed) != expected_rules or len(reviewed) != len(
                        set(reviewed)
                    ):
                        raise ValueError(
                            f"Return one rationale_checks entry for each current Rule reason, including retained Rules: {sorted(expected_rules)}"
                        )
                    if any(
                        c.conclusion == "failed" for c in value.inspection_checks
                    ) or any(not c.supported for c in value.rationale_checks):
                        raise ValueError(
                            "Unresolved inspections or unsupported Rule reasons require repair before complete."
                        )
                return value

            output_model = create_model(
                "StageCompletion",
                __base__=StageCompletion,
                __validators__={"require_valid_buffer": require_valid_buffer},
            )

        @after_model(can_jump_to=["model"])
        def require_output(state, runtime):
            last = state["messages"][-1]
            if state.get("structured_response") is None and not getattr(
                last, "tool_calls", ()
            ):
                return {
                    "messages": [
                        HumanMessage(
                            content=prompt("submit_retry")
                            .replace("{output_name}", output_model.__name__)
                            .replace(
                                "{submission_error}",
                                submission_error(last)
                                or "The response did not contain a valid structured submission.",
                            )
                        )
                    ],
                    "jump_to": "model",
                }
            return None

        read_rounds = 0
        read_limit = {"consolidate": 4, "reconcile": 7, "node_assess": 4}.get(phase)

        @wrap_model_call
        def bounded_reads(request, handler):
            nonlocal read_rounds
            read_rounds += 1
            if read_limit is not None and read_rounds > read_limit:

                def name(tool):
                    return (
                        tool.name
                        if hasattr(tool, "name")
                        else tool.get("name", tool.get("function", {}).get("name"))
                    )

                request = request.override(
                    tools=[
                        t
                        for t in request.tools
                        if name(t)
                        not in {"inspect_graph", "read_evidence", "read_guidance"}
                    ],
                    tool_choice="required",
                )
            return handler(request)

        @wrap_tool_call
        def enforce_read_budget(request, handler):
            if (
                read_limit is not None
                and read_rounds > read_limit
                and request.tool_call["name"]
                in {"inspect_graph", "read_evidence", "read_guidance"}
            ):
                from langchain_core.messages import ToolMessage

                return ToolMessage(
                    content="Read budget exhausted. Current definitions and source evidence already supplied; submit the supported small edits, repair pending errors, or finish with failed if essential evidence is still missing.",
                    tool_call_id=request.tool_call["id"],
                    name=request.tool_call["name"],
                )
            return handler(request)

        text = (
            prompt(phase)
            + "\n\n"
            + prompt("context").replace("{output_name}", output_model.__name__)
        )
        if buffer is not None:
            text += "\n\n## Buffered submission override\nUse submit_edits for all graph edits; do not include them in the finish response. StageCompletion only acknowledges the saved proposal. Candidate tools show the last validated buffer state.\n"
        payload = pack_payload(payload)
        body = prompt_json(payload)
        from cortex.infrastructure.artifacts.graph_merge_trace import (
            FileSystemGraphMergeTraceSink,
        )

        call_sink = (
            FileSystemGraphMergeTraceSink(
                self.trace_sink.path.with_name("model_calls.jsonl")
            )
            if hasattr(self.trace_sink, "path")
            else None
        )
        counter = EpisodeTrace(
            call_sink, phase, budget=getattr(self, "call_budget", None)
        )
        agent = create_agent(
            self.llm
            if phase in {"consolidate", "reconcile", "node_assess"}
            and (getattr(self.llm, "extra_body", None) or {})
            .get("thinking", {})
            .get("type")
            == "disabled"
            else _AutomaticToolChoice(self.llm),
            tools=build_tools(
                context, visible_source_ids={n["id"] for n in payload.get("nodes", [])}
            ),
            system_prompt=text,
            response_format=ToolStrategy(output_model),
            middleware=[
                require_output,
                bounded_reads,
                enforce_read_budget,
                ModelCallLimitMiddleware(
                    run_limit=self.max_calls, exit_behavior="error"
                ),
                ToolCallLimitMiddleware(
                    run_limit=self.max_calls * 2, exit_behavior="error"
                ),
            ],
            name=f"graph_integration_{phase}",
        )
        event = {
            "component": f"integration_{phase}",
            "prompt_sha256": sha256(text.encode()).hexdigest(),
            "system_characters": len(text),
            "payload_characters": len(body),
            "payload": payload,
        }
        result = None
        try:
            result = agent.invoke(
                {"messages": [HumanMessage(content=body)]},
                config={
                    "callbacks": [counter],
                    "recursion_limit": self.max_calls * 8 + 10,
                },
            )
            output = result.get("structured_response")
            if output is None:
                raise ValueError("Agent returned no structured decision")
            event["parsed_response"] = output.model_dump(mode="json")
            if buffer is not None and output.verdict == "complete" and not buffer.valid:
                raise ValueError(
                    "Finish requires a validated pending proposal; repair submit_edits errors first"
                )
            return output
        except Exception as exc:
            event["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            event["model_calls"] = counter.calls
            event["tools"] = list(context.tool_log)
            event["messages"] = [
                m.model_dump(mode="json")
                for m in (
                    result.get("messages", ()) if result is not None else counter.inputs
                )
            ]
            if result is None:
                event["messages"] = [
                    m.model_dump(mode="json") for m in counter.responses
                ]
            if self.trace_sink:
                self.trace_sink.record(event)
