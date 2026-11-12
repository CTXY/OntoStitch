from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    after_model,
)
from langchain.agents.structured_output import ToolStrategy
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables.config import merge_configs
from pydantic import PrivateAttr, model_validator

from cortex.capabilities.graph_merge.llm.prompts import load_prompt, prompt_fingerprint
from cortex.capabilities.graph_merge.llm.references import _RefCodec
from cortex.capabilities.graph_merge.llm.tools import build_read_tools
from cortex.capabilities.graph_merge.models import (
    CrossTypeCandidate,
    CrossTypeResolution,
    GroupIdentityView,
    NodeIdentitySelection,
    NodeIdentityView,
)
from cortex.capabilities.graph_merge.reconciliation.models import (
    ConsolidationProposal,
    ConsolidationReview,
)
from cortex.capabilities.graph_merge.reconciliation.validation import source_checklist
from cortex.infrastructure.llm.parsing import run_llm_extraction_sync


class GraphMergeAgentTraceSink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...


class _Agent:
    """Small structured calls for identity matching; no reconciliation logic."""

    def __init__(self, llm: BaseChatModel, trace_sink=None):
        self.llm, self.trace_sink = llm, trace_sink

    def _run(self, name, output_model, payload, *, ontology_asset_type=None):
        codec = _RefCodec.from_payload(payload)
        trace = {}
        result = None
        try:
            result = run_llm_extraction_sync(
                llm=self.llm,
                prompt=load_prompt(name),
                output_model=output_model,
                human_payload=codec.encode(payload),
                trace_capture=trace,
                repair_with_context=name == "identity",
            )
            return codec.decode_model(result, output_model)
        finally:
            if self.trace_sink is not None:
                self.trace_sink.record(
                    {
                        "component": name,
                        "prompt_sha256": prompt_fingerprint(name),
                        "request": trace.get("prompt"),
                        "response": trace.get("response"),
                        "repair_response": trace.get("repair_response"),
                        "parsed_response": result.model_dump(mode="json")
                        if result
                        else None,
                    }
                )


class LLMSemanticIdentityResolver(_Agent):
    def resolve(
        self,
        seed: NodeIdentityView,
        collection_candidates: tuple[GroupIdentityView, ...],
        query_candidates: tuple[NodeIdentityView, ...],
        policy,
    ) -> NodeIdentitySelection:
        entry_ids = {item.entry_id for item in collection_candidates}
        query_ids = {item.query_id for item in query_candidates}

        class CandidateIdentitySelection(NodeIdentitySelection):
            @model_validator(mode="after")
            def validate_selection(self):
                self.validate_candidates(entry_ids, query_ids)
                return self

        payload = {
            "identity_policy": policy.model_dump(mode="json"),
            "seed": seed.model_dump(mode="json"),
            "collection_candidates": [
                item.model_dump(mode="json") for item in collection_candidates
            ],
            "query_candidates": [
                item.model_dump(mode="json") for item in query_candidates
            ],
            # Keep the comparison subject next to the end of a potentially long payload. This is
            # deliberately redundant data, not another semantic signal.
            "decision_anchor": {
                "query_id": seed.query_id,
                "node_type": seed.node_type,
                "identity_fields": seed.representation.identity_fields,
                "instruction": (
                    "Compare every verdict to this seed; never substitute a candidate as the seed."
                ),
            },
        }
        return self._run(
            "identity",
            CandidateIdentitySelection,
            payload,
            ontology_asset_type=seed.node_type,
        )


class LLMCrossTypeAssetResolver(_Agent):
    def resolve(
        self, candidates: tuple[CrossTypeCandidate, ...]
    ) -> CrossTypeResolution:
        return self._run(
            "cross_type_identity",
            CrossTypeResolution,
            {"candidates": [item.model_dump(mode="json") for item in candidates]},
            ontology_asset_type="concept",
        )


@dataclass(frozen=True)
class RoleResult:
    output: ConsolidationProposal | ConsolidationReview
    calls: int


class RoleInvocationError(RuntimeError):
    def __init__(self, message: str, calls: int):
        super().__init__(message)
        self.calls = calls


class _AutomaticToolChoice(BaseChatModel):
    """Keep LangChain ToolStrategy while permitting reasoning providers' auto tool mode."""

    _model: BaseChatModel = PrivateAttr()

    def __init__(self, model):
        super().__init__(cache=False)
        self._model = model

    @property
    def _llm_type(self):
        return "merge-automatic-tool-choice"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self._model.bind_tools(tools, tool_choice="auto", **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._model._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


class _CallCounter(BaseCallbackHandler):
    def __init__(self):
        self.calls = 0

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.calls += 1


class MergeRole:
    """A bounded LangChain agent; the outer graph owns semantic revision."""

    def __init__(self, llm, role: str, trace_sink=None):
        self.llm, self.role, self.trace_sink = llm, role, trace_sink

    def invoke(
        self, context, *, proposal=None, feedback=None, remaining_calls=18, config=None
    ):
        output_model = (
            ConsolidationProposal if self.role == "consolidate" else ConsolidationReview
        )
        payload = context.payload()
        if self.role == "consolidate":
            payload["revision"] = {
                "previous_proposal": proposal,
                "feedback": feedback,
                "remaining_calls": remaining_calls,
            }
        else:
            payload["proposal"] = proposal
            pairs = source_checklist(
                ConsolidationProposal.model_validate(proposal),
                context.evidence_by_ref(),
            )
            payload["source_checklist"] = [
                {
                    "supporting_ref": ref.model_dump(mode="json"),
                    "evidence_id": evidence_id,
                }
                for ref, evidence_id in sorted(
                    pairs, key=lambda item: (item[0].origin, item[0].ref_id, item[1])
                )
            ]
            if feedback:
                payload["format_feedback"] = feedback

        @after_model(can_jump_to=["model"])
        def require_structured_response(state, runtime):
            last = state["messages"][-1]
            if state.get("structured_response") is None and not getattr(
                last, "tool_calls", ()
            ):
                invalid = [
                    {
                        "name": item.get("name"),
                        "error": str(item.get("error", ""))[-1000:],
                    }
                    for item in getattr(last, "invalid_tool_calls", ())
                ]
                return {
                    "messages": [
                        HumanMessage(
                            content=f"Complete this role by calling the {output_model.__name__} structured output tool. Plain text is not a submitted result. Preserve the evidence and proposal already read. Return valid JSON arguments; apostrophes inside JSON strings do not need backslash escaping. Invalid previous tool calls: {json.dumps(invalid, ensure_ascii=False)}"
                        )
                    ],
                    "jump_to": "model",
                }
            return None

        agent = create_agent(
            _AutomaticToolChoice(self.llm),
            tools=build_read_tools(context),
            system_prompt=load_prompt(self.role, context.group.node_type),
            response_format=ToolStrategy(output_model, handle_errors=False),
            middleware=[
                require_structured_response,
                ModelCallLimitMiddleware(
                    run_limit=remaining_calls,
                    thread_limit=remaining_calls,
                    exit_behavior="error",
                ),
                ToolCallLimitMiddleware(
                    run_limit=remaining_calls, exit_behavior="error"
                ),
            ],
            name=f"merge_{self.role}",
        )
        counter = _CallCounter()
        invocation_config = merge_configs(config or {}, {"callbacks": [counter]})
        invocation_config["recursion_limit"] = max(12, remaining_calls * 5)
        invocation_config["metadata"] = {
            **invocation_config.get("metadata", {}),
            "prompt_fingerprint": prompt_fingerprint(
                self.role, context.group.node_type
            ),
            "group_id": context.group.group_id,
            "role": self.role,
        }
        try:
            result = agent.invoke(
                {
                    "messages": [
                        HumanMessage(
                            content="<merge_context>\n"
                            + json.dumps(payload, ensure_ascii=False)
                            + "\n</merge_context>"
                        )
                    ]
                },
                config=invocation_config,
            )
            output = result.get("structured_response")
            if output is None:
                raise ValueError("Agent did not return its structured output")
        except Exception as exc:
            raise RoleInvocationError(
                f"{type(exc).__name__}: {exc}", max(1, counter.calls)
            ) from exc
        if self.trace_sink is not None:
            self.trace_sink.record(
                {
                    "component": self.role,
                    "group_id": context.group.group_id,
                    "prompt_sha256": prompt_fingerprint(
                        self.role, context.group.node_type
                    ),
                    "parsed_response": output.model_dump(mode="json"),
                    "model_calls": counter.calls,
                }
            )
        return RoleResult(output=output, calls=max(1, counter.calls))


class LLMGroupConsolidator(MergeRole):
    def __init__(self, llm, trace_sink=None):
        super().__init__(llm, "consolidate", trace_sink)


class LLMConsolidationChecker(MergeRole):
    def __init__(self, llm, trace_sink=None):
        super().__init__(llm, "assess", trace_sink)
