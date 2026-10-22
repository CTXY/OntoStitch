from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from cortex.capabilities.graph_merge.models import (
    EvidenceItem,
    FusionProposal,
    NodeContent,
    NodeRepresentation,
    RepresentationRef,
    SemanticResidual,
    SharedSemantic,
)
from cortex.capabilities.graph_merge.reconciliation.consolidation import (
    active_memory,
    fingerprint,
    invoke,
)
from cortex.capabilities.graph_merge.reconciliation.coverage import (
    evidence_complete,
    feedback_kind,
    sources_covered,
)
from cortex.capabilities.graph_merge.reconciliation.generalization import (
    GeneralizationCoverageVerifier,
    _plans_covered,
)
from cortex.schema import FieldDefinition, GraphNode, IdentityMode, NodeType


class FactFuser(Protocol):
    def fuse(
        self,
        *,
        mode: Literal["equivalent", "compatible"],
        baseline: NodeContent,
        representations: tuple[NodeRepresentation, ...],
        evidence_by_ref: Mapping[RepresentationRef, tuple[EvidenceItem, ...]],
        node_type: NodeType,
        shared: tuple[SharedSemantic, ...],
        residuals: tuple[SemanticResidual, ...],
        validation_feedback: dict[str, object] | None = None,
        dependency_nodes: tuple[GraphNode, ...] = (),
    ) -> FusionProposal: ...


class FactFuserRegistry:
    """Resolve Schema-compatible Fact strategies with a generic semantic fallback."""

    def __init__(
        self,
        default: FactFuser | None,
        registrations: Mapping[str, tuple[frozenset[str], FactFuser]] | None = None,
    ) -> None:
        self.default = default
        self.registrations = dict(registrations or {})

    def resolve(
        self, node_type: NodeType, effective_fields: Mapping[str, FieldDefinition]
    ) -> FactFuser | None:
        registration = self.registrations.get(node_type.id)
        if registration is None:
            return self.default
        required_fields, fuser = registration
        return fuser if required_fields <= set(effective_fields) else self.default


def fuse_if_eligible(
    *,
    mode: Literal["equivalent", "compatible"],
    baseline: NodeContent,
    representations: tuple[NodeRepresentation, ...],
    evidence_by_ref: Mapping[RepresentationRef, tuple[EvidenceItem, ...]],
    node_type: NodeType,
    fuser: FactFuser | None,
    verifier: GeneralizationCoverageVerifier | None,
    effective_fields: Mapping[str, FieldDefinition],
    shared: tuple[SharedSemantic, ...] = (),
    residuals: tuple[SemanticResidual, ...] = (),
    dependency_nodes: tuple[GraphNode, ...] = (),
) -> tuple[NodeContent | None, str | None]:
    if not evidence_complete(representations, evidence_by_ref):
        return None, "EVIDENCE_LIMIT_REACHED"
    if fuser is None:
        return None, "FUSER_UNAVAILABLE"
    memory = active_memory.get()
    try:
        proposal = invoke(
            "fuse",
            fuser.fuse,
            mode=mode,
            baseline=baseline,
            representations=representations,
            evidence_by_ref=evidence_by_ref,
            node_type=node_type,
            shared=shared,
            residuals=residuals,
            **(
                {"validation_feedback": memory.payload(), "dependency_nodes": dependency_nodes}
                if memory
                else {}
            ),
        )
    except Exception:
        return None, "MODEL_FAILURE"
    if proposal.action != "fuse" or proposal.content is None:
        if memory:
            memory.reject(
                "generation_failure",
                refs=tuple(item.ref for item in representations),
                reason="fusion_unable",
                model_reason=proposal.reason,
            )
        return None, "INVALID_FUSION_PROPOSAL"
    content = proposal.content
    if set(content.properties) - set(effective_fields):
        return None, "INVALID_FUSION_PROPOSAL"
    if any(
        field.required and content.properties.get(field_id) is None
        for field_id, field in effective_fields.items()
    ):
        return None, "INVALID_FUSION_PROPOSAL"
    if node_type.identity_policy.mode == IdentityMode.EXACT and _identity(
        content, node_type
    ) != _identity(baseline, node_type):
        return None, "INVALID_FUSION_PROPOSAL"
    if verifier is None:
        return None, "INVALID_FUSION_PROPOSAL"
    memory = active_memory.get()
    candidate_id = fingerprint(content)
    candidate_key = fingerprint(
        {
            "candidate_id": candidate_id,
            "supporting_refs": sorted(
                (item.ref.origin, item.ref.ref_id) for item in representations
            ),
        }
    )
    if memory and candidate_key in memory.failed_candidates:
        memory.stop_reason = "repeated_failed_candidate"
        return None, "INVALID_FUSION_PROPOSAL"
    record = (
        memory.record_candidate(
            content, tuple(item.ref for item in representations), candidate_id, candidate_key
        )
        if memory
        else None
    )
    try:
        verification = invoke(
            "coverage",
            verifier.verify,
            proposal=proposal,
            representations=representations,
            evidence_by_ref=evidence_by_ref,
            node_type=node_type,
            **({"dependency_nodes": dependency_nodes} if dependency_nodes else {}),
        )
    except Exception:
        if memory:
            memory.failed_review(record)
        return None, "MODEL_FAILURE"
    expected = {item.ref for item in representations}
    actual = [item.supporting_ref for item in verification.verdicts]
    covered = (
        len(actual) == len(set(actual))
        and set(actual) == expected
        and all(item.verdict == "covered" for item in verification.verdicts)
        and sources_covered(verification, representations, evidence_by_ref)
        and _plans_covered(
            verification.plan_verdicts,
            representations,
            content,
            {item.id for items in evidence_by_ref.values() for item in items},
        )
    )
    if record is not None:
        record.update(
            {
                "review": verification.model_dump(mode="json"),
                "accepted": covered,
                "review_status": "complete",
            }
        )
    if not covered:
        if memory:
            kind = feedback_kind(verification, representations, evidence_by_ref)
            if kind != "contract_error":
                memory.failed_candidates.add(candidate_key)
            memory.reject(
                kind,
                refs=tuple(item.ref for item in representations),
                candidate_id=candidate_id,
                review=verification.model_dump(mode="json"),
            )
        return None, "INVALID_FUSION_PROPOSAL"
    return content, None


def _identity(content: NodeContent, node_type: NodeType) -> tuple[object, ...]:
    payload = content.model_dump(mode="python")
    values = []
    for path in node_type.identity_policy.fields:
        value: object = payload
        for segment in path.split("."):
            value = value.get(segment) if isinstance(value, dict) else None
        values.append(value)
    return tuple(values)


__all__ = ["FactFuser", "FactFuserRegistry", "fuse_if_eligible"]
