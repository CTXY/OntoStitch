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
    _dict_list,
    _graph_ids,
    _graph_ref_payloads,
    _physical_ref,
    _replace_refs,
    _residual_graph_payload,
    _union,
    _validate_slots,
)
from cortex.capabilities.online_context.discovery.join_paths import discovery_join_candidates
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
from cortex.infrastructure.llm import LLMStructuredOutputError, run_llm_extraction
from cortex.schema import GraphState


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

    def __init__(self, llm: BaseChatModel, *, trace_sink=None, audit_slots: bool = False) -> None:
        self._llm = llm
        self._trace_sink = trace_sink
        self._audit_slots = audit_slots

    def _trace(self, stage, payload):
        if self._trace_sink is not None:
            self._trace_sink(stage, payload)

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
        intra_trace = {}
        self._trace("intra_llm", intra_trace)
        intra = await run_llm_extraction(
            llm=self._llm,
            prompt=load_prompt("intra_node_conflicts"),
            output_model=IntraNodeOutput,
            trace_capture=intra_trace,
            human_payload={
                "question": question,
                "initial_sql": initial_sql,
                "retrieved_graph_state": llm_graph_payload,
                "signal_node_ids": llm_signals,
                "join_candidates": join_candidates,
            },
        )
        self._trace("intra_raw", intra.model_dump(mode="json"))
        intra = IntraNodeOutput.model_validate(handles.decode(intra.model_dump(mode="json")))
        intra = _render_intra_columns(intra, retrieved_graph_state)
        intra = _reconcile_intra_slot_ids(intra)
        self._trace("intra_rendered", intra.model_dump(mode="json"))
        _validate_intra(intra, signals)
        slots = tuple(
            _materialize_slot(item) for item in intra.slots if _draft_slot_has_provenance(item)
        )
        slots = _deduplicate_slots(slots)
        slots = self._retain_valid_slots(
            "intra_rejected_slot", slots, retrieved_graph_state, initial_sql
        )
        if initial_sql:
            initial_memory_trace = {}
            self._trace("initial_sql_memory_llm", initial_memory_trace)
            initial_memory_payload = {
                "question": question,
                "initial_sql": initial_sql,
                "join_candidates": join_candidates,
                "handled_slots": handles.encode([item.model_dump(mode="json") for item in slots]),
                "residual_graph_state": handles.encode(
                    _residual_graph_payload(graph_payload, intra)
                ),
            }
            for attempt in range(2):
                try:
                    initial_memory = await run_llm_extraction(
                        llm=self._llm,
                        prompt=load_prompt("initial_sql_memory_conflicts"),
                        output_model=InitialSqlMemoryOutput,
                        trace_capture=initial_memory_trace,
                        human_payload=initial_memory_payload,
                    )
                    initial_memory = InitialSqlMemoryOutput.model_validate(
                        handles.decode(initial_memory.model_dump(mode="json"))
                    )
                    break
                except LLMStructuredOutputError:
                    if attempt == 1:
                        raise
                    self._trace("initial_sql_memory_structured_retry", {"attempt": attempt + 2})
                    initial_memory_payload = {
                        **initial_memory_payload,
                        "format_feedback": (
                            "Every new Slot must contain at least two grounded, substitutable "
                            "Candidates. If only one Candidate is available, remove that Slot."
                        ),
                    }
            self._trace("initial_sql_memory_raw", initial_memory.model_dump(mode="json"))
            _validate_initial_memory_new_slots(initial_memory)
            slots = _apply_augmentations(slots, initial_memory)
            slots = _render_choice_point_columns(slots, retrieved_graph_state)
            slots = _deduplicate_slots(slots)
            slots = self._retain_valid_slots(
                "initial_sql_memory_rejected_slot",
                slots,
                retrieved_graph_state,
                initial_sql,
            )

        cross_trace = {}
        self._trace("cross_llm", cross_trace)
        cross_payload = {
            "question": question,
            "join_candidates": join_candidates,
            "handled_slots": handles.encode([item.model_dump(mode="json") for item in slots]),
            "residual_graph_state": handles.encode(_residual_graph_payload(graph_payload, intra)),
        }
        for attempt in range(2):
            try:
                cross = await run_llm_extraction(
                    llm=self._llm,
                    prompt=load_prompt("cross_node_conflicts"),
                    output_model=CrossNodeOutput,
                    trace_capture=cross_trace,
                    human_payload=cross_payload,
                )
                cross = CrossNodeOutput.model_validate(
                    handles.decode(cross.model_dump(mode="json"))
                )
                break
            except LLMStructuredOutputError:
                if attempt == 1:
                    raise
                self._trace("cross_structured_retry", {"attempt": attempt + 2})
                cross_payload = {
                    **cross_payload,
                    "format_feedback": (
                        "Every new Slot must contain at least two grounded, substitutable "
                        "Candidates. If only one Candidate is available, remove that Slot."
                    ),
                }
        self._trace("cross_raw", cross.model_dump(mode="json"))
        combined = _apply_augmentations(slots, cross)
        combined = _render_choice_point_columns(combined, retrieved_graph_state)
        self._trace("slots_before_validation", [s.model_dump(mode="json") for s in combined])
        combined = _deduplicate_slots(combined)
        combined = self._retain_valid_slots(
            "cross_rejected_slot", combined, retrieved_graph_state, initial_sql
        )
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
                raise ValueError("slot audit must assess every conflict point exactly once")
            kept = expected - {item.slot_id for item in audit.decisions if not item.keep}
            self._trace("slot_relevance_audit", audit.model_dump(mode="json"))
            combined = tuple(item for item in combined if item.choice_point_id in kept)
        return ChoiceOrganizationResult(
            choice_points=combined,
            intra_node_assessments=assessments,
        )

    def _retain_valid_slots(
        self,
        stage: str,
        slots: tuple[ChoicePoint, ...],
        state: GraphState,
        initial_sql: str | None,
    ) -> tuple[ChoicePoint, ...]:
        retained: list[ChoicePoint] = []
        for slot in slots:
            try:
                _validate_slots((slot,), state, initial_sql)
            except ValueError as error:
                self._trace(
                    stage,
                    {"slot_id": slot.choice_point_id, "error": str(error)},
                )
                continue
            retained.append(slot)
        return tuple(retained)


def _intra_node_signal_ids(state: GraphState) -> tuple[str, ...]:
    conflict_ids = {item.node_id for item in state.node_conflicts}
    multi_target_ids = {
        node.id
        for node in state.graph.nodes
        if node.node_type == "concept"
        and node.id not in conflict_ids
        and len(_dict_list(node.properties.get("targets"))) >= 2
    }
    return tuple(sorted(conflict_ids | multi_target_ids))


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


def _reconcile_intra_slot_ids(output: IntraNodeOutput) -> IntraNodeOutput:
    """Keep usable Slots when an assessment retains a stale, un-emitted Slot ID."""
    emitted = {item.slot_id for item in output.slots}
    return output.model_copy(
        update={
            "assessments": tuple(
                item.model_copy(
                    update={"slot_ids": tuple(ref for ref in item.slot_ids if ref in emitted)}
                )
                for item in output.assessments
            )
        }
    )


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


def _draft_candidate_has_provenance(item: CandidateDraft) -> bool:
    return bool(item.graph_refs or (item.initial_sql_evidence or "").strip())


def _draft_slot_has_provenance(item: SlotDraft) -> bool:
    return all(_draft_candidate_has_provenance(candidate) for candidate in item.candidates)


def _materialize_slot(slot: SlotDraft) -> ChoicePoint:
    return ChoicePoint(
        choice_point_id=slot.slot_id,
        scope=slot.scope,
        decision_question=slot.decision_question,
        selection_mode="multiple",
        options=tuple(_materialize_candidate(item) for item in slot.candidates),
    )


def _render_intra_columns(output: IntraNodeOutput, state: GraphState) -> IntraNodeOutput:
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
                        update={"statement": _render_statement_columns(option.statement, payloads)}
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
        replacements[_physical_ref(table_name, column_name)] = f"{table_name}.{rendered_column}"

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
            if not _draft_candidate_has_provenance(draft):
                continue
            option = _materialize_candidate(draft)
            if draft.candidate_id in options:
                options[option.option_id] = _merge_options(options[option.option_id], option)
                continue
            options[option.option_id] = option
        by_id[augmentation.slot_id] = slot.model_copy(update={"options": tuple(options.values())})
    for draft in output.new_slots:
        if not _draft_slot_has_provenance(draft):
            continue
        slot = _materialize_slot(draft)
        if slot.choice_point_id in by_id:
            continue
        by_id[slot.choice_point_id] = slot
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
