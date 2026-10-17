from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from time import monotonic
from typing import Any, Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import model_validator

from cortex.capabilities.graph_merge.mining.concept_hierarchy.models import ConceptMinerOutcome
from cortex.capabilities.graph_merge.models import (
    BranchReconciliationGroup,
    BranchSelection,
    ConsolidationProposal,
    ConsolidationReview,
    CrossTypeCandidate,
    CrossTypeResolution,
    FinalNodeGroup,
    FusionProposal,
    GeneralizationProposal,
    GeneralizationVerification,
    GroupIdentityView,
    NodeIdentitySelection,
    NodeIdentityView,
    ReconcileOutcome,
    ReconciliationInputAnalysis,
    ReconciliationPairDecision,
)
from cortex.capabilities.graph_merge.reconciliation.outcome_compiler import (
    compile_single_conflict_point,
    complete_single_shared_group,
)
from cortex.capabilities.graph_merge.reconciliation.strategy import (
    representation_scope_candidates,
    request_scope_candidates,
)
from cortex.infrastructure.llm.diagnostics import exception_details
from cortex.infrastructure.llm.parsing import run_llm_extraction
from cortex.schema.ontology_asset_definitions import render_ontology_asset_contract

_DATABASE_ONTOLOGY_TYPES = frozenset({"concept", "metric", "generation_rule"})
_ASSET_SECTION_BY_TYPE = {
    "concept": "concept",
    "metric": "metric",
    "generation_rule": "generation_rule",
}


class GraphMergeAgentTraceSink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...


class _Agent:
    def __init__(self, llm: BaseChatModel, trace_sink: GraphMergeAgentTraceSink | None = None):
        self.llm = llm
        self.trace_sink = trace_sink

    async def _run(
        self,
        name: str,
        output_model,
        payload: dict[str, Any],
        *,
        prompt_extension: str = "",
        ontology_asset_type: str | None = None,
    ):
        from cortex.capabilities.graph_merge.reconciliation.consolidation import active_memory

        memory = active_memory.get()
        if memory is not None:
            memory.consume(name)
            payload = {
                **payload,
                "working_memory": (
                    memory.prompt_payload(name)
                    if name in {"consolidate", "consolidation_check"}
                    else memory.payload()
                ),
            }
        ref_codec = _RefCodec.from_payload(payload)
        payload = ref_codec.encode(payload)
        prompt = (
            files("cortex.capabilities.graph_merge.llm.prompts")
            .joinpath(f"{name}.md")
            .read_text(encoding="utf-8")
        )
        extensions = []
        if ontology_asset_type in _DATABASE_ONTOLOGY_TYPES:
            if name in {"consolidate", "consolidation_check"}:
                extensions.append(
                    files("cortex.capabilities.graph_merge.llm.prompts")
                    .joinpath("merge_types", f"{ontology_asset_type}.md")
                    .read_text(encoding="utf-8")
                    .strip()
                )
            elif name not in {"identity", "cross_type_identity"}:
                # Legacy agents retain their original extraction/repair contract for compatibility.
                extensions.append(
                    render_ontology_asset_contract(
                        ("common", "owners", _ASSET_SECTION_BY_TYPE[ontology_asset_type])
                    ).strip()
                )
        if prompt_extension:
            extensions.append(prompt_extension.strip())
        if extensions:
            rendered_extensions = "\n\n".join(extensions)
            prompt = f"{prompt.rstrip()}\n\n{rendered_extensions}\n"
        trace: dict[str, Any] = {}
        started = monotonic()
        try:
            result = await run_llm_extraction(
                llm=self.llm,
                prompt=prompt,
                output_model=output_model,
                human_payload=payload,
                trace_capture=trace,
                repair_with_context=name == "identity",
                allow_repair=memory is None,
            )
        except Exception as exc:
            if self.trace_sink is not None:
                self.trace_sink.record(
                    {
                        "component": name,
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "request": trace.get("prompt"),
                        "response": trace.get("response"),
                        "repair_response": trace.get("repair_response"),
                        "error_type": type(exc).__name__,
                        "error_chain": exception_details(exc),
                        "elapsed_seconds": round(monotonic() - started, 3),
                        "input_refs": sorted(_refs(payload)),
                    }
                )
            raise
        decoded_result = ref_codec.decode_model(result, output_model)
        if self.trace_sink is not None:
            self.trace_sink.record(
                {
                    "component": name,
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "request": trace.get("prompt"),
                    "response": trace.get("response"),
                    "repair_response": trace.get("repair_response"),
                    "parsed_response": result.model_dump(mode="json"),
                    "input_refs": sorted(_refs(payload)),
                }
            )
        return decoded_result


_REF_ORIGINS = frozenset({"existing_node", "incoming_node", "existing_alternative"})


class _RefCodec:
    def __init__(self, mapping: dict[tuple[str, str], str]):
        self.forward = mapping
        self.reverse = {
            (origin, short): {"origin": origin, "ref_id": ref_id}
            for (origin, ref_id), short in mapping.items()
        }

    @classmethod
    def from_payload(cls, payload: Any) -> _RefCodec:
        refs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def visit(value: Any) -> None:
            if _is_ref_payload(value):
                key = (str(value["origin"]), str(value["ref_id"]))
                if key not in seen:
                    seen.add(key)
                    refs.append(key)
                return
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list | tuple):
                for child in value:
                    visit(child)

        visit(payload)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        prefix = "gm_ref"
        if any(f"{prefix}_{index:03d}" in serialized for index in range(1, len(refs) + 1)):
            digest = hashlib.sha256(serialized.encode()).hexdigest()[:6]
            prefix = f"gm_ref_{digest}"
        return cls({key: f"{prefix}_{index:03d}" for index, key in enumerate(refs, start=1)})

    def encode(self, value: Any) -> Any:
        if _is_ref_payload(value):
            key = (str(value["origin"]), str(value["ref_id"]))
            short = self.forward.get(key)
            if short is None:
                return value
            return {**value, "ref_id": short}
        if isinstance(value, dict):
            return {key: self.encode(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.encode(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self.encode(child) for child in value)
        return value

    def decode(self, value: Any) -> Any:
        if _is_ref_payload(value):
            original = self.reverse.get((str(value["origin"]), str(value["ref_id"])))
            if original is None:
                return value
            return {**value, **original}
        if isinstance(value, dict):
            return {key: self.decode(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.decode(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self.decode(child) for child in value)
        return value

    def decode_model(self, result, output_model):
        if not self.forward:
            return result
        decoded = self.decode(result.model_dump(mode="json"))
        return output_model.model_validate(decoded)


def _is_ref_payload(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("origin") in _REF_ORIGINS
        and isinstance(value.get("ref_id"), str)
    )


class LLMSemanticIdentityResolver(_Agent):
    async def resolve(
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
            "query_candidates": [item.model_dump(mode="json") for item in query_candidates],
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
        return await self._run(
            "identity",
            CandidateIdentitySelection,
            payload,
            ontology_asset_type=seed.node_type,
        )


class LLMCrossTypeAssetResolver(_Agent):
    async def resolve(self, candidates: tuple[CrossTypeCandidate, ...]) -> CrossTypeResolution:
        return await self._run(
            "cross_type_identity",
            CrossTypeResolution,
            {"candidates": [item.model_dump(mode="json") for item in candidates]},
            ontology_asset_type="concept",
        )


class LLMNodeReconciler(_Agent):
    def __init__(self, llm, trace_sink=None, *, prompt_variant: str | None = None):
        super().__init__(llm, trace_sink)
        self.prompt_variant = prompt_variant

    async def reconcile(
        self,
        group,
        evidence,
        policy,
        *,
        dependency_nodes=(),
        validation_feedback=None,
    ) -> ReconcileOutcome:
        prompt_root = files("cortex.capabilities.graph_merge.llm.prompts")
        type_prompt = prompt_root.joinpath(
            "reconcile_types", f"{self.prompt_variant or 'strict_default'}.md"
        )
        if not type_prompt.is_file():
            type_prompt = prompt_root.joinpath("reconcile_types", "strict_default.md")
        return await self._run(
            "reconcile",
            ReconcileOutcome,
            {
                "identity_policy": policy.model_dump(mode="json"),
                "group": group.model_dump(mode="json"),
                "evidence": [item.model_dump(mode="json") for item in evidence],
                "request_scope_candidates": request_scope_candidates(group, evidence),
                "dependency_evidence": [item.model_dump(mode="json") for item in dependency_nodes],
                "validation_feedback": validation_feedback,
            },
            prompt_extension=type_prompt.read_text(encoding="utf-8"),
            ontology_asset_type=self.prompt_variant,
        )


class LLMReconciliationPairAnalyzer(_Agent):
    def __init__(self, llm, trace_sink=None, *, compact_payload: bool = False):
        super().__init__(llm, trace_sink)
        self.compact_payload = compact_payload

    async def analyze(
        self, left, right, evidence, policy, *, node_type: str | None = None, dependency_nodes=()
    ):
        evidence_payload = [item.model_dump(mode="json") for item in evidence]
        dependency_payload = [item.model_dump(mode="json") for item in dependency_nodes]
        payload = {
            "identity_policy": policy.model_dump(mode="json"),
            "left_node": left.model_dump(mode="json"),
            "right_node": right.model_dump(mode="json"),
            "evidence": evidence_payload,
            "dependency_evidence": dependency_payload,
        }
        if self.compact_payload:
            referenced_ids = set(
                payload["left_node"]["content"]["properties"].get("references", [])
                + payload["right_node"]["content"]["properties"].get("references", [])
            )
            payload.pop("identity_policy")
            payload["evidence"] = [
                {"content": {"question": item["content"].get("question")}}
                for item in evidence_payload
            ]
            payload["dependency_evidence"] = [
                item for item in dependency_payload if item["id"] in referenced_ids
            ]
            if node_type == "metric":
                for key in ("left_node", "right_node"):
                    payload[key]["content"]["properties"].pop("sql_generation_plans", None)
                    payload[key]["content"]["properties"].pop("physical_dependencies", None)
        return await self._run(
            "reconcile_semantic_pair",
            ReconciliationPairDecision,
            payload,
        )


class LLMReconciliationOutcomeComposer(_Agent):
    async def compose(
        self,
        group,
        evidence,
        pair_decisions,
        *,
        requested_results=None,
        input_analysis=None,
        validation_feedback=None,
    ):
        members = []
        for member in group.members:
            compact = member.model_dump(mode="json")
            if group.node_type == "metric":
                compact["content"]["properties"].pop("sql_generation_plans", None)
                compact["content"]["properties"].pop("physical_dependencies", None)
            members.append(compact)
        return await self._run(
            "reconcile_from_pairs",
            ReconcileOutcome,
            {
                "group": {
                    "node_type": group.node_type,
                    "members": members,
                },
                "questions": [
                    {
                        "source": item.source.model_dump(mode="json"),
                        "question": item.content.get("question"),
                    }
                    for item in evidence
                ],
                "pair_decisions": [
                    {
                        key: value
                        for key, value in decision.model_dump(mode="json").items()
                        if key != "reason"
                    }
                    for decision in pair_decisions
                ],
                "requested_results": (
                    requested_results.model_dump(mode="json")
                    if requested_results is not None
                    else None
                ),
                "input_analysis": (
                    input_analysis.model_dump(mode="json") if input_analysis is not None else None
                ),
                "validation_feedback": validation_feedback,
            },
        )


class LLMReconciliationInputAnalyzer(_Agent):
    async def analyze(self, group, evidence, *, validation_feedback=None):
        evidence_by_source = {item.source: item for item in evidence}
        return await self._run(
            "reconcile_input_analysis",
            ReconciliationInputAnalysis,
            {
                "node_type": group.node_type,
                "members": [
                    {
                        "ref": member.ref.model_dump(mode="json"),
                        "description": member.content.description,
                        "properties": {
                            key: value
                            for key, value in member.content.properties.items()
                            if not (
                                group.node_type == "metric"
                                and key in {"sql_generation_plans", "physical_dependencies"}
                            )
                        },
                        "sources": [source.model_dump(mode="json") for source in member.sources],
                        "questions": [
                            evidence_by_source[source].content.get("question")
                            for source in member.sources
                            if source in evidence_by_source
                            and evidence_by_source[source].content.get("question")
                        ],
                    }
                    for member in group.members
                ],
                "validation_feedback": validation_feedback,
            },
        )


class LLMCostSensitiveNodeReconciler:
    """Prepare exact observations, then spend pair calls only on risky merges."""

    def __init__(
        self,
        llm: BaseChatModel,
        trace_sink: GraphMergeAgentTraceSink | None = None,
        *,
        prompt_variant: str | None = None,
    ):
        self.analyzer = LLMReconciliationInputAnalyzer(llm, trace_sink)
        self.composer = LLMReconciliationOutcomeComposer(llm, trace_sink)
        self.pair_analyzer = LLMReconciliationPairAnalyzer(llm, trace_sink, compact_payload=True)
        self.metric_scope_arbiter = LLMMetricScopePairArbiter(llm, trace_sink)
        self.fallback = LLMNodeReconciler(llm, trace_sink, prompt_variant=prompt_variant)

    async def reconcile(
        self,
        group,
        evidence,
        policy,
        *,
        dependency_nodes=(),
        validation_feedback=None,
    ) -> ReconcileOutcome:
        if isinstance(group, BranchReconciliationGroup):
            return await self.fallback.reconcile(
                group,
                evidence,
                policy,
                dependency_nodes=dependency_nodes,
                validation_feedback=validation_feedback,
            )
        if not isinstance(group, FinalNodeGroup):
            raise TypeError("expected a final or branch reconciliation group")
        if validation_feedback is not None:
            return await self.fallback.reconcile(
                group,
                evidence,
                policy,
                dependency_nodes=dependency_nodes,
                validation_feedback=validation_feedback,
            )
        analysis = await self.analyzer.analyze(group, evidence)
        if not _complete_input_analysis(analysis, group):
            analysis = await self.analyzer.analyze(
                group,
                evidence,
                validation_feedback={
                    "expected_refs": [
                        member.ref.model_dump(mode="json") for member in group.members
                    ],
                    "previous_analysis": analysis.model_dump(mode="json"),
                },
            )
        if not _complete_input_analysis(analysis, group):
            return await self.fallback.reconcile(
                group,
                evidence,
                policy,
                dependency_nodes=dependency_nodes,
                validation_feedback=validation_feedback,
            )
        outcome = await self.composer.compose(
            group,
            evidence,
            (),
            requested_results=analysis.requested_results,
            input_analysis=analysis,
            validation_feedback=validation_feedback,
        )
        outcome = complete_single_shared_group(outcome, group.members)
        outcome = compile_single_conflict_point(outcome, group.members)
        review_sets = _merged_operand_review_sets(outcome, analysis)
        if not review_sets:
            return outcome
        review_pair_count = sum(len(refs) * (len(refs) - 1) // 2 for refs in review_sets)
        if review_pair_count > 30:
            raise ValueError("merged actions need more than 30 pair reviews")
        by_ref = {member.ref: member for member in group.members}
        decisions = []
        for refs in review_sets:
            for index, left_ref in enumerate(refs):
                for right_ref in refs[index + 1 :]:
                    left = by_ref[left_ref]
                    right = by_ref[right_ref]
                    decision = await self.pair_analyzer.analyze(
                        left,
                        right,
                        evidence,
                        policy,
                        node_type=group.node_type,
                        dependency_nodes=dependency_nodes,
                    )
                    if group.node_type == "metric" and decision.resolution == "conflict":
                        scope = await self.metric_scope_arbiter.analyze(
                            left,
                            right,
                            evidence,
                            dependency_nodes=dependency_nodes,
                        )
                        if scope.resolution in {"equivalent", "generalize", "refinement"}:
                            decision = scope
                    decisions.append(decision)
        if all(
            decision.resolution in {"equivalent", "generalize", "refinement"}
            for decision in decisions
        ):
            return outcome
        outcome = await self.composer.compose(
            group,
            evidence,
            tuple(decisions),
            requested_results=analysis.requested_results,
            input_analysis=analysis,
            validation_feedback=validation_feedback,
        )
        outcome = complete_single_shared_group(outcome, group.members)
        outcome = compile_single_conflict_point(outcome, group.members)
        group_by_ref = {
            ref: index for index, item in enumerate(outcome.groups) for ref in item.supporting_refs
        }
        if any(
            decision.resolution == "conflict"
            and group_by_ref.get(decision.left_ref) == group_by_ref.get(decision.right_ref)
            for decision in decisions
        ):
            conflicts = tuple(
                decision.model_dump(mode="json")
                for decision in decisions
                if decision.resolution == "conflict"
                and group_by_ref.get(decision.left_ref) == group_by_ref.get(decision.right_ref)
            )
            return await self.fallback.reconcile(
                group,
                evidence,
                policy,
                dependency_nodes=dependency_nodes,
                validation_feedback={
                    "contract_errors": ["PAIR_REVIEW_CONTRADICTION"],
                    "reviewed_conflicts": list(conflicts),
                    "previous_outcome": outcome.model_dump(mode="json"),
                    "instruction": (
                        "Reconcile the complete group independently. The cost-sensitive pair "
                        "review found a conflict that its composer left in one group; preserve "
                        "the compared behaviors without assuming either intermediate decision "
                        "is authoritative."
                    ),
                },
            )
        return outcome


def _complete_input_analysis(analysis: ReconciliationInputAnalysis, group: FinalNodeGroup):
    expected = {member.ref for member in group.members}
    observed = [item.representation_ref for item in analysis.nodes]
    slotted = [ref for slot in analysis.requested_results.slots for ref in slot.member_refs]
    return (
        len(observed) == len(expected)
        and set(observed) == expected
        and len(slotted) == len(expected)
        and set(slotted) == expected
    )


def _merged_operand_review_sets(outcome: ReconcileOutcome, analysis: ReconciliationInputAnalysis):
    operands = {item.representation_ref: item.exact_operands for item in analysis.nodes}
    slot_by_ref = {
        ref: index
        for index, slot in enumerate(analysis.requested_results.slots)
        for ref in slot.member_refs
    }
    review_sets = []
    for group in outcome.groups:
        by_slot: dict[int, list] = {}
        for ref in group.supporting_refs:
            by_slot.setdefault(slot_by_ref[ref], []).append(ref)
        review_sets.extend(
            tuple(refs)
            for refs in by_slot.values()
            if len(refs) > 1 and len({operands[ref] for ref in refs}) > 1
        )
    return tuple(review_sets)


class LLMMetricScopePairArbiter(_Agent):
    async def analyze(self, left, right, evidence, *, dependency_nodes=()):
        return await self._run(
            "reconcile_metric_scope_pair",
            ReconciliationPairDecision,
            {
                "left_node": left.model_dump(mode="json"),
                "right_node": right.model_dump(mode="json"),
                "evidence": [
                    {
                        "content": {
                            "question": item.model_dump(mode="json")["content"].get("question")
                        }
                    }
                    for item in evidence
                ],
                "dependency_evidence": [item.model_dump(mode="json") for item in dependency_nodes],
            },
        )


class LLMKnowledgeGeneralizer(_Agent):
    def __init__(self, llm, trace_sink=None, *, prompt_variant: str | None = None):
        super().__init__(llm, trace_sink)
        self.prompt_variant = prompt_variant

    async def generalize(
        self,
        *,
        baseline,
        representations,
        evidence_by_ref,
        node_type,
        identity_policy,
        mode,
        shared,
        residuals,
        residual_relations,
        validation_feedback=None,
        dependency_nodes=(),
    ) -> GeneralizationProposal:
        prompt_root = files("cortex.capabilities.graph_merge.llm.prompts")
        type_prompt = (
            prompt_root.joinpath("generalize_types", f"{self.prompt_variant}.md")
            if self.prompt_variant
            else None
        )
        extension = type_prompt.read_text(encoding="utf-8") if type_prompt else ""
        baseline_payload = baseline.model_dump(mode="json")
        representation_payloads = [item.model_dump(mode="json") for item in representations]
        if node_type.id == "metric":
            legacy_values = [
                baseline_payload["properties"].get("physical_dependencies"),
                *(
                    item["content"]["properties"].get("physical_dependencies")
                    for item in representation_payloads
                ),
            ]
            if any(value not in (None, []) for value in legacy_values):
                raise ValueError("nonempty legacy metric physical_dependencies needs migration")
            baseline_payload["properties"].pop("physical_dependencies", None)
            for item in representation_payloads:
                item["content"]["properties"].pop("physical_dependencies", None)
        return await self._run(
            "generalize",
            GeneralizationProposal,
            {
                "baseline": baseline_payload,
                "representations": representation_payloads,
                "evidence_by_ref": _evidence_payload(evidence_by_ref),
                "node_type": node_type.model_dump(mode="json"),
                "identity_policy": identity_policy.model_dump(mode="json"),
                "mode": mode,
                "shared": [item.model_dump(mode="json") for item in shared],
                "residuals": [item.model_dump(mode="json") for item in residuals],
                "residual_relations": [item.model_dump(mode="json") for item in residual_relations],
                "validation_feedback": validation_feedback,
                "request_scope_candidates": representation_scope_candidates(
                    representations,
                    tuple(item for values in evidence_by_ref.values() for item in values),
                ),
                "dependency_evidence": [item.model_dump(mode="json") for item in dependency_nodes],
            },
            prompt_extension=extension,
            ontology_asset_type=self.prompt_variant,
        )


class LLMFactFuser(_Agent):
    def __init__(self, llm, trace_sink=None, *, prompt_variant: str | None = None):
        super().__init__(llm, trace_sink)
        self.prompt_variant = prompt_variant

    async def fuse(
        self,
        *,
        mode,
        baseline,
        representations,
        evidence_by_ref,
        node_type,
        shared,
        residuals,
        validation_feedback=None,
        dependency_nodes=(),
    ) -> FusionProposal:
        prompt_root = files("cortex.capabilities.graph_merge.llm.prompts")
        type_prompt = (
            prompt_root.joinpath("fuse_types", f"{self.prompt_variant}.md")
            if self.prompt_variant
            else None
        )
        extension = type_prompt.read_text(encoding="utf-8") if type_prompt else ""
        return await self._run(
            "fuse",
            FusionProposal,
            {
                "mode": mode,
                "baseline": baseline.model_dump(mode="json"),
                "representations": [item.model_dump(mode="json") for item in representations],
                "evidence_by_ref": _evidence_payload(evidence_by_ref),
                "node_type": node_type.model_dump(mode="json"),
                "shared": [item.model_dump(mode="json") for item in shared],
                "residuals": [item.model_dump(mode="json") for item in residuals],
                "validation_feedback": validation_feedback,
                "dependency_evidence": [item.model_dump(mode="json") for item in dependency_nodes],
            },
            prompt_extension=extension,
        )


class LLMGeneralizationCoverageVerifier(_Agent):
    async def verify(
        self, *, proposal, representations, evidence_by_ref, node_type, dependency_nodes=()
    ) -> GeneralizationVerification:
        proposal_payload = proposal.model_dump(mode="json")
        proposal_payload.pop("reason", None)
        evidence = tuple(item for items in evidence_by_ref.values() for item in items)
        return await self._run(
            "coverage_verifier",
            GeneralizationVerification,
            {
                "proposal": proposal_payload,
                "representations": [item.model_dump(mode="json") for item in representations],
                "evidence_by_ref": _evidence_payload(evidence_by_ref),
                "request_scope_candidates": representation_scope_candidates(
                    representations, evidence
                ),
                "node_type": node_type.model_dump(mode="json"),
                "dependency_evidence": [item.model_dump(mode="json") for item in dependency_nodes],
                "source_checklist": [
                    {"supporting_ref": item.ref.model_dump(mode="json"), "evidence_id": ev.id}
                    for item in representations
                    for ev in evidence_by_ref.get(item.ref, ())
                ],
                "input_sql_generation_plans": [
                    {
                        "representation_ref": item.ref.model_dump(mode="json"),
                        "plans": item.content.properties.get("sql_generation_plans", []),
                    }
                    for item in representations
                ],
            },
        )


def _consolidation_context(group, schema, evidence_by_ref, dependency_nodes, dependency_conflicts):
    return {
        "group": group.model_dump(mode="json"),
        "node_type": schema.node_type_index()[group.node_type].model_dump(
            mode="json", exclude={"fields"}
        ),
        "effective_fields": {
            key: field.model_dump(mode="json")
            for key, field in schema.effective_node_fields(group.node_type).items()
        },
        "evidence_by_ref": _evidence_payload(evidence_by_ref),
        "dependency_evidence": [n.model_dump(mode="json") for n in dependency_nodes],
        "dependency_conflicts": [
            c.model_dump(mode="json", exclude={"consolidation"}) for c in dependency_conflicts
        ],
        "request_scope_candidates": representation_scope_candidates(
            group.members, tuple(ev for items in evidence_by_ref.values() for ev in items)
        ),
    }


class LLMGroupConsolidator(_Agent):
    async def propose(
        self, *, group, schema, evidence_by_ref, dependency_nodes=(), dependency_conflicts=()
    ):
        return await self._run(
            "consolidate",
            ConsolidationProposal,
            _consolidation_context(
                group, schema, evidence_by_ref, dependency_nodes, dependency_conflicts
            ),
            ontology_asset_type=group.node_type,
        )


class LLMConsolidationChecker(_Agent):
    async def check(
        self,
        *,
        proposal,
        group,
        schema,
        evidence_by_ref,
        dependency_nodes=(),
        dependency_conflicts=(),
    ):
        from cortex.capabilities.graph_merge.reconciliation.proposal import review_checklists

        return await self._run(
            "consolidation_check",
            ConsolidationReview,
            {
                **_consolidation_context(
                    group, schema, evidence_by_ref, dependency_nodes, dependency_conflicts
                ),
                "proposal": proposal.model_dump(mode="json"),
                **review_checklists(proposal, group, evidence_by_ref),
            },
            ontology_asset_type=group.node_type,
        )


class LLMConflictBranchSelector(_Agent):
    async def select(
        self, incoming, alternatives, evidence, node_type, identity_policy
    ) -> BranchSelection:
        return await self._run(
            "branch",
            BranchSelection,
            {
                "incoming": incoming.model_dump(mode="json"),
                "alternatives": [item.model_dump(mode="json") for item in alternatives],
                "evidence": [item.model_dump(mode="json") for item in evidence],
                "node_type": node_type.model_dump(mode="json"),
                "identity_policy": identity_policy.model_dump(mode="json"),
            },
        )


class LLMConceptResolver(_Agent):
    async def resolve(self, subgraph, schema, *, allow_grounding_removal) -> ConceptMinerOutcome:
        return await self._run(
            "concept_mine",
            ConceptMinerOutcome,
            {
                "subgraph": subgraph.model_dump(mode="json"),
                "edge_types": [
                    item.model_dump(mode="json")
                    for item in schema.edge_types
                    if item.id in {"broader_than", "maps_to_value", "maps_to_column"}
                ],
                "allow_grounding_removal": allow_grounding_removal,
            },
        )


def _evidence_payload(evidence_by_ref) -> list[dict[str, Any]]:
    return [
        {
            "representation_ref": ref.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
        }
        for ref, evidence in evidence_by_ref.items()
    ]


def _refs(payload: Any) -> set[str]:
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    return set(__import__("re").findall(r'"ref_id"\s*:\s*"([^"]+)"', serialized))
