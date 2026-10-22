from __future__ import annotations

from typing import Any, Protocol

from langchain_core.language_models.chat_models import BaseChatModel


class LLMProvider(Protocol):
    def extract_assets(
        self,
        record: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[Any]: ...


class NodeLLMProvider:
    def __init__(self, llm: BaseChatModel):
        self.client = llm
        self.supports_join_edge_extraction = callable(getattr(llm, "invoke", None))

    def extract_assets(
        self,
        record: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[dict]:
        context = context or {}
        prompt = context.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("LLM extraction context requires a non-empty prompt")
        prompt_name = context.get("prompt_name")
        if prompt_name == "gold_join_edges":
            from cortex.capabilities.sql_training.extractors.gold_join_edges import (
                extract_gold_join_proposals,
            )

            return extract_gold_join_proposals(
                self.client, record, prompt, trace_capture=context.get("trace_capture")
            )
        if prompt_name in {
            "error_analysis_minimal_repair_action",
            "error_analysis_minimal_repair_ablation",
            "error_analysis_minimal_repair_joint_ablation",
            "error_analysis_minimal_repair_review",
        }:
            from cortex.capabilities.sql_training.error_analysis.agent import (
                extract_error_analysis_llm_payloads,
            )

            trace_capture = context.get("trace_capture")
            if trace_capture is None:
                return extract_error_analysis_llm_payloads(self.client, record, prompt, prompt_name)
            return extract_error_analysis_llm_payloads(
                self.client,
                record,
                prompt,
                prompt_name,
                trace_capture=trace_capture,
            )
        if prompt_name == "error_analysis_execution_failure":
            from cortex.capabilities.sql_training.error_analysis.execution_failure import (
                extract_execution_failure_analysis,
            )

            return extract_execution_failure_analysis(
                self.client,
                record,
                prompt,
                trace_capture=context.get("trace_capture"),
            )
        raise ValueError(f"Unsupported extraction prompt: {prompt_name}")


def build_node_llm_provider(llm: BaseChatModel | None) -> LLMProvider | None:
    """Wrap a Bootstrap-created model without constructing a provider in the Capability."""

    return NodeLLMProvider(llm) if llm is not None else None
