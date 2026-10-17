from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable
from typing import Protocol

from cortex.capabilities.graph_merge.identity import exact_identity_key
from cortex.capabilities.graph_merge.models import (
    BranchSelection,
    DifferencePoint,
    EvidenceItem,
    FinalNodeGroup,
    NodeContent,
    NodeRepresentation,
    ReconciledNode,
    ReconciledRepresentation,
    ReconcileOutcome,
    ReconciliationGroup,
    RepresentationRef,
)
from cortex.capabilities.graph_merge.reconciliation.consolidation import (
    ConsolidationMemory,
    active_memory,
    fingerprint,
    invoke,
)
from cortex.capabilities.graph_merge.reconciliation.deterministic_fusion import (
    FuseError,
    NodeFuserRegistry,
    build_default_node_fuser_registry,
)
from cortex.capabilities.graph_merge.reconciliation.fusion import (
    FactFuser,
    FactFuserRegistry,
    fuse_if_eligible,
)
from cortex.capabilities.graph_merge.reconciliation.generalization import (
    GeneralizationCoverageVerifier,
    KnowledgeGeneralizer,
    KnowledgeGeneralizerRegistry,
    generalize_if_eligible,
)
from cortex.capabilities.graph_merge.reconciliation.strategy import (
    NodeReconciler,
    NodeReconcilerRegistry,
    ReconciliationSemanticsValidator,
)
from cortex.schema import (
    ConflictAlternative,
    ConflictChoice,
    ConflictPoint,
    GraphNode,
    IdentityMode,
    IdentityPolicy,
    MergeRole,
    MergeStatus,
    NodeMergeConflict,
    NodeType,
    SchemaDefinition,
    SourceReference,
)


class EvidenceResolver(Protocol):
    def resolve(
        self, sources: tuple[SourceReference, ...]
    ) -> Awaitable[tuple[EvidenceItem, ...]]: ...


class CachedEvidenceResolver:
    """Merge-run scoped source cache shared by identity and reconciliation."""

    def __init__(self, resolver: EvidenceResolver):
        self.resolver = resolver
        self._cache: dict[SourceReference, EvidenceItem] = {}

    async def resolve(self, sources: tuple[SourceReference, ...]) -> tuple[EvidenceItem, ...]:
        missing = tuple(source for source in sources if source not in self._cache)
        if missing:
            resolved = await self.resolver.resolve(missing)
            self._cache.update({item.source: item for item in resolved})
        return tuple(self._cache[source] for source in sources if source in self._cache)


class ConflictBranchSelector(Protocol):
    def select(
        self,
        incoming: NodeRepresentation,
        alternatives: tuple[NodeRepresentation, ...],
        evidence: tuple[EvidenceItem, ...],
        node_type: NodeType,
        identity_policy: IdentityPolicy,
    ) -> Awaitable[BranchSelection]: ...


class CanonicalIdFactory(Protocol):
    def new_node_id(self, group: FinalNodeGroup) -> str: ...

    def new_alternative_id(
        self, node_id: str, supporting_refs: tuple[RepresentationRef, ...]
    ) -> str: ...


class StableCanonicalIdFactory:
    def new_node_id(self, group: FinalNodeGroup) -> str:
        refs = ",".join(sorted(member.ref.ref_id for member in group.members))
        digest = hashlib.sha256(f"{group.node_type}:{refs}".encode()).hexdigest()[:20]
        return f"{group.node_type}.canonical.{digest}"

    def new_alternative_id(
        self, node_id: str, supporting_refs: tuple[RepresentationRef, ...]
    ) -> str:
        refs = ",".join(sorted(f"{ref.origin}:{ref.ref_id}" for ref in supporting_refs))
        digest = hashlib.sha256(f"{node_id}:{refs}".encode()).hexdigest()[:20]
        return f"alternative.{digest}"


class NodeGroupReconciler:
    def __init__(
        self,
        *,
        reconciler: NodeReconciler | None,
        evidence_resolver: EvidenceResolver | None,
        branch_selector: ConflictBranchSelector | None,
        generalizer: KnowledgeGeneralizer | None,
        fact_fuser: FactFuser | None = None,
        generalization_verifier: GeneralizationCoverageVerifier | None = None,
        id_factory: CanonicalIdFactory | None = None,
        fuser_registry: NodeFuserRegistry | None = None,
        fact_fuser_registry: FactFuserRegistry | None = None,
        generalizer_registry: KnowledgeGeneralizerRegistry | None = None,
        reconciler_registry: NodeReconcilerRegistry | None = None,
        consolidation_max_rounds: int = 3,
        consolidation_max_calls: int = 18,
        trace_sink=None,
        group_consolidator=None,
        consolidation_checker=None,
    ) -> None:
        if consolidation_max_rounds < 1 or consolidation_max_calls < 1:
            raise ValueError("Consolidation budgets must be positive")
        self.consolidation_max_rounds = consolidation_max_rounds
        self.consolidation_max_calls = consolidation_max_calls
        self.trace_sink = trace_sink
        if (group_consolidator is None) != (consolidation_checker is None):
            raise ValueError("Group consolidator and checker must be supplied together")
        self.group_consolidator = group_consolidator
        self.consolidation_checker = consolidation_checker
        self.reconciler = reconciler
        self.evidence_resolver = evidence_resolver
        self.branch_selector = branch_selector
        self.generalizer = generalizer
        self.generalization_verifier = generalization_verifier
        self.id_factory = id_factory or StableCanonicalIdFactory()
        self.fuser_registry = fuser_registry or build_default_node_fuser_registry()
        self.fact_fuser_registry = fact_fuser_registry or FactFuserRegistry(fact_fuser)
        self.generalizer_registry = generalizer_registry or KnowledgeGeneralizerRegistry(
            generalizer
        )
        self.reconciler_registry = reconciler_registry or NodeReconcilerRegistry(reconciler)

    def canonical_id_for_group(self, group: FinalNodeGroup) -> str:
        """Resolve the canonical ID before content reconciliation.

        A repeated authoring identity can carry revised content and therefore miss exact-content
        grouping. Exposing the stable ID lets the operator reunite that provisional group with its
        existing canonical node before reconciliation instead of constructing duplicate node IDs.
        """

        return group.existing_node_id or self.id_factory.new_node_id(group)

    async def reconcile_group(
        self,
        group,
        *,
        schema,
        historical_conflict=None,
        dependency_nodes=(),
        dependency_conflicts=(),
    ):
        memory = ConsolidationMemory(self.consolidation_max_rounds, self.consolidation_max_calls)
        token = active_memory.set(memory)
        try:
            result = await self._reconcile_group(
                group,
                schema=schema,
                historical_conflict=historical_conflict,
                dependency_nodes=dependency_nodes,
                dependency_conflicts=dependency_conflicts,
            )
            if not memory.stop_reason:
                memory.stop_reason = "accepted_or_preserved"
            updates = {"consolidation": memory.payload()}
            if result.conflict is not None:
                updates["conflict"] = result.conflict.model_copy(
                    update={"consolidation": memory.audit()}
                )
            result = result.model_copy(update=updates)
            if self.trace_sink is not None:
                self.trace_sink.record(
                    {"component": "consolidation", "group_id": group.group_id, **memory.payload()}
                )
            return result
        finally:
            active_memory.reset(token)

    async def _reconcile_group(
        self,
        group: FinalNodeGroup,
        *,
        schema: SchemaDefinition,
        historical_conflict: NodeMergeConflict | None = None,
        dependency_nodes: tuple[GraphNode, ...] = (),
        dependency_conflicts=(),
    ) -> ReconciledNode:
        node_type = schema.node_type_index()[group.node_type]
        canonical_id = self.canonical_id_for_group(group)
        if node_type.identity_policy.mode == IdentityMode.EXACT:
            incoming = [member for member in group.members if member.ref.origin == "incoming_node"]
            existing = [member for member in group.members if member.ref.origin == "existing_node"]
            if (
                len(incoming) == 1
                and existing
                and any(
                    exact_identity_key(member.content, node_type.identity_policy)
                    != exact_identity_key(incoming[0].content, node_type.identity_policy)
                    for member in existing
                )
            ):
                revision = incoming[0]
                return ReconciledNode(
                    node=_materialize_node(
                        canonical_id,
                        group.node_type,
                        revision.content,
                        revision.sources,
                    ),
                    representations=group.members,
                    evidence_by_ref={},
                )
        evidence_by_ref: dict[RepresentationRef, tuple[EvidenceItem, ...]] = {
            member.ref: () for member in group.members
        }
        evidence: tuple[EvidenceItem, ...] = ()
        diagnostics: list[str] = []
        memory = active_memory.get()
        if historical_conflict is not None:
            # Re-open all complete alternatives; a prior branch assignment is not evidence.
            memory.history.append(
                {
                    "historical_conflict": historical_conflict.model_dump(
                        mode="json", exclude={"consolidation"}
                    )
                }
            )
            group = group.model_copy(
                update={
                    "members": (
                        *(
                            _alternative_rep(group, item)
                            for item in historical_conflict.alternatives
                        ),
                        *(item for item in group.members if item.ref.origin == "incoming_node"),
                    )
                }
            )
        if self.group_consolidator is not None and (
            group.node_type in {"metric", "generation_rule"}
            or (
                node_type.identity_policy.mode != IdentityMode.EXACT
                and self._simple_outcome(group, node_type, schema) is None
            )
        ):
            return await self._consolidate_proposal(
                group,
                canonical_id=canonical_id,
                schema=schema,
                dependency_nodes=dependency_nodes,
                dependency_conflicts=dependency_conflicts,
                historical_conflict=historical_conflict,
            )
        analysis = self._simple_outcome(group, node_type, schema)
        simple = analysis is not None
        if not simple:
            evidence_by_ref, evidence, evidence_code = await self._evidence(group.members)
            if evidence_code:
                diagnostics.append(evidence_code)
        memory.context = {
            "members": [item.model_dump(mode="json") for item in group.members],
            "evidence_by_ref": [
                {
                    "ref": ref.model_dump(mode="json"),
                    "evidence": [item.model_dump(mode="json") for item in items],
                }
                for ref, items in evidence_by_ref.items()
            ],
            "dependency_nodes": [item.model_dump(mode="json") for item in dependency_nodes],
        }
        previous_failure = None
        pending_feedback = []
        for round_index in range(1, self.consolidation_max_rounds + 1):
            memory.round = round_index
            # A local candidate defect keeps the checked analysis; semantic feedback reopens it.
            old_feedback = len(memory.feedback)
            reanalyze = analysis is None or any(
                item["kind"] != "candidate_defect" and item.get("target") != "candidate"
                for item in pending_feedback
            )
            if not simple and reanalyze:
                analysis = await self._semantic_outcome(
                    group, evidence, node_type, diagnostics, schema, dependency_nodes
                )
            memory.analysis = analysis
            outcomes = await self._resolve_groups(
                analysis.groups,
                representations=group.members,
                evidence_by_ref=evidence_by_ref,
                node_type=node_type,
                schema=schema,
                diagnostics=diagnostics,
                dependency_nodes=dependency_nodes,
            )
            # Explicit unresolved singletons are complete preserved results.
            if len(memory.feedback) == old_feedback:
                break
            pending_feedback = memory.feedback[old_feedback:]
            failure = fingerprint(
                {
                    "analysis": analysis.model_dump(mode="json"),
                    "feedback": memory.feedback[old_feedback:],
                }
            )
            if memory.stop_reason or failure == previous_failure:
                memory.stop_reason = memory.stop_reason or "no_supported_revision"
                break
            previous_failure = failure
            if round_index == self.consolidation_max_rounds:
                memory.stop_reason = "round_budget_exhausted"
        unresolved_refs = set(analysis.unresolved_refs)
        unresolved_refs.update(
            ref
            for item in outcomes
            for ref in item.supporting_refs
            if not any(
                item.supporting_refs == planned.supporting_refs for planned in analysis.groups
            )
        )
        if unresolved_refs:
            diagnostics.append("CONSOLIDATION_PRESERVED_UNRESOLVED")
        memory.history.append(
            {
                "unresolved_refs": [
                    ref.model_dump(mode="json")
                    for ref in sorted(unresolved_refs, key=lambda ref: (ref.origin, ref.ref_id))
                ]
            }
        )
        if len(outcomes) == 1:
            outcome = outcomes[0]
            sources = _sources_for_refs(outcome.supporting_refs, group.members)
            return ReconciledNode(
                node=_materialize_node(
                    canonical_id,
                    group.node_type,
                    outcome.content,
                    sources,
                ),
                representations=group.members,
                evidence_by_ref=evidence_by_ref,
                diagnostic_codes=tuple(diagnostics),
            )
        result = self._materialize_conflict(
            canonical_id,
            group,
            outcomes,
            analysis.difference_points,
            evidence_by_ref,
            diagnostics,
        )
        if result.conflict is not None:
            if historical_conflict is not None and unresolved_refs:
                retained = _retained_conflict_points(
                    historical_conflict, result.conflict.alternatives
                )
                current_ids = {point.point_id for point in result.conflict.conflict_points}
                result = result.model_copy(
                    update={
                        "conflict": result.conflict.model_copy(
                            update={
                                "conflict_points": (
                                    *result.conflict.conflict_points,
                                    *(
                                        point
                                        for point in retained
                                        if point.point_id not in current_ids
                                    ),
                                ),
                            }
                        )
                    }
                )
            reasons = tuple(
                dict.fromkeys(item.get("reason") or item["kind"] for item in memory.feedback)
            )
            conflict = result.conflict.model_copy(
                update={
                    "consolidation_status": "unresolved"
                    if unresolved_refs
                    else "semantic_conflict",
                    "preservation_reasons": (reasons or (analysis.reason,))
                    if unresolved_refs
                    else (),
                    "consolidation": memory.audit(),
                }
            )
            result = result.model_copy(update={"conflict": conflict})
        return result

    async def _consolidate_proposal(
        self,
        group,
        *,
        canonical_id,
        schema,
        dependency_nodes,
        dependency_conflicts,
        historical_conflict,
    ):
        from cortex.capabilities.graph_merge.reconciliation.proposal import consolidate

        memory = active_memory.get()
        evidence_by_ref, _, code = await self._evidence(group.members)
        memory.context = {
            "historical_conflict": historical_conflict.model_dump(
                mode="json", exclude={"consolidation"}
            )
            if historical_conflict is not None
            else None,
            "members": [m.model_dump(mode="json") for m in group.members],
            "evidence_by_ref": [
                {
                    "ref": ref.model_dump(mode="json"),
                    "evidence": [ev.model_dump(mode="json") for ev in items],
                }
                for ref, items in evidence_by_ref.items()
            ],
            "dependency_nodes": [n.model_dump(mode="json") for n in dependency_nodes],
            "dependency_conflicts": [c.model_dump(mode="json") for c in dependency_conflicts],
        }
        proposal = await consolidate(
            group,
            schema=schema,
            evidence_by_ref=evidence_by_ref,
            dependency_nodes=dependency_nodes,
            dependency_conflicts=dependency_conflicts,
            proposer=self.group_consolidator,
            checker=self.consolidation_checker,
        )
        by_ref = {m.ref: m for m in group.members}
        outcomes = tuple(
            ReconciledRepresentation(content=c.content, supporting_refs=c.supporting_refs)
            for c in proposal.candidates
        ) + tuple(
            ReconciledRepresentation(content=by_ref[r].content, supporting_refs=(r,))
            for r in proposal.unresolved_refs
        )
        diagnostics = [code] if code else []
        if proposal.unresolved_refs:
            diagnostics.append("CONSOLIDATION_PRESERVED_UNRESOLVED")
        result = self._materialize_conflict(
            canonical_id,
            group,
            outcomes,
            proposal.difference_points,
            evidence_by_ref,
            diagnostics,
        )
        if result.conflict is not None:
            points = result.conflict.conflict_points
            if historical_conflict is not None and proposal.unresolved_refs:
                retained = _retained_conflict_points(
                    historical_conflict, result.conflict.alternatives
                )
                points += tuple(
                    p for p in retained if p.point_id not in {x.point_id for x in points}
                )
            result = result.model_copy(
                update={
                    "conflict": result.conflict.model_copy(
                        update={
                            "conflict_points": points,
                            "consolidation_status": "unresolved"
                            if proposal.unresolved_refs
                            else "semantic_conflict",
                            "preservation_reasons": (proposal.reason,)
                            if proposal.unresolved_refs
                            else (),
                        }
                    )
                }
            )
        return result

    async def _resolve_groups(
        self,
        groups: tuple[ReconciliationGroup, ...],
        *,
        representations: tuple[NodeRepresentation, ...],
        evidence_by_ref: dict[RepresentationRef, tuple[EvidenceItem, ...]],
        node_type: NodeType,
        schema: SchemaDefinition,
        diagnostics: list[str],
        dependency_nodes: tuple[GraphNode, ...] = (),
    ) -> tuple[ReconciledRepresentation, ...]:
        """Dispatch deterministic fusion or conditional semantic generalization."""

        by_ref = {representation.ref: representation for representation in representations}
        resolved = []
        for group in groups:
            group_representations = tuple(by_ref[ref] for ref in group.supporting_refs)
            if len(group_representations) == 1:
                item = group_representations[0]
                resolved.append(
                    ReconciledRepresentation(
                        target_alternative_id=group.target_alternative_id,
                        content=item.content,
                        supporting_refs=group.supporting_refs,
                    )
                )
                continue
            baseline = by_ref[group.baseline_ref]
            if group.reason_code in {"identical_content", "deterministic_fusion"}:
                try:
                    content = self.fuser_registry.fuse(
                        group_representations,
                        baseline=baseline,
                        node_type=node_type,
                        schema=schema,
                    )
                except FuseError:
                    diagnostics.append("INVALID_SELECTION")
                    resolved.extend(_preserved_outcomes(group_representations))
                    continue
                resolved.append(
                    ReconciledRepresentation(
                        target_alternative_id=group.target_alternative_id,
                        content=content,
                        supporting_refs=group.supporting_refs,
                    )
                )
                continue
            group_evidence = {ref: evidence_by_ref.get(ref, ()) for ref in group.supporting_refs}
            effective_fields = schema.effective_node_fields(node_type.id)
            if group.relation not in {"equivalent", "compatible"}:
                resolved.extend(_preserved_outcomes(group_representations))
                continue
            memory = active_memory.get()
            feedback_count = len(memory.feedback) if memory else 0
            if node_type.merge_role == MergeRole.FACT:
                content, code = await fuse_if_eligible(
                    mode=group.relation,
                    baseline=baseline.content,
                    representations=group_representations,
                    evidence_by_ref=group_evidence,
                    node_type=node_type,
                    fuser=self.fact_fuser_registry.resolve(node_type, effective_fields),
                    verifier=self.generalization_verifier,
                    effective_fields=effective_fields,
                    shared=group.shared,
                    residuals=group.residuals,
                    dependency_nodes=dependency_nodes,
                )
            else:
                content, code = await generalize_if_eligible(
                    mode=group.relation,
                    baseline=baseline.content,
                    representations=group_representations,
                    evidence_by_ref=group_evidence,
                    node_type=node_type,
                    generalizer=self.generalizer_registry.resolve(node_type, effective_fields),
                    verifier=self.generalization_verifier,
                    shared=group.shared,
                    residuals=group.residuals,
                    residual_relations=group.residual_relations,
                    effective_fields=effective_fields,
                    dependency_nodes=dependency_nodes,
                )
            if code:
                diagnostics.append(code)
            if content is None:
                if memory and len(memory.feedback) == feedback_count:
                    kind = (
                        "insufficient_evidence"
                        if code in {"EVIDENCE_LIMIT_REACHED", "EVIDENCE_UNAVAILABLE"}
                        else "model_failure"
                        if code == "MODEL_FAILURE"
                        else "generation_failure"
                    )
                    memory.reject(
                        kind,
                        refs=group.supporting_refs,
                        reason=code,
                        target="candidate" if kind == "model_failure" else "analysis",
                    )
                for representation in group_representations:
                    resolved.append(
                        ReconciledRepresentation(
                            target_alternative_id=(
                                group.target_alternative_id
                                if representation.ref.origin == "existing_alternative"
                                else None
                            ),
                            content=representation.content,
                            supporting_refs=(representation.ref,),
                        )
                    )
            else:
                resolved.append(
                    ReconciledRepresentation(
                        target_alternative_id=group.target_alternative_id,
                        content=content,
                        supporting_refs=group.supporting_refs,
                    )
                )
        return tuple(resolved)

    def _simple_outcome(self, group, node_type, schema):
        if len(group.members) == 1 or _all_equal(group.members):
            return ReconcileOutcome(
                groups=(
                    ReconciliationGroup(
                        supporting_refs=tuple(member.ref for member in group.members),
                        baseline_ref=group.members[0].ref,
                        relation="equivalent",
                        reason_code="identical_content",
                        reason="Representations have identical content.",
                    ),
                ),
                reason="Representations have identical content.",
            )
        if _deterministically_fuseable(group.members, node_type, schema, self.fuser_registry):
            return ReconcileOutcome(
                groups=(
                    ReconciliationGroup(
                        supporting_refs=tuple(member.ref for member in group.members),
                        baseline_ref=group.members[0].ref,
                        relation="equivalent",
                        reason_code="deterministic_fusion",
                        reason="Structured implementation fields can be fused deterministically.",
                    ),
                ),
                reason="Structured implementation fields can be fused deterministically.",
            )
        return None

    async def _semantic_outcome(
        self, group, evidence, node_type, diagnostics, schema, dependency_nodes
    ):
        effective_fields = schema.effective_node_fields(node_type.id)
        strategy = self.reconciler_registry.resolve(node_type, effective_fields)
        if not evidence or strategy.reconciler is None:
            if not evidence:
                diagnostics.append("EVIDENCE_UNAVAILABLE")
            return ReconcileOutcome(
                groups=_preserved_groups(group.members),
                unresolved_refs=tuple(item.ref for item in group.members),
                reason="Evidence or reconciliation strategy is unavailable.",
            )
        memory = active_memory.get()
        validation_feedback = (
            {
                "semantic_feedback": memory.feedback,
                "previous_outcome": memory.analysis.model_dump(mode="json")
                if memory.analysis
                else None,
                "instruction": (
                    "Re-examine differences, selectors, dependencies and grouping using feedback."
                ),
            }
            if memory and memory.feedback
            else None
        )
        if memory and memory.feedback:
            detail = memory.feedback[-1].get("details")
            if isinstance(detail, dict):
                validation_feedback = {**validation_feedback, **detail}
        for attempt in range(1 if memory else 3):
            feedback_start = len(memory.feedback) if memory else 0
            try:
                kwargs = {"dependency_nodes": dependency_nodes} if dependency_nodes else {}
                if validation_feedback is not None:
                    kwargs["validation_feedback"] = validation_feedback
                outcome = await invoke(
                    "reconcile",
                    strategy.reconciler.reconcile,
                    group,
                    evidence,
                    node_type.identity_policy,
                    **kwargs,
                )
            except TypeError:
                # Backward-compatible custom reconcilers may not implement targeted repair.
                if attempt:
                    break
                diagnostics.append("MODEL_FAILURE")
                if memory and len(memory.feedback) == feedback_start:
                    memory.reject("model_failure", reason="Reconciliation call or parsing failed")
                return ReconcileOutcome(
                    groups=_preserved_groups(group.members),
                    unresolved_refs=tuple(item.ref for item in group.members),
                    reason="Reconciliation model failed.",
                )
            except Exception:
                diagnostics.append("MODEL_FAILURE")
                if memory and len(memory.feedback) == feedback_start:
                    memory.reject("model_failure", reason="Reconciliation call or parsing failed")
                return ReconcileOutcome(
                    groups=_preserved_groups(group.members),
                    unresolved_refs=tuple(item.ref for item in group.members),
                    reason="Reconciliation model failed.",
                )
            outcome = _normalize_reconcile_outcome(outcome)
            if _valid_outcome(
                outcome,
                group.members,
                node_type,
                schema,
                evidence,
                group.members,
                strategy.validator,
            ):
                return outcome
            validation_feedback = {
                "contract_errors": list(
                    _reconciliation_contract_errors(
                        outcome,
                        group.members,
                        node_type,
                        evidence,
                        strategy.validator,
                    )
                ),
                "previous_outcome": outcome.model_dump(mode="json"),
                "instruction": (
                    "Correct only these structural contract errors. Preserve all valid "
                    "difference points and their source case IDs."
                ),
            }
        diagnostics.append("INVALID_SELECTION")
        if memory:
            memory.reject("contract_error", details=validation_feedback)
        return ReconcileOutcome(
            groups=_preserved_groups(group.members),
            unresolved_refs=tuple(item.ref for item in group.members),
            reason="Reconciliation output failed contract validation.",
        )

    async def _evidence(self, representations):
        sources = _union_sources(*(item.sources for item in representations))
        if not sources or self.evidence_resolver is None:
            return {item.ref: () for item in representations}, (), None
        try:
            evidence = await self.evidence_resolver.resolve(sources)
        except Exception:
            return {item.ref: () for item in representations}, (), "EVIDENCE_UNAVAILABLE"
        by_source = {item.source: item for item in evidence}
        by_ref = {
            representation.ref: tuple(
                by_source[source] for source in representation.sources if source in by_source
            )
            for representation in representations
        }
        code = None if len(by_source) == len(sources) else "EVIDENCE_UNAVAILABLE"
        return by_ref, evidence, code

    def _materialize_conflict(
        self,
        canonical_id,
        group,
        outcomes,
        difference_points,
        evidence_by_ref,
        diagnostics,
    ):
        alternatives = []
        for outcome in outcomes:
            alternatives.append(
                ConflictAlternative(
                    alternative_id=next(
                        (
                            ref.ref_id
                            for ref in outcome.supporting_refs
                            if ref.origin == "existing_alternative"
                        ),
                        self.id_factory.new_alternative_id(canonical_id, outcome.supporting_refs),
                    ),
                    content=outcome.content.model_dump(mode="python"),
                    sources=_sources_for_refs(outcome.supporting_refs, group.members),
                )
            )
        alternatives = _deduplicate_alternatives(alternatives)
        if len(alternatives) == 1:
            item = alternatives[0]
            return ReconciledNode(
                node=_materialize_node(
                    canonical_id,
                    group.node_type,
                    NodeContent.model_validate(item.content),
                    item.sources,
                ),
                representations=group.members,
                evidence_by_ref=evidence_by_ref,
                diagnostic_codes=tuple(diagnostics),
            )
        envelope = next(
            (item.content for item in group.members if item.ref.origin == "existing_node"),
            NodeContent.model_validate(alternatives[0].content),
        )
        envelope = _safe_conflict_envelope(envelope, alternatives)
        sources = _union_sources(*(item.sources for item in alternatives))
        return ReconciledNode(
            node=_materialize_node(
                canonical_id, group.node_type, envelope, sources, status=MergeStatus.CONFLICT
            ),
            conflict=NodeMergeConflict(
                node_id=canonical_id,
                alternatives=tuple(alternatives),
                conflict_points=_materialize_conflict_points(
                    difference_points, outcomes, alternatives
                ),
            ),
            representations=group.members,
            evidence_by_ref=evidence_by_ref,
            diagnostic_codes=tuple(diagnostics),
        )


def _alternative_rep(group, alternative):
    return NodeRepresentation(
        ref=RepresentationRef(origin="existing_alternative", ref_id=alternative.alternative_id),
        node_type=group.node_type,
        content=NodeContent.model_validate(alternative.content),
        sources=alternative.sources,
    )


def _valid_outcome(
    outcome,
    members,
    node_type,
    schema,
    evidence,
    representations,
    semantics_validator: ReconciliationSemanticsValidator | None,
):
    expected = {member.ref for member in members}
    evidence_ids = {item.id for item in evidence}
    if any(item.representation_ref not in expected for item in outcome.observations) or any(
        not set(item.supporting_refs) <= expected or not set(item.evidence_ids) <= evidence_ids
        for item in outcome.explanations
    ):
        return False
    actual = [ref for item in outcome.groups for ref in item.supporting_refs]
    if len(actual) != len(set(actual)) or set(actual) != expected or not outcome.groups:
        return False
    return _valid_difference_points(outcome, members) and all(
        item.baseline_ref in item.supporting_refs
        and (len(item.supporting_refs) > 1 or item.relation == "equivalent")
        and item.target_alternative_id is None
        and _valid_reconciliation_semantics(item, node_type)
        and (
            semantics_validator is None
            or semantics_validator.validate(item, evidence, representations)
        )
        for item in outcome.groups
    )


def _valid_branch_outcome(
    outcome,
    expected_members,
    root_id,
    node_type,
    schema,
    evidence,
    representations,
    semantics_validator: ReconciliationSemanticsValidator | None,
):
    roots = [item for item in outcome.groups if item.target_alternative_id == root_id]
    unknown_targets = [
        item for item in outcome.groups if item.target_alternative_id not in {None, root_id}
    ]
    actual = [ref for item in outcome.groups for ref in item.supporting_refs]
    expected_refs = {item.ref for item in expected_members}
    return (
        len(roots) == 1
        and not unknown_targets
        and len(actual) == len(set(actual))
        and set(actual) == expected_refs
        and _valid_difference_points(outcome, expected_members)
        and all(
            item.baseline_ref in item.supporting_refs
            and (len(item.supporting_refs) > 1 or item.relation == "equivalent")
            and _valid_reconciliation_semantics(item, node_type)
            and (
                semantics_validator is None
                or semantics_validator.validate(item, evidence, representations)
            )
            for item in outcome.groups
        )
    )


def _reconciliation_contract_errors(
    outcome,
    members,
    node_type,
    evidence,
    semantics_validator: ReconciliationSemanticsValidator | None,
):
    """Describe contract failures for a bounded, model-driven repair."""

    errors = []
    expected = {member.ref for member in members}
    actual = [ref for item in outcome.groups for ref in item.supporting_refs]
    if not outcome.groups or len(actual) != len(set(actual)) or set(actual) != expected:
        errors.append("MEMBER_PARTITION_INCOMPLETE")
    if not _valid_difference_points(outcome, members):
        conflicts = [point for point in outcome.difference_points if point.resolution == "conflict"]
        if _incomplete_conflict_point_coverage(outcome, members):
            errors.append(
                "CONFLICT_POINT_MEMBER_COVERAGE_INCOMPLETE:every conflict Difference Point "
                "must assign every representation_ref to exactly one position; add the "
                "missing refs to the correct existing position or create a separate position"
            )
        elif len(outcome.groups) > 1 and not conflicts:
            errors.append(
                "MULTIPLE_GROUPS_WITHOUT_CONFLICT_POINT:merge disjoint or selector-resolved "
                "behaviors into one compatible group with source-grounded conditional residuals"
            )
        elif len(outcome.groups) == 1 and conflicts:
            errors.append("CONFLICT_POINT_NOT_PARTITIONED")
        elif _splits_same_conflict_position(outcome):
            errors.append(
                "GROUPS_AND_CONFLICT_POSITIONS_DISAGREE:for representations assigned the same "
                "conflict-position signature, either merge their result-equivalent or "
                "selector-resolved variation into one group, or separate their positions when "
                "their complete behaviors can still change the result without a selector"
            )
        else:
            errors.append("DIFFERENCE_MEMBER_COVERAGE_INVALID")
    for index, group in enumerate(outcome.groups):
        prefix = f"groups[{index}]"
        if group.baseline_ref not in group.supporting_refs:
            errors.append(f"{prefix}:BASELINE_OUTSIDE_GROUP")
        if len(group.supporting_refs) == 1 and group.relation != "equivalent":
            errors.append(f"{prefix}:SINGLETON_NOT_EQUIVALENT")
        if group.relation == "equivalent" and (group.residuals or group.residual_relations):
            errors.append(f"{prefix}:EQUIVALENT_HAS_RESIDUALS")
        if group.relation == "compatible" and not group.shared:
            errors.append(f"{prefix}:COMPATIBLE_MISSING_SHARED")
        if not _valid_reconciliation_semantics(group, node_type):
            errors.append(f"{prefix}:INVALID_RESIDUAL_CONTRACT")
        elif semantics_validator is not None and not semantics_validator.validate(
            group, evidence, members
        ):
            errors.append(
                f"{prefix}:UNSUPPORTED_SELECTOR:the proposed condition does not partition "
                "the observed source contexts; re-check every supporting Question and query "
                "context. This deterministic rejection is supplied counterevidence: do not "
                "return the same conditional grouping or selector again. When result-changing "
                "implementations remain applicable under the same observable selector, emit a "
                "conflict difference point and separate groups"
            )
    return tuple(dict.fromkeys(errors or ("INVALID_RECONCILIATION_OUTPUT",)))


def _valid_difference_points(outcome, representations):
    if outcome.unresolved_refs:
        unresolved = set(outcome.unresolved_refs)
        if (
            len(unresolved) != len(outcome.unresolved_refs)
            or not unresolved <= {item.ref for item in representations}
            or any(
                len(group.supporting_refs) != 1
                for group in outcome.groups
                if unresolved.intersection(group.supporting_refs)
            )
        ):
            return False
        resolved_groups = tuple(
            group for group in outcome.groups if not unresolved.intersection(group.supporting_refs)
        )
        if not resolved_groups:
            return not outcome.difference_points
        return _valid_difference_points(
            outcome.model_copy(update={"groups": resolved_groups, "unresolved_refs": ()}),
            tuple(item for item in representations if item.ref not in unresolved),
        )
    expected_refs = {item.ref for item in representations}
    source_case_ids = {
        item.ref: tuple(
            sorted({source.case_id for source in item.sources if source.case_id is not None})
        )
        for item in representations
    }
    point_ids = [point.point_id for point in outcome.difference_points]
    if len(point_ids) != len(set(point_ids)):
        return False
    for point in outcome.difference_points:
        members = [member for position in point.positions for member in position.members]
        refs = [member.representation_ref for member in members]
        if len(refs) != len(set(refs)) or set(refs) != expected_refs:
            return False
        if any(
            tuple(sorted(set(member.source_case_ids))) != source_case_ids[member.representation_ref]
            for member in members
        ):
            return False
    conflicts = [point for point in outcome.difference_points if point.resolution == "conflict"]
    if len(outcome.groups) > 1 and conflicts:
        return not _splits_same_conflict_position(outcome)
    return len(outcome.groups) == 1 and not conflicts


def _splits_same_conflict_position(outcome) -> bool:
    """Reject alternatives that make no distinct choice at any conflict point."""

    conflicts = tuple(
        point for point in outcome.difference_points if point.resolution == "conflict"
    )
    if not conflicts:
        return False
    group_by_ref = {
        ref: group_index
        for group_index, group in enumerate(outcome.groups)
        for ref in group.supporting_refs
    }
    signature_groups: dict[tuple[int, ...], set[int]] = {}
    refs = tuple(group_by_ref)
    for ref in refs:
        positions = []
        for point in conflicts:
            position_index = next(
                (
                    position_index
                    for position_index, position in enumerate(point.positions)
                    if any(member.representation_ref == ref for member in position.members)
                ),
                None,
            )
            if position_index is None:
                return False
            positions.append(position_index)
        signature = tuple(positions)
        signature_groups.setdefault(signature, set()).add(group_by_ref[ref])
    return any(len(groups) > 1 for groups in signature_groups.values())


def _incomplete_conflict_point_coverage(outcome, representations) -> bool:
    expected_refs = {item.ref for item in representations}
    for point in outcome.difference_points:
        if point.resolution != "conflict":
            continue
        refs = [
            member.representation_ref for position in point.positions for member in position.members
        ]
        if len(refs) != len(set(refs)) or set(refs) != expected_refs:
            return True
    return False


def _normalize_reconcile_outcome(outcome: ReconcileOutcome) -> ReconcileOutcome:
    """Normalize local structural contradictions without making a semantic decision."""

    outcome = outcome.model_copy(
        update={
            # A single observed position is shared behavior, not a difference.
            "difference_points": tuple(
                point for point in outcome.difference_points if len(point.positions) >= 2
            ),
            "groups": tuple(
                group.model_copy(
                    update={
                        # A singleton alternative already preserves its complete source node.
                        # Repeating its behavior as a residual adds no information.
                        "residuals": () if len(group.supporting_refs) == 1 else group.residuals,
                        "residual_relations": tuple(
                            relation
                            for relation in group.residual_relations
                            if len(group.supporting_refs) > 1
                            and not (
                                relation.relation == "compatible"
                                and len(relation.residual_refs) < 2
                            )
                        ),
                    }
                )
                for group in outcome.groups
            ),
        }
    )
    outcome = outcome.model_copy(
        update={
            "groups": tuple(
                group.model_copy(update={"relation": "compatible"})
                if group.relation == "equivalent"
                and len(group.supporting_refs) > 1
                and (group.residuals or group.residual_relations)
                else group
                for group in outcome.groups
            )
        }
    )
    return outcome


def _materialize_node(node_id, node_type, content, sources, status=MergeStatus.NORMAL):
    return GraphNode(
        id=node_id,
        node_type=node_type,
        **content.model_dump(mode="python"),
        sources=sources,
        merge_status=status,
    )


def _all_equal(members):
    return len({_content_fingerprint(member.content) for member in members}) == 1


def _deterministically_fuseable(members, node_type, schema, fuser_registry):
    if not members:
        return False
    try:
        fuser_registry.fuse(
            members,
            baseline=members[0],
            node_type=node_type,
            schema=schema,
        )
    except FuseError:
        return False
    return True


def _try_deterministic_fusion(members, node_type, schema, fuser_registry):
    if not members:
        return None
    try:
        return fuser_registry.fuse(
            tuple(members),
            baseline=members[0],
            node_type=node_type,
            schema=schema,
        )
    except FuseError:
        return None


def _compact_conflict_alternatives(conflict, node_type_id, node_type, schema, fuser_registry):
    if conflict.conflict_points:
        return conflict
    alternatives = _compact_alternatives(
        list(conflict.alternatives),
        node_type_id,
        node_type,
        schema,
        fuser_registry,
    )
    if len(alternatives) < 2:
        return conflict.model_copy(update={"alternatives": tuple(conflict.alternatives)})
    return conflict.model_copy(update={"alternatives": tuple(alternatives)})


def _retained_conflict_points(conflict, alternatives):
    alternative_ids = {item.alternative_id for item in alternatives}
    return tuple(
        point
        for point in conflict.conflict_points
        if {alternative_id for choice in point.choices for alternative_id in choice.alternative_ids}
        <= alternative_ids
    )


def _compact_alternatives(alternatives, node_type_id, node_type, schema, fuser_registry):
    """Collapse alternatives only when their complete executable contracts fuse safely."""

    buckets: list[list[ConflictAlternative]] = []
    for alternative in sorted(alternatives, key=lambda item: item.alternative_id):
        for bucket in buckets:
            representations = tuple(
                NodeRepresentation(
                    ref=RepresentationRef(
                        origin="existing_alternative", ref_id=item.alternative_id
                    ),
                    node_type=node_type_id,
                    content=NodeContent.model_validate(item.content),
                    sources=item.sources,
                )
                for item in (*bucket, alternative)
            )
            if (
                _try_deterministic_fusion(representations, node_type, schema, fuser_registry)
                is not None
            ):
                bucket.append(alternative)
                break
        else:
            buckets.append([alternative])

    compacted = []
    for bucket in buckets:
        representations = tuple(
            NodeRepresentation(
                ref=RepresentationRef(origin="existing_alternative", ref_id=item.alternative_id),
                node_type=node_type_id,
                content=NodeContent.model_validate(item.content),
                sources=item.sources,
            )
            for item in bucket
        )
        content = _try_deterministic_fusion(representations, node_type, schema, fuser_registry)
        if content is None:
            compacted.extend(bucket)
            continue
        compacted.append(
            ConflictAlternative(
                alternative_id=min(item.alternative_id for item in bucket),
                content=content.model_dump(mode="python"),
                sources=_union_sources(*(item.sources for item in bucket)),
            )
        )
    return _deduplicate_alternatives(compacted)


def _valid_reconciliation_semantics(group, node_type):
    """Fail closed when a proposed compression cannot preserve source-relative residuals."""

    refs = set(group.supporting_refs)
    claim_refs = [
        ref for claim in (*group.shared, *group.residuals) for ref in claim.supporting_refs
    ]
    if any(ref not in refs for ref in claim_refs):
        return False
    residual_ids = [item.residual_id for item in group.residuals]
    if len(residual_ids) != len(set(residual_ids)):
        return False
    forbidden_selector_tokens = {
        token
        for ref in group.supporting_refs
        for token in (ref.ref_id, f"{ref.origin}:{ref.ref_id}")
    }
    if any(
        residual.applies_when
        and not _reusable_selector(residual.applies_when, forbidden_selector_tokens)
        for residual in group.residuals
    ):
        return False
    known_residuals = set(residual_ids)
    for relation in group.residual_relations:
        if (
            not relation.residual_refs
            or (
                len(relation.residual_refs) < 2
                and relation.relation not in {"conditional", "refinement"}
            )
            or len(set(relation.residual_refs)) != len(relation.residual_refs)
            or not set(relation.residual_refs) <= known_residuals
            or (
                relation.condition
                and not _reusable_selector(relation.condition, forbidden_selector_tokens)
            )
        ):
            return False
    if group.relation == "equivalent":
        return (
            (bool(group.shared) or len(group.supporting_refs) == 1)
            and not group.residuals
            and not group.residual_relations
        )
    if group.relation != "compatible" or not group.shared:
        return False
    if any(item.relation == "conflict" for item in group.residual_relations):
        return False
    return all(
        residual.applies_when
        or any(
            residual.residual_id in relation.residual_refs
            and relation.relation in {"compatible", "refinement"}
            for relation in group.residual_relations
        )
        for residual in group.residuals
    )


def _reusable_selector(selector, forbidden_tokens):
    normalized = selector.casefold()
    return (
        not any(token.casefold() in normalized for token in forbidden_tokens)
        and "case_id" not in normalized
        and "source_id" not in normalized
        and "metric_ref" not in normalized
        and "representation_ref" not in normalized
    )


def _preserved_outcomes(members):
    return tuple(
        ReconciledRepresentation(content=member.content, supporting_refs=(member.ref,))
        for member in members
    )


def _preserved_groups(members):
    return tuple(
        ReconciliationGroup(
            supporting_refs=(member.ref,),
            baseline_ref=member.ref,
            relation="equivalent",
            reason_code="preserve_unchanged",
            reason="The representation is preserved unchanged.",
        )
        for member in members
    )


def _materialize_conflict_points(
    difference_points: tuple[DifferencePoint, ...],
    outcomes: tuple[ReconciledRepresentation, ...],
    alternatives: list[ConflictAlternative],
) -> tuple[ConflictPoint, ...]:
    """Project unresolved input differences onto complete published alternatives."""

    alternative_by_content = {
        _content_fingerprint(NodeContent.model_validate(item.content)): item.alternative_id
        for item in alternatives
    }
    alternative_by_ref = {
        ref: alternative_by_content[_content_fingerprint(outcome.content)]
        for outcome in outcomes
        for ref in outcome.supporting_refs
    }
    result = []
    for point in difference_points:
        if point.resolution != "conflict":
            continue
        choices = []
        for position in point.positions:
            alternative_ids = tuple(
                sorted(
                    {alternative_by_ref[member.representation_ref] for member in position.members}
                )
            )
            choices.append(
                ConflictChoice(
                    alternative_ids=alternative_ids,
                    source_case_ids=tuple(
                        sorted(
                            {
                                case_id
                                for member in position.members
                                for case_id in member.source_case_ids
                            }
                        )
                    ),
                    statement=position.statement,
                )
            )
        if len({item for choice in choices for item in choice.alternative_ids}) < 2:
            continue
        result.append(
            ConflictPoint(
                point_id=point.point_id,
                aspect=point.aspect,
                description=point.reason,
                choices=tuple(choices),
            )
        )
    return tuple(result)


def _safe_conflict_envelope(
    baseline: NodeContent, alternatives: list[ConflictAlternative]
) -> NodeContent:
    """Expose only relationship-bearing values shared by every conflict branch."""

    contents = [NodeContent.model_validate(item.content) for item in alternatives]
    properties = dict(baseline.properties)
    for field in ("references", "targets"):
        values = [content.properties.get(field) for content in contents]
        if not values or not all(isinstance(value, list) for value in values):
            continue
        common = {
            json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            for item in values[0]
        }
        for value in values[1:]:
            common &= {
                json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                for item in value
            }
        properties[field] = [
            item
            for item in values[0]
            if json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":")) in common
        ]
    return baseline.model_copy(update={"properties": properties})


def _content_fingerprint(content):
    return json.dumps(content.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def _deduplicate_alternatives(alternatives):
    by_content = {}
    for item in sorted(alternatives, key=lambda value: value.alternative_id):
        key = _content_fingerprint(NodeContent.model_validate(item.content))
        if key in by_content:
            old = by_content[key]
            by_content[key] = old.model_copy(
                update={"sources": _union_sources(old.sources, item.sources)}
            )
        else:
            by_content[key] = item
    return sorted(
        by_content.values(),
        key=lambda item: (
            _content_fingerprint(NodeContent.model_validate(item.content)),
            item.alternative_id,
        ),
    )


def _sources_for_refs(refs, representations):
    by_ref = {item.ref: item for item in representations}
    return _union_sources(*(by_ref[ref].sources for ref in refs))


def _union_sources(*groups):
    values = {}
    for group in groups:
        for source in group:
            key = (source.source_id, source.chunk_id, source.locator, source.case_id)
            values[key] = source
    keys = sorted(values, key=lambda item: tuple(value or "" for value in item))
    return tuple(values[key] for key in keys)
