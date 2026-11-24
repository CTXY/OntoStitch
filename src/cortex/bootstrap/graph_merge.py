"""Construct the single supported incremental Graph Merge workflow."""
from __future__ import annotations

from pathlib import Path

from langchain_core.language_models import BaseChatModel

from cortex.application.merge_graph import GraphMergeUseCase
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.evidence import CachedEvidenceResolver, EvidenceResolver
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.capabilities.graph_merge.llm.trace import GraphMergeAgentTraceSink
from cortex.config import CortexConfig, load_config
from cortex.infrastructure.llm import create_chat_model


def build_graph_merge_operator(
    config_path: str | Path | CortexConfig = "configs/cortex.yaml",
    *,
    evidence_resolver: EvidenceResolver | None = None,
    trace_sink: GraphMergeAgentTraceSink | None = None,
    llm: BaseChatModel | None = None,
    integration_agent=None,
    workspace_path: str | Path | None = None,
) -> GraphMergeUseCase:
    """Build the production workflow, or inject its current protocol for tests.

    Production uses the configured SQLite workspace. An injected agent defaults
    to an in-memory run unless an explicit workspace path is supplied.
    """
    root = config_path if isinstance(config_path, CortexConfig) else load_config(config_path)
    config = root.services.graph_merge
    if workspace_path is None and integration_agent is None:
        checkpoint_url = config.checkpoint_url.get_secret_value()
        if checkpoint_url.startswith("sqlite:///"):
            workspace_path = checkpoint_url.removeprefix("sqlite:///")
    return IncrementalGraphIntegrationEngine(
        integration_agent or IncrementalIntegrationAgent(
            llm or create_chat_model(root.llm, profile="graph_merge"),
            max_calls=config.consolidation_max_calls,
            trace_sink=trace_sink,
            request_max_characters=config.request_max_characters,
        ),
        evidence_resolver=CachedEvidenceResolver(evidence_resolver) if evidence_resolver else None,
        workspace_path=workspace_path,
        max_rounds=config.consolidation_max_rounds,
        top_k=config.candidate_top_k,
        sql_dialect=root.services.sql_training.validation.dialect,
        model_call_budget=config.merge_model_call_budget,
        node_context_max_characters=config.node_context_max_characters,
    )
