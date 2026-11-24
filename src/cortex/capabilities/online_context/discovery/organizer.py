from __future__ import annotations

import re

from langchain_core.language_models.chat_models import BaseChatModel

from cortex.application.online_context import (
    ChoicePoint,
    InteractionOption,
)
from cortex.capabilities.online_context.discovery.candidates import (
    _deduplicate_slots,
    _merge_evidence_assessments,
    _merge_options,
)
from cortex.capabilities.online_context.discovery.evidence import (
    _analysis_graph_payload,
    _graph_ids,
    _graph_ref_payloads,
    _physical_ref,
    _replace_refs,
    _residual_graph_payload,
    _union,
    _validate_candidate_references,
    _validate_slots,
)
from cortex.capabilities.online_context.discovery.join_paths import (
    discovery_join_candidates,
)
from cortex.capabilities.online_context.discovery.models import (
    CandidateDraft,
    ChoiceOrganizationResult,
    CrossNodeOutput,
    InitialSqlMemoryOutput,
    IntraNodeOutput,
    SlotDraft,
    SlotRelevanceOutput,
)
from cortex.capabilities.online_context.llm.prompts import load_prompt
from cortex.infrastructure.llm import run_llm_extraction
from cortex.schema import GraphState
from cortex.schema.bindings import target_groups


class _GraphRefHandles:
    """A run-local LLM vocabulary for otherwise stable, verbose Graph references."""

    def __init__(self, refs: set[str]) -> None:
        ordered = tuple(sorted(refs))
        self._to_handle = {ref: f"g{index:05d}" for index, ref in enumerate(ordered, 1)}
        self._to_ref = {handle: ref for ref, handle in self._to_handle.items()}

    @classmethod
    def for_state(cls, state: GraphState) -> _GraphRefHandles:
        return cls(_graph_ids(state))

    def encode(self, value):
        return _replace_refs(value, self._to_handle)

    def decode(self, value):
        # Known canonical IDs remain accepted for deterministic test doubles and
        # backwards-compatible callers, but the LLM payload exposes handles only.
        return _replace_refs(value, self._to_ref)


class LlmChoiceOrganizer:
    """Accumulate grounded choices, including JOINs, through three discovery stages."""

    def __init__(
        self, llm: BaseChatModel, *, trace_sink=None, audit_slots: bool = False,
        intra_batch_size: int | None = None, intra_checkpoint_root=None,
    ) -> None:
        self._llm = llm
        self._trace_sink = trace_sink
        self._audit_slots = audit_slots
        self._intra_batch_size = intra_batch_size
        self._intra_checkpoint_root = intra_checkpoint_root

    def _trace(self, stage, payload):
        if self._trace_sink is not None:
            self._trace_sink(stage, payload)

    def _normalize_proposals(self, output, state, initial_sql):
        normalized = _normalize_proposed_slots(output, state, initial_sql)
        field = "slots" if isinstance(output, IntraNodeOutput) else "new_slots"
        before, after = getattr(output, field), getattr(normalized, field)
        if before != after:
            self._trace("proposal_normalization", {
                "reason": "Exact duplicate implementations merge provenance; fewer than two distinct implementations is not a choice",
                "proposed": [s.model_dump(mode="json") for s in before],
                "retained_slot_ids": [s.slot_id for s in after],
            })
        return normalized

    async def organize(
        self,
        *,
        question: str,
        initial_sql: str | None,
        retrieved_graph_state: GraphState,
    ) -> ChoiceOrganizationResult:
        signals = _intra_node_signal_ids(retrieved_graph_state)
        graph_payload = _analysis_graph_payload(retrieved_graph_state)
        handles = _GraphRefHandles.for_state(retrieved_graph_state)
        llm_graph_payload = handles.encode(graph_payload)
        llm_signals = handles.encode(signals)
        raw_join_candidates = discovery_join_candidates(retrieved_graph_state)
        join_candidates = handles.encode(raw_join_candidates)
        self._trace(
            "organizer_input",
            {
                "question": question,
                "initial_sql": initial_sql,
                "retrieved_graph_state": llm_graph_payload,
                "signal_node_ids": llm_signals,
                "join_candidates": join_candidates,
            },
        )
        from .sql_fragments import sql_fragments, citation_schema
        fragments = sql_fragments(initial_sql)
        intra_trace = {}
        self._trace("intra_llm", intra_trace)

        def validate_intra_draft(result):
            decoded = IntraNodeOutput.model_validate(
                handles.decode(result.model_dump(mode="json"))
            )
            if any(c.initial_sql_evidence for slot in decoded.slots for c in slot.candidates):
                raise ValueError("Intra-node candidates cite memory/schema only; Initial-SQL attribution belongs to the comparison stage")
            _validate_intra(decoded, signals)
            decoded = _normalize_proposed_slots(decoded, retrieved_graph_state, initial_sql)
            _validate_slots(
                tuple(_materialize_slot(item) for item in decoded.slots),
                retrieved_graph_state,
                initial_sql,
            )

        def validate_augmentation_draft(result):
            decoded = type(result).model_validate(
                handles.decode(result.model_dump(mode="json"))
            )
            if any(d.slot_id in {s.choice_point_id for s in slots} for d in decoded.new_slots):
                raise ValueError("new_slots cannot replace a handled Slot; use slot_augmentations")
            decoded = _normalize_proposed_slots(decoded, retrieved_graph_state, initial_sql)
            if isinstance(decoded, InitialSqlMemoryOutput):
                _validate_initial_memory_new_slots(decoded)
            # Validate raw additions before a merge can mask a bad new SQL
            # fragment with an older valid citation on the same Candidate.
            evidence_items = [c for slot in decoded.new_slots for c in slot.candidates]
            for addition in decoded.slot_augmentations:
                evidence_items.extend(addition.new_candidates)
                evidence_items.extend(addition.provenance_additions)
            graph_ids = _graph_ids(retrieved_graph_state)
            for item in evidence_items:
                _validate_candidate_references(item, graph_ids, initial_sql)
            _validate_slots(
                _apply_augmentations(slots, decoded),
                retrieved_graph_state,
                initial_sql,
            )

        intra_arguments = dict(
            llm=self._llm,
            prompt=load_prompt("intra_node_conflicts"),
            output_model=IntraNodeOutput,
            output_schema=citation_schema(IntraNodeOutput),validation_context={"sql_fragments":{}},
            json_mode=True,
            repair_with_context=True,
            output_validator=validate_intra_draft,
            trace_capture=intra_trace,
            human_payload={
                "question": question,
                "initial_sql": initial_sql,
                "retrieved_graph_state": llm_graph_payload,
                "signal_node_ids": llm_signals,
                "join_candidates": join_candidates,
            },
        )
        if self._intra_batch_size is not None and len(signals)>self._intra_batch_size:
            from .intra_batches import extract_intra_batches
            intra = await extract_intra_batches(
                llm=self._llm,prompt=intra_arguments['prompt'],
                human_payload=intra_arguments['human_payload'],
                output_validator=validate_intra_draft,batch_size=self._intra_batch_size,
                checkpoint_root=self._intra_checkpoint_root,trace_sink=self._trace,
            )
        else:
            intra = await run_llm_extraction(**intra_arguments)
        self._trace("intra_raw", intra.model_dump(mode="json"))
        intra = IntraNodeOutput.model_validate(
            handles.decode(intra.model_dump(mode="json"))
        )
        intra = self._normalize_proposals(intra, retrieved_graph_state, initial_sql)
        intra = _render_intra_columns(intra, retrieved_graph_state)
        self._trace("intra_rendered", intra.model_dump(mode="json"))
        _validate_intra(intra, signals)
        slots = tuple(
            _materialize_slot(item)
            for item in intra.slots
        )
        slots = _deduplicate_slots(slots)
        _validate_slots(slots, retrieved_graph_state, initial_sql)
        if initial_sql:
            initial_memory_trace = {}
            self._trace("initial_sql_memory_llm", initial_memory_trace)
            initial_memory_payload = {
                "question": question,
                "initial_sql": initial_sql,
                "join_candidates": join_candidates,
                "handled_slots": handles.encode(
                    [_discovery_slot(item.model_dump(mode="json")) for item in slots]
                ),
                "residual_graph_state": handles.encode(
                    _residual_graph_payload(graph_payload, intra)
                ),
            }
            initial_memory = await run_llm_extraction(
                llm=self._llm,
                prompt=load_prompt("initial_sql_memory_conflicts"),
                output_model=InitialSqlMemoryOutput,
                output_schema=citation_schema(InitialSqlMemoryOutput),validation_context={"sql_fragments":fragments},
                json_mode=True,
                repair_with_context=True,
                output_validator=validate_augmentation_draft,
                trace_capture=initial_memory_trace,
                human_payload={**initial_memory_payload,"initial_sql_fragments":fragments},
            )
            initial_memory = InitialSqlMemoryOutput.model_validate(
                handles.decode(initial_memory.model_dump(mode="json"))
            )
            initial_memory = self._normalize_proposals(initial_memory, retrieved_graph_state, initial_sql)
            self._trace(
                "initial_sql_memory_raw", initial_memory.model_dump(mode="json")
            )
            _validate_initial_memory_new_slots(initial_memory)
            slots = _apply_augmentations(slots, initial_memory)
            slots = _render_choice_point_columns(slots, retrieved_graph_state)
            slots = _deduplicate_slots(slots)
            _validate_slots(slots, retrieved_graph_state, initial_sql)

        cross_trace = {}
        self._trace("cross_llm", cross_trace)
        cross_payload = {
            "question": question,
            "join_candidates": join_candidates,
            "handled_slots": handles.encode(
                [_discovery_slot(item.model_dump(mode="json")) for item in slots]
            ),
            "residual_graph_state": handles.encode(
                _residual_graph_payload(graph_payload, intra)
            ),
        }
        cross = await run_llm_extraction(
            llm=self._llm,
            prompt=load_prompt("cross_node_conflicts"),
            output_model=CrossNodeOutput,
            output_schema=citation_schema(CrossNodeOutput),validation_context={"sql_fragments":fragments},
            json_mode=True,
            repair_with_context=True,
            output_validator=validate_augmentation_draft,
            trace_capture=cross_trace,
            human_payload={**cross_payload,"initial_sql_fragments":fragments},
        )
        cross = CrossNodeOutput.model_validate(
            handles.decode(cross.model_dump(mode="json"))
        )
        cross = self._normalize_proposals(cross, retrieved_graph_state, initial_sql)
        self._trace("cross_raw", cross.model_dump(mode="json"))
        combined = _apply_augmentations(slots, cross)
        combined = _render_choice_point_columns(combined, retrieved_graph_state)
        self._trace(
            "slots_before_validation", [s.model_dump(mode="json") for s in combined]
        )
        combined = _deduplicate_slots(combined)
        _validate_slots(combined, retrieved_graph_state, initial_sql)
        assessments = intra.assessments
        if combined and self._audit_slots:
            audit = await run_llm_extraction(
                llm=self._llm,
                prompt=load_prompt("audit_slot_relevance"),
                output_model=SlotRelevanceOutput,
                human_payload={
                    "question": question,
                    "initial_sql": initial_sql,
                    "slots": [item.model_dump(mode="json") for item in combined],
                    "retrieved_graph_state": graph_payload,
                    "join_candidates": raw_join_candidates,
                },
            )
            expected = {item.choice_point_id for item in combined}
            if (
                len(audit.decisions) != len(expected)
                or {item.slot_id for item in audit.decisions} != expected
            ):
                raise ValueError(
                    "slot audit must assess every conflict point exactly once"
                )
            kept = expected - {
                item.slot_id for item in audit.decisions if not item.keep
            }
            self._trace("slot_relevance_audit", audit.model_dump(mode="json"))
            combined = tuple(item for item in combined if item.choice_point_id in kept)
        return ChoiceOrganizationResult(
            choice_points=combined,
            intra_node_assessments=assessments,
        )

def _intra_node_signal_ids(state: GraphState) -> tuple[str, ...]:
    conflict_ids = {item.id for item in state.conflict_nodes}
    multi_target_ids = {
        node.id
        for node in state.graph.nodes
        if node.node_type == "concept"
        and node.id not in conflict_ids
        and len(target_groups(node.properties)) >= 2
    }
    return tuple(sorted(conflict_ids | multi_target_ids))


def _normalize_proposed_slots(output, state, initial_sql):
    """Validate every original citation before merging exact textual duplicates.

    This is not semantic equivalence or answer selection. The final ChoicePoint
    still requires at least two implementations. Never erase invalid evidence
    merely because its containing proposal would otherwise be omitted.
    """
    field = "slots" if isinstance(output, IntraNodeOutput) else "new_slots"
    proposals = getattr(output, field)
    if len({s.slot_id for s in proposals}) != len(proposals):
        raise ValueError("proposed slot IDs must be unique")
    ids = _graph_ids(state)
    kept = []
    for slot in proposals:
        if len({c.candidate_id for c in slot.candidates}) != len(slot.candidates):
            raise ValueError("candidate IDs must be unique within a slot")
        grouped = {}
        for candidate in slot.candidates:
            _validate_candidate_references(candidate, ids, initial_sql)
            key = candidate.statement.strip()
            if not key:
                raise ValueError("candidate statement must not be empty")
            previous = grouped.get(key)
            if previous is None:
                grouped[key] = candidate
                continue
            refs = _union((previous.graph_refs, candidate.graph_refs))
            fragment = previous.initial_sql_evidence or candidate.initial_sql_evidence
            grouped[key] = previous.model_copy(update={
                "graph_refs": refs,
                "initial_sql_evidence": fragment,
                "sources": tuple(s for s, present in (
                    ("memory_graph", bool(refs)), ("initial_sql", bool(fragment))
                ) if present),
                "evidence_assessment": _merge_evidence_assessments(
                    previous.evidence_assessment, candidate.evidence_assessment
                ),
            })
        if len(grouped) >= 2:
            kept.append(slot.model_copy(update={"candidates": tuple(grouped.values())}))
    updates = {field: tuple(kept)}
    if isinstance(output, IntraNodeOutput):
        retained = {s.slot_id for s in kept}
        updates["assessments"] = tuple(a.model_copy(update={
            "slot_ids": tuple(s for s in a.slot_ids if s in retained),
            **({"status": "not_conflicting"} if a.slot_ids and not set(a.slot_ids) & retained else {}),
        }) for a in output.assessments)
    return output.model_copy(update=updates)


def _validate_intra(
    output: IntraNodeOutput,
    signal_ids: tuple[str, ...],
) -> None:
    slot_ids = {item.slot_id for item in output.slots}
    if len(slot_ids) != len(output.slots):
        raise ValueError("intra-node slot IDs must be unique")
    for assessment in output.assessments:
        if not set(assessment.slot_ids) <= slot_ids:
            raise ValueError("intra-node assessment references an unknown slot")


def _materialize_candidate(item: CandidateDraft) -> InteractionOption:
    return InteractionOption(
        option_id=item.candidate_id,
        statement=item.statement,
        candidate_sources=tuple(
            source
            for source, present in (
                ("memory_graph", bool(item.graph_refs)),
                ("initial_sql", bool(item.initial_sql_evidence)),
            )
            if present
        ),
        graph_refs=item.graph_refs,
        initial_sql_evidence=item.initial_sql_evidence,
        evidence_assessment=item.evidence_assessment,
    )


def _materialize_slot(slot: SlotDraft) -> ChoicePoint:
    return ChoicePoint(
        choice_point_id=slot.slot_id,
        scope=slot.scope,
        decision_question=slot.decision_question,
        selection_mode="multiple",
        options=tuple(_materialize_candidate(item) for item in slot.candidates),
    )


def _render_intra_columns(
    output: IntraNodeOutput, state: GraphState
) -> IntraNodeOutput:
    payloads = _graph_ref_payloads(state)
    return output.model_copy(
        update={
            "slots": tuple(
                slot.model_copy(
                    update={
                        "candidates": tuple(
                            candidate.model_copy(
                                update={
                                    "statement": _render_statement_columns(
                                        candidate.statement,
                                        payloads,
                                    )
                                }
                            )
                            for candidate in slot.candidates
                        )
                    }
                )
                for slot in output.slots
            )
        }
    )


def _render_choice_point_columns(
    points: tuple[ChoicePoint, ...], state: GraphState
) -> tuple[ChoicePoint, ...]:
    payloads = _graph_ref_payloads(state)
    return tuple(
        point.model_copy(
            update={
                "options": tuple(
                    option.model_copy(
                        update={
                            "statement": _render_statement_columns(
                                option.statement, payloads
                            )
                        }
                    )
                    for option in point.options
                )
            }
        )
        for point in points
    )


def _render_statement_columns(statement: str, payloads: dict[str, object]) -> str:
    replacements: dict[str, str] = {}
    for payload in payloads.values():
        if not isinstance(payload, dict) or payload.get("node_type") != "column":
            continue
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            continue
        table_ref = str(properties.get("table_ref") or "")
        column_name = str(properties.get("column_name") or "")
        if not table_ref.startswith("table.") or not column_name:
            continue
        table_name = table_ref.removeprefix("table.")
        rendered_column = (
            column_name
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column_name)
            else f"`{column_name}`"
        )
        replacements[_physical_ref(table_name, column_name)] = (
            f"{table_name}.{rendered_column}"
        )

    def replace(match: re.Match[str]) -> str:
        table, quoted, backticked, plain = match.groups()
        column = quoted or backticked or plain
        return replacements.get(_physical_ref(table, column), match.group(0))

    return re.sub(
        r"(?:(?:column|table)\.)?([A-Za-z_][A-Za-z0-9_]*)\."
        r'(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))',
        replace,
        statement,
    )


def _apply_augmentations(
    slots: tuple[ChoicePoint, ...], output: InitialSqlMemoryOutput | CrossNodeOutput
) -> tuple[ChoicePoint, ...]:
    by_id = {item.choice_point_id: item for item in slots}
    if len(by_id) != len(slots):
        raise ValueError("handled slot IDs must be unique")
    for augmentation in output.slot_augmentations:
        if augmentation.slot_id not in by_id:
            raise ValueError("cross-node augmentation references an unknown slot")
        slot = by_id[augmentation.slot_id]
        options = {item.option_id: item for item in slot.options}
        for addition in augmentation.provenance_additions:
            if addition.candidate_id not in options:
                raise ValueError("provenance addition references an unknown candidate")
            current = options[addition.candidate_id]
            graph_refs = _union((current.graph_refs, addition.graph_refs))
            sql_evidence = current.initial_sql_evidence or addition.initial_sql_evidence
            assessment = _merge_evidence_assessments(
                current.evidence_assessment, addition.evidence_assessment
            )
            sources = tuple(
                source
                for source, present in (
                    ("memory_graph", bool(graph_refs)),
                    ("initial_sql", bool(sql_evidence)),
                )
                if present
            )
            options[addition.candidate_id] = current.model_copy(
                update={
                    "graph_refs": graph_refs,
                    "initial_sql_evidence": sql_evidence,
                    "evidence_assessment": assessment,
                    "candidate_sources": sources,
                }
            )
        for draft in augmentation.new_candidates:
            option = _materialize_candidate(draft)
            if draft.candidate_id in options:
                options[option.option_id] = _merge_options(
                    options[option.option_id], option
                )
                continue
            options[option.option_id] = option
        by_id[augmentation.slot_id] = slot.model_copy(
            update={"options": tuple(options.values())}
        )
    for draft in output.new_slots:
        slot = _materialize_slot(draft)
        if slot.choice_point_id in by_id:
            raise ValueError(
                f"{slot.choice_point_id}: new_slots cannot replace a handled Slot; use slot_augmentations"
            )
        by_id[slot.choice_point_id] = slot
    from .contracts import validate_initial_sql_sources
    validate_initial_sql_sources(tuple(by_id.values()))
    return tuple(by_id.values())


def _validate_initial_memory_new_slots(output: InitialSqlMemoryOutput) -> None:
    for slot in output.new_slots:
        sources = {
            source
            for candidate in slot.candidates
            for source in candidate.evidence_assessment.source_types
        }
        if "initial_sql" not in sources or not sources & {
            "ontology_asset",
            "database_schema",
        }:
            raise ValueError(
                "an Initial-SQL-versus-Memory Slot requires Initial SQL and Memory Candidates"
            )


__all__ = ["LlmChoiceOrganizer"]


def _discovery_slot(value):
    if isinstance(value, dict):
        return {k: _discovery_slot(v) for k,v in value.items() if k != 'candidate_sources'}
    if isinstance(value, list): return [_discovery_slot(v) for v in value]
    return value
