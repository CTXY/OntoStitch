from __future__ import annotations

from contextlib import nullcontext
from functools import partial
from pathlib import Path

from langchain_core.language_models import BaseChatModel

from cortex.application.merge_graph import GraphMergeUseCase
from cortex.capabilities.graph_merge.identity import (
    CrossTypeAssetResolver,
    IdentityGrouper,
    SemanticIdentityResolver,
    build_default_identity_implementation_registry,
)
from cortex.capabilities.graph_merge.llm import (
    LLMConsolidationChecker,
    LLMGroupConsolidator,
    LLMSemanticIdentityResolver,
)
from cortex.capabilities.graph_merge.llm.agents import GraphMergeAgentTraceSink
from cortex.capabilities.graph_merge.llm.prompts import prompt_version
from cortex.capabilities.graph_merge.normalization import (
    build_default_node_group_normalizer_registry,
)
from cortex.capabilities.graph_merge.operator import GraphMergeOperator
from cortex.capabilities.graph_merge.reconciliation import (
    CachedEvidenceResolver,
    EvidenceResolver,
    NodeGroupReconciler,
)
from cortex.config import CortexConfig, config_fingerprint, load_config
from cortex.infrastructure.llm import create_chat_model, create_embedding_model
from cortex.infrastructure.workflow.checkpoints import persistent_checkpointer


def build_graph_merge_operator(
    config_path: str | Path | CortexConfig = "configs/cortex.yaml",
    *,
    identity_resolver: SemanticIdentityResolver | None = None,
    cross_type_resolver: CrossTypeAssetResolver | None = None,
    evidence_resolver: EvidenceResolver | None = None,
    trace_sink: GraphMergeAgentTraceSink | None = None,
    live_semantics: bool = True,
    llm: BaseChatModel | None = None,
    group_consolidator=None,
    consolidation_checker=None,
    checkpointer=None,
    integration_agent=None,
) -> GraphMergeUseCase:
    """One semantic path; production persists through the official SQLite saver.

    Tests may inject an in-memory saver. Offline mode preserves ambiguous originals
    without model calls and does not require a database connection.
    """
    root = (
        config_path
        if isinstance(config_path, CortexConfig)
        else load_config(config_path)
    )
    config = root.services.graph_merge
    # The production path integrates explicit graph batches. Legacy role injection
    # remains available for old ablations/tests, never silently used as fallback.
    if integration_agent is not None or (
        live_semantics
        and identity_resolver is None
        and group_consolidator is None
        and consolidation_checker is None
        and cross_type_resolver is None
    ):
        from cortex.capabilities.graph_merge.integration.incremental import (
            IncrementalGraphIntegrationEngine as GraphIntegrationEngine,
        )
        from cortex.capabilities.graph_merge.llm.incremental import (
            IncrementalIntegrationAgent as IntegrationAgent,
        )

        factory = (
            (lambda: nullcontext(checkpointer))
            if checkpointer is not None
            else partial(persistent_checkpointer, config.checkpoint_url)
        )
        engine = GraphIntegrationEngine(
            integration_agent
            or IntegrationAgent(
                llm or create_chat_model(root.llm, profile="graph_merge"),
                max_calls=config.consolidation_max_calls,
                trace_sink=trace_sink,
            ),
            evidence_resolver=CachedEvidenceResolver(evidence_resolver)
            if evidence_resolver
            else None,
            checkpoint_factory=factory,
            max_rounds=config.consolidation_max_rounds,
            top_k=config.candidate_top_k,
            runtime_signature=config_fingerprint(root),
            sql_dialect=root.services.sql_training.validation.dialect,
            model_call_budget=config.merge_model_call_budget,
        )
        checkpoint_url = config.checkpoint_url.get_secret_value()
        if checkpointer is None and checkpoint_url.startswith("sqlite:///"):
            engine.workspace_path = checkpoint_url.removeprefix("sqlite:///")
        return engine
    if (group_consolidator is None) != (consolidation_checker is None):
        raise ValueError("Consolidator and Assessment must be supplied together")
    embeddings = None
    if live_semantics:
        model = llm or create_chat_model(root.llm, profile="graph_merge")
        identity_resolver = identity_resolver or LLMSemanticIdentityResolver(
            model, trace_sink
        )
        group_consolidator = group_consolidator or LLMGroupConsolidator(
            model, trace_sink
        )
        consolidation_checker = consolidation_checker or LLMConsolidationChecker(
            model, trace_sink
        )
        embeddings = create_embedding_model(root.llm, model=root.embedding_model)
    cached = CachedEvidenceResolver(evidence_resolver) if evidence_resolver else None
    checkpoint_factory = (
        (lambda: nullcontext(checkpointer))
        if checkpointer is not None
        else (
            partial(persistent_checkpointer, config.checkpoint_url)
            if live_semantics
            else None
        )
    )
    return GraphMergeOperator(
        identity_grouper=IdentityGrouper(
            identity_resolver,
            evidence_resolver=cached,
            trace_sink=trace_sink,
            candidate_top_k=config.candidate_top_k,
            implementation_registry=build_default_identity_implementation_registry(),
            cross_type_resolver=cross_type_resolver,
            embeddings=embeddings,
        ),
        node_reconciler=NodeGroupReconciler(
            group_consolidator=group_consolidator,
            consolidation_checker=consolidation_checker,
            evidence_resolver=cached,
            consolidation_max_rounds=config.consolidation_max_rounds,
            consolidation_max_calls=config.consolidation_max_calls,
            max_evidence_items=config.max_evidence_items,
            max_evidence_characters=config.max_evidence_characters,
            trace_sink=trace_sink,
        ),
        checkpoint_factory=checkpoint_factory,
        workflow_version=f"merge-v3-transactions-auto-tools:{config_fingerprint(root)}:{prompt_version()}",
        normalizer_registry=build_default_node_group_normalizer_registry(),
    )
