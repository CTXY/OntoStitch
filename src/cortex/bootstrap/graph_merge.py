from __future__ import annotations

from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel

from cortex.capabilities.graph_merge.identity import (
    CrossTypeAssetResolver,
    IdentityGrouper,
    SemanticIdentityResolver,
    build_default_identity_implementation_registry,
)
from cortex.capabilities.graph_merge.llm import (
    LLMConflictBranchSelector,
    LLMCostSensitiveNodeReconciler,
    LLMCrossTypeAssetResolver,
    LLMFactFuser,
    LLMGeneralizationCoverageVerifier,
    LLMKnowledgeGeneralizer,
    LLMNodeReconciler,
    LLMSemanticIdentityResolver,
)
from cortex.capabilities.graph_merge.llm.agents import (
    GraphMergeAgentTraceSink,
    LLMConsolidationChecker,
    LLMGroupConsolidator,
)
from cortex.capabilities.graph_merge.mining import BaseGraphMiner
from cortex.capabilities.graph_merge.normalization import (
    build_default_node_group_normalizer_registry,
)
from cortex.capabilities.graph_merge.operator import GraphMergeOperator
from cortex.capabilities.graph_merge.reconciliation import (
    CachedEvidenceResolver,
    ConflictBranchSelector,
    EvidenceResolver,
    GenerationRuleReconciliationSemanticsValidator,
    MetricReconciliationSemanticsValidator,
    NodeGroupReconciler,
    NodeReconciler,
    NodeReconcilerRegistry,
)
from cortex.capabilities.graph_merge.reconciliation.fusion import FactFuser, FactFuserRegistry
from cortex.capabilities.graph_merge.reconciliation.generalization import (
    GeneralizationCoverageVerifier,
    KnowledgeGeneralizer,
    KnowledgeGeneralizerRegistry,
)
from cortex.capabilities.graph_merge.rewrite import GraphRewriteApplier
from cortex.config import CortexConfig, load_config
from cortex.infrastructure.llm import create_chat_model, create_embedding_model


def build_graph_merge_operator(
    config_path: str | Path | CortexConfig = "configs/cortex.yaml",
    *,
    identity_resolver: SemanticIdentityResolver | None = None,
    cross_type_resolver: CrossTypeAssetResolver | None = None,
    node_reconciler: NodeReconciler | None = None,
    reconciler_registry: NodeReconcilerRegistry | None = None,
    evidence_resolver: EvidenceResolver | None = None,
    branch_selector: ConflictBranchSelector | None = None,
    generalizer: KnowledgeGeneralizer | None = None,
    fact_fuser: FactFuser | None = None,
    fact_fuser_registry: FactFuserRegistry | None = None,
    generalizer_registry: KnowledgeGeneralizerRegistry | None = None,
    generalization_verifier: GeneralizationCoverageVerifier | None = None,
    trace_sink: GraphMergeAgentTraceSink | None = None,
    live_semantics: bool = True,
    llm: BaseChatModel | None = None,
    graph_miner: BaseGraphMiner | None = None,
    rewrite_applier: GraphRewriteApplier | None = None,
    group_consolidator=None,
    consolidation_checker=None,
) -> GraphMergeOperator:
    """Assemble the merge capability from typed config and injected semantic ports."""

    root_config = config_path if isinstance(config_path, CortexConfig) else load_config(config_path)
    config = root_config.services.graph_merge
    custom_legacy = any(
        port is not None
        for port in (
            node_reconciler,
            reconciler_registry,
            generalizer,
            fact_fuser,
            fact_fuser_registry,
            generalizer_registry,
            generalization_verifier,
        )
    )
    if (group_consolidator is None) != (consolidation_checker is None):
        raise ValueError("Group consolidator and checker must be supplied together")
    embeddings = (
        create_embedding_model(
            root_config.llm,
            model=root_config.embedding_model,
        )
        if live_semantics
        else None
    )
    if live_semantics:
        semantic_llm = llm or create_chat_model(root_config.llm, profile="graph_merge")
        if group_consolidator is None and not custom_legacy:
            group_consolidator = LLMGroupConsolidator(semantic_llm, trace_sink)
            consolidation_checker = LLMConsolidationChecker(semantic_llm, trace_sink)
        identity_resolver = identity_resolver or LLMSemanticIdentityResolver(
            semantic_llm, trace_sink
        )
        cross_type_resolver = cross_type_resolver or LLMCrossTypeAssetResolver(
            semantic_llm, trace_sink
        )
        supplied_reconciler = node_reconciler is not None
        node_reconciler = node_reconciler or LLMCostSensitiveNodeReconciler(
            semantic_llm, trace_sink
        )
        if reconciler_registry is None:
            registrations = {}
            if not supplied_reconciler:
                registrations = {
                    "concept": (
                        frozenset({"targets"}),
                        LLMCostSensitiveNodeReconciler(
                            semantic_llm, trace_sink, prompt_variant="concept"
                        ),
                        None,
                    ),
                    "metric": (
                        frozenset({"expression"}),
                        LLMNodeReconciler(semantic_llm, trace_sink, prompt_variant="metric"),
                        MetricReconciliationSemanticsValidator(),
                    ),
                    "generation_rule": (
                        frozenset({"triggers"}),
                        LLMCostSensitiveNodeReconciler(
                            semantic_llm, trace_sink, prompt_variant="generation_rule"
                        ),
                        GenerationRuleReconciliationSemanticsValidator(),
                    ),
                }
            reconciler_registry = NodeReconcilerRegistry(node_reconciler, registrations)
        generalizer = generalizer or LLMKnowledgeGeneralizer(semantic_llm, trace_sink)
        fact_fuser = fact_fuser or LLMFactFuser(semantic_llm, trace_sink)
        fact_fuser_registry = fact_fuser_registry or FactFuserRegistry(
            fact_fuser,
            {
                "concept": (
                    frozenset({"targets"}),
                    LLMFactFuser(semantic_llm, trace_sink, prompt_variant="concept"),
                )
            },
        )
        generalizer_registry = generalizer_registry or KnowledgeGeneralizerRegistry(
            generalizer,
            {
                "metric": (
                    frozenset({"expression"}),
                    LLMKnowledgeGeneralizer(semantic_llm, trace_sink, prompt_variant="metric"),
                ),
                "generation_rule": (
                    frozenset({"triggers"}),
                    LLMKnowledgeGeneralizer(
                        semantic_llm, trace_sink, prompt_variant="generation_rule"
                    ),
                ),
            },
        )
        generalization_verifier = generalization_verifier or LLMGeneralizationCoverageVerifier(
            semantic_llm, trace_sink
        )
        branch_selector = branch_selector or LLMConflictBranchSelector(semantic_llm, trace_sink)
        if rewrite_applier is None:
            rewrite_applier = GraphRewriteApplier(allow_grounding_removal=True)
    cached_evidence = CachedEvidenceResolver(evidence_resolver) if evidence_resolver else None
    return GraphMergeOperator(
        identity_grouper=IdentityGrouper(
            identity_resolver,
            evidence_resolver=cached_evidence,
            trace_sink=trace_sink,
            candidate_top_k=config.candidate_top_k,
            implementation_registry=build_default_identity_implementation_registry(),
            cross_type_resolver=cross_type_resolver,
            embeddings=embeddings,
        ),
        node_reconciler=NodeGroupReconciler(
            group_consolidator=group_consolidator,
            consolidation_checker=consolidation_checker,
            consolidation_max_rounds=config.consolidation_max_rounds,
            consolidation_max_calls=config.consolidation_max_calls,
            trace_sink=trace_sink,
            reconciler=node_reconciler,
            reconciler_registry=reconciler_registry,
            evidence_resolver=cached_evidence,
            branch_selector=branch_selector,
            generalizer=generalizer,
            fact_fuser=fact_fuser,
            fact_fuser_registry=fact_fuser_registry,
            generalizer_registry=generalizer_registry,
            generalization_verifier=generalization_verifier,
        ),
        graph_miner=graph_miner,
        rewrite_applier=rewrite_applier,
        normalizer_registry=build_default_node_group_normalizer_registry(),
    )
