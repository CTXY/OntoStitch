from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from cortex.capabilities.graph_merge.models import (
    EvidenceItem,
    GeneralizationProposal,
    GeneralizationVerification,
    NodeContent,
    NodeRepresentation,
    RepresentationRef,
    ResidualRelation,
    SemanticFieldProvenance,
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
from cortex.schema import (
    FieldDefinition,
    GraphNode,
    IdentityMode,
    IdentityPolicy,
    MergeRole,
    NodeType,
)


class KnowledgeGeneralizer(Protocol):
    def generalize(
        self,
        *,
        baseline: NodeContent,
        representations: tuple[NodeRepresentation, ...],
        evidence_by_ref: Mapping[RepresentationRef, tuple[EvidenceItem, ...]],
        node_type: NodeType,
        identity_policy: IdentityPolicy,
        mode: Literal["equivalent", "compatible"],
        shared: tuple[SharedSemantic, ...],
        residuals: tuple[SemanticResidual, ...],
        residual_relations: tuple[ResidualRelation, ...],
        validation_feedback: dict[str, object] | None = None,
        dependency_nodes: tuple[GraphNode, ...] = (),
    ) -> GeneralizationProposal: ...


class KnowledgeGeneralizerRegistry:
    """Resolve Schema-compatible Knowledge strategies with a generic fallback."""

    def __init__(
        self,
        default: KnowledgeGeneralizer | None,
        registrations: Mapping[str, tuple[frozenset[str], KnowledgeGeneralizer]] | None = None,
    ) -> None:
        self.default = default
        self.registrations = dict(registrations or {})

    def resolve(
        self, node_type: NodeType, effective_fields: Mapping[str, FieldDefinition]
    ) -> KnowledgeGeneralizer | None:
        registration = self.registrations.get(node_type.id)
        if registration is None:
            return self.default
        required_fields, generalizer = registration
        return generalizer if required_fields <= set(effective_fields) else self.default


class GeneralizationCoverageVerifier(Protocol):
    def verify(
        self,
        *,
        proposal: GeneralizationProposal,
        representations: tuple[NodeRepresentation, ...],
        evidence_by_ref: Mapping[RepresentationRef, tuple[EvidenceItem, ...]],
        node_type: NodeType,
        dependency_nodes: tuple[GraphNode, ...] = (),
    ) -> GeneralizationVerification: ...


def generalize_if_eligible(
    *,
    baseline: NodeContent,
    representations: tuple[NodeRepresentation, ...],
    evidence_by_ref: Mapping[RepresentationRef, tuple[EvidenceItem, ...]],
    node_type: NodeType,
    generalizer: KnowledgeGeneralizer | None,
    verifier: GeneralizationCoverageVerifier | None = None,
    shared: tuple[SharedSemantic, ...] = (),
    residuals: tuple[SemanticResidual, ...] = (),
    residual_relations: tuple[ResidualRelation, ...] = (),
    effective_fields: Mapping[str, FieldDefinition] | None = None,
    mode: Literal["equivalent", "compatible"] = "compatible",
    dependency_nodes: tuple[GraphNode, ...] = (),
) -> tuple[NodeContent | None, str | None]:
    if node_type.merge_role != MergeRole.KNOWLEDGE:
        return baseline, None
    if generalizer is None:
        return None, "GENERALIZER_UNAVAILABLE"
    case_ids = {
        source.case_id
        for representation in representations
        for source in representation.sources
        if source.case_id is not None
    }
    if len({_fingerprint(item.content) for item in representations}) < 2:
        return baseline, None
    if mode == "compatible" and len(case_ids) < 2:
        return None, "EVIDENCE_LIMIT_REACHED"
    evidence_cases = {
        item.case_id
        for items in evidence_by_ref.values()
        for item in items
        if item.case_id is not None
    }
    if not case_ids <= evidence_cases or not evidence_complete(representations, evidence_by_ref):
        return None, "EVIDENCE_LIMIT_REACHED"
    fields = node_type.fields if effective_fields is None else effective_fields
    if verifier is None:
        return None, "INVALID_GENERALIZATION_PROPOSAL"
    all_references = sorted(
        {
            reference
            for representation in representations
            for reference in representation.content.properties.get("references", [])
        }
    )
    reference_probe = baseline.model_copy(
        update={
            "properties": {
                **baseline.properties,
                "references": all_references,
            }
        }
    )
    forbidden_source_specific_references = _generation_rule_stale_references(
        reference_probe,
        representations,
        node_type=node_type,
        mode=mode,
        residuals=residuals,
        residual_relations=residual_relations,
    )
    memory = active_memory.get()
    validation_feedback = memory.payload() if memory else None
    feedback_start = len(memory.feedback) if memory else 0
    verification_history = []
    # Schema/contract checks and independent semantic coverage can surface distinct losses.
    # Permit one bounded follow-up after each layer without turning reconciliation into an
    # unbounded model loop.
    for _attempt in range(1 if memory else 3):
        try:
            extra = {"dependency_nodes": dependency_nodes} if dependency_nodes else {}
            proposal = invoke(
                "generalize",
                generalizer.generalize,
                baseline=baseline,
                representations=representations,
                evidence_by_ref=evidence_by_ref,
                node_type=node_type,
                identity_policy=node_type.identity_policy,
                mode=mode,
                shared=shared,
                residuals=residuals,
                residual_relations=residual_relations,
                validation_feedback=validation_feedback,
                **extra,
            )
        except Exception:
            return None, "MODEL_FAILURE"
        if proposal.action == "unable":
            if memory:
                memory.reject(
                    "generation_failure",
                    refs=tuple(item.ref for item in representations),
                    reason="generation_unable",
                    model_reason=proposal.reason,
                )
            return None, "INVALID_GENERALIZATION_PROPOSAL"
        if proposal.content is None or not _valid_content(proposal.content, fields):
            validation_feedback = {
                "contract_error": "invalid_content",
                "instruction": (
                    "Return Schema-valid content containing every required property and no "
                    "properties outside the supplied effective field definitions."
                ),
            }
            continue
        if node_type.identity_policy.mode == IdentityMode.EXACT and (
            _identity(proposal.content, node_type.identity_policy)
            != _identity(baseline, node_type.identity_policy)
        ):
            validation_feedback = {
                "contract_error": "exact_identity_changed",
                "required_identity": list(_identity(baseline, node_type.identity_policy)),
                "instruction": "Preserve the baseline exact-identity field values.",
            }
            continue
        corrected_content = _correct_generation_rule_sql_semantics(
            proposal.content, node_type=node_type
        )
        if corrected_content != proposal.content:
            proposal = proposal.model_copy(update={"content": corrected_content})
        stale_references = _generation_rule_stale_references(
            proposal.content,
            representations,
            node_type=node_type,
            mode=mode,
            residuals=residuals,
            residual_relations=residual_relations,
        )
        invalid_rank_semantics = _generation_rule_misstates_rank_semantics(
            proposal.content, node_type=node_type
        )
        if stale_references or invalid_rank_semantics:
            contract_errors = []
            instructions = []
            if stale_references:
                contract_errors.append(
                    "generalization_added_unconditional_source_specific_reference"
                )
                instructions.append(
                    "Remove source-specific references whose concrete role was parameterized "
                    "out of the generalized rule; retain one only inside an explicit "
                    "evidence-supported conditional branch."
                )
            if invalid_rank_semantics:
                contract_errors.append("generation_rule_invalid_rank_semantics")
                instructions.append(
                    "RANK() assigns equal values the same rank and skips subsequent rank "
                    "numbers. Correct the explanation; do not claim that RANK() does not skip."
                )
            validation_feedback = {
                "contract_error": contract_errors[0],
                "contract_errors": contract_errors,
                "source_specific_references": list(stale_references),
                "must_not_reference": list(forbidden_source_specific_references),
                "instruction": " ".join(
                    (
                        *instructions,
                        (
                            "Keep every ID in must_not_reference absent on this retry; those "
                            "source-specific dependencies are not supported by a conditional "
                            "residual that owns them."
                            if forbidden_source_specific_references
                            else ""
                        ),
                    )
                ).strip(),
            }
            continue
        proposal = proposal.model_copy(
            update={"field_provenance": _derive_provenance(proposal, representations, fields)}
        )
        if not _valid_provenance(proposal, representations, fields):
            missing_paths = sorted(
                {item.source_path for item in proposal.field_provenance if item.output_path is None}
            )
            validation_feedback = {
                "contract_error": "incomplete_definition_coverage",
                "missing_definition_paths": missing_paths,
                "instruction": (
                    "Preserve every source definition field in the generalized content. "
                    "Revise the proposal so each listed path has a semantically covering "
                    "output field."
                ),
            }
            continue
        candidate_id = fingerprint(proposal.content)
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
            memory.reject(
                "candidate_defect",
                refs=tuple(item.ref for item in representations),
                candidate_id=candidate_id,
                reason="Repeated rejected definition",
            )
            return None, "INVALID_GENERALIZATION_PROPOSAL"
        record = (
            memory.record_candidate(
                proposal.content,
                tuple(item.ref for item in representations),
                candidate_id,
                candidate_key,
            )
            if memory
            else None
        )
        try:
            extra = {"dependency_nodes": dependency_nodes} if dependency_nodes else {}
            verification = invoke(
                "coverage",
                verifier.verify,
                proposal=proposal,
                representations=representations,
                evidence_by_ref=evidence_by_ref,
                node_type=node_type,
                **extra,
            )
        except Exception:
            if memory:
                memory.failed_review(record)
            return None, "MODEL_FAILURE"
        expected = {item.ref for item in representations}
        actual = [item.supporting_ref for item in verification.verdicts]
        evidence_ids = {item.id for items in evidence_by_ref.values() for item in items}
        covered = (
            len(actual) == len(set(actual))
            and set(actual) == expected
            and all(item.verdict == "covered" for item in verification.verdicts)
            and sources_covered(verification, representations, evidence_by_ref)
            and _plans_covered(
                verification.plan_verdicts,
                representations,
                proposal.content,
                evidence_ids,
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
        if covered:
            return proposal.content, None
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
        verification_history.append(verification.model_dump(mode="json"))
        validation_feedback = {
            "verification": verification_history[-1],
            "verification_history": list(verification_history),
            "must_not_reference": list(forbidden_source_specific_references),
            "instruction": (
                "Revise the proposal to address every uncovered representation and plan in "
                "the complete verification_history, including corrections from earlier retries. "
                "Treat only supplied evidence as a counterexample; unobserved contexts do not "
                "invalidate an otherwise evidence-consistent generalization. Preserve all prior "
                "contract corrections and keep every ID in must_not_reference absent. Do not "
                "introduce an alternative executable action that no supplied representation "
                "permits. A not_covered verdict must cite supplied counterevidence; unknown "
                "means coverage has not been proved."
            ),
        }
    if memory and validation_feedback and len(memory.feedback) == feedback_start:
        memory.reject(
            "contract_error",
            target="candidate",
            refs=tuple(item.ref for item in representations),
            details=validation_feedback,
        )
    return None, "INVALID_GENERALIZATION_PROPOSAL"


def _derive_provenance(
    proposal: GeneralizationProposal,
    representations: tuple[NodeRepresentation, ...],
    fields: Mapping[str, FieldDefinition],
):
    """Build complete path provenance deterministically; semantic coverage remains LLM-reviewed."""

    output = proposal.content.model_dump(mode="python") if proposal.content else {}
    rows = []
    for representation in representations:
        source = representation.content.model_dump(mode="python")
        for path in _definition_paths(representation.content, fields):
            source_value = _path_value(source, path)
            output_value = _path_value(output, path)
            rows.append(
                SemanticFieldProvenance(
                    supporting_ref=representation.ref,
                    source_path=path,
                    output_path=path if output_value is not _MISSING else None,
                    transformation="exact" if source_value == output_value else "semantic",
                    reason="Deterministic same-path comparison.",
                )
            )
    return tuple(rows)


def _valid_provenance(
    proposal: GeneralizationProposal,
    representations: tuple[NodeRepresentation, ...],
    fields: Mapping[str, FieldDefinition],
) -> bool:
    expected = {
        (representation.ref, path)
        for representation in representations
        for path in _definition_paths(representation.content, fields)
    }
    actual = [
        (item.supporting_ref, _normalize_path(item.source_path))
        for item in proposal.field_provenance
    ]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        return False
    output = proposal.content.model_dump(mode="python") if proposal.content else {}
    by_ref = {item.ref: item.content.model_dump(mode="python") for item in representations}
    for item in proposal.field_provenance:
        if item.transformation == "omitted" or item.output_path is None:
            return False
        source_value = _path_value(by_ref[item.supporting_ref], _normalize_path(item.source_path))
        output_value = _path_value(output, _normalize_path(item.output_path))
        if output_value is _MISSING:
            return False
        if item.transformation == "exact" and source_value != output_value:
            return False
    return True


_MISSING = object()


def _path_value(payload: object, path: str) -> object:
    value = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _normalize_path(path: str) -> str:
    return path.removeprefix("content.")


def _definition_paths(
    content: NodeContent, fields: Mapping[str, FieldDefinition]
) -> tuple[str, ...]:
    paths = ["name"]
    if content.description is not None:
        paths.append("description")
    if content.aliases:
        paths.append("aliases")
    if content.tags:
        paths.append("tags")
    paths.extend(f"properties.{key}" for key in sorted(content.properties) if key in fields)
    return tuple(paths)


def _identity(content: NodeContent, policy: IdentityPolicy) -> tuple[object, ...]:
    return tuple(
        content.properties.get(path.removeprefix("properties."))
        if path.startswith("properties.")
        else getattr(content, path)
        for path in policy.fields
    )


def _valid_content(content: NodeContent, fields: Mapping[str, FieldDefinition]) -> bool:
    if set(content.properties) - set(fields):
        return False
    return all(
        not field.required or field_id in content.properties for field_id, field in fields.items()
    )


def _fingerprint(content: NodeContent) -> str:
    return content.model_dump_json(exclude_none=False)


def _generation_rule_stale_references(
    content: NodeContent,
    representations: tuple[NodeRepresentation, ...],
    *,
    node_type: NodeType,
    mode: Literal["equivalent", "compatible"],
    residuals: tuple[SemanticResidual, ...],
    residual_relations: tuple[ResidualRelation, ...],
) -> tuple[str, ...]:
    if node_type.id != "generation_rule" or not representations:
        return ()
    common = set(representations[0].content.properties.get("references", []))
    for representation in representations[1:]:
        common &= set(representation.content.properties.get("references", []))
    proposed = set(content.properties.get("references", []))
    stale = proposed - common
    if mode != "compatible" or not stale:
        return tuple(sorted(stale))
    residual_by_id = {item.residual_id: item for item in residuals}
    conditional_supporting_refs = {
        ref
        for relation in residual_relations
        if relation.relation == "conditional" and relation.condition
        for residual_id in relation.residual_refs
        if residual_id in residual_by_id
        for ref in residual_by_id[residual_id].supporting_refs
    }
    disallowed = []
    for reference in stale:
        holders = {
            item.ref
            for item in representations
            if reference in item.content.properties.get("references", [])
        }
        if not holders or not holders <= conditional_supporting_refs:
            disallowed.append(reference)
    return tuple(sorted(disallowed))


def _generation_rule_misstates_rank_semantics(content: NodeContent, *, node_type: NodeType) -> bool:
    if node_type.id != "generation_rule":
        return False
    text = " ".join(
        str(value)
        for value in (
            content.description,
            content.properties.get("condition"),
            content.properties.get("action"),
            content.properties.get("reason"),
        )
        if value is not None
    ).lower()
    return "rank" in text and ("不跳过" in text or "does not skip" in text)


def _correct_generation_rule_sql_semantics(
    content: NodeContent, *, node_type: NodeType
) -> NodeContent:
    if node_type.id != "generation_rule":
        return content

    def correct(value: object) -> object:
        if not isinstance(value, str) or "rank" not in value.casefold():
            return value
        return (
            value.replace("且不跳过后续排名", "并跳过后续排名号")
            .replace("不跳过后续排名号", "跳过后续排名号")
            .replace("does not skip subsequent ranks", "skips subsequent ranks")
        )

    properties = dict(content.properties)
    for field in ("condition", "action", "reason"):
        if field in properties:
            properties[field] = correct(properties[field])
    return content.model_copy(
        update={
            "description": correct(content.description),
            "properties": properties,
        }
    )


def _plans_covered(
    plan_verdicts,
    representations,
    content: NodeContent,
    evidence_ids: set[str],
) -> bool:
    expected = {
        (item.ref, str(plan.get("id", "")))
        for item in representations
        for plan in item.content.properties.get("sql_generation_plans", [])
        if isinstance(plan, dict)
    }
    if not expected:
        return not plan_verdicts
    actual = [(item.supporting_ref, item.source_plan_id) for item in plan_verdicts]
    canonical_ids = {
        str(plan.get("id", ""))
        for plan in content.properties.get("sql_generation_plans", [])
        if isinstance(plan, dict)
    }
    return (
        len(actual) == len(set(actual))
        and set(actual) == expected
        and all(
            item.canonical_plan_id in canonical_ids
            and item.verdict == "covered"
            and item.relation != "incompatible"
            for item in plan_verdicts
        )
    )


def _grounded_counterevidence(counterevidence_ids: tuple[str, ...], evidence_ids: set[str]) -> bool:
    return bool(counterevidence_ids) and set(counterevidence_ids) <= evidence_ids
