"""Merge repeated candidate behavior before discovery augmentation or scoring."""

from cortex.application.online_context import CandidateEvidenceAssessment


def _merge_options(previous, option):
    refs = _union((previous.graph_refs, option.graph_refs))
    evidence = previous.initial_sql_evidence or option.initial_sql_evidence
    assessment = _merge_evidence_assessments(
        previous.evidence_assessment, option.evidence_assessment
    )
    return previous.model_copy(
        update={
            "graph_refs": refs,
            "initial_sql_evidence": evidence,
            "evidence_assessment": assessment,
            "candidate_sources": tuple(
                source
                for source, present in (
                    ("memory_graph", bool(refs)),
                    ("initial_sql", bool(evidence)),
                )
                if present
            ),
        }
    )


def _deduplicate_options(options):
    # Only merge identical displayed implementations. Never erase whitespace/case
    # inside a SQL literal to infer semantic equivalence.
    grouped = {}
    for option in options:
        key = option.statement.strip()
        if key not in grouped:
            grouped[key] = option
            continue
        grouped[key] = _merge_options(grouped[key], option)

    # The Organizer can paraphrase the same Initial-SQL implementation twice.
    # Within one Slot, identical non-empty SQL evidence and identical Graph
    # provenance denote one Candidate even when the displayed prose differs.
    # Graph-only Candidates are deliberately excluded: one Graph alternative
    # may legitimately support more than one SQL operation.
    evidence_grouped = {}
    result = []
    for option in grouped.values():
        if not option.initial_sql_evidence:
            result.append(option)
            continue
        evidence_key = (
            option.initial_sql_evidence,
            tuple(sorted(option.graph_refs)),
        )
        result_index = evidence_grouped.get(evidence_key)
        if result_index is None:
            evidence_grouped[evidence_key] = len(result)
            result.append(option)
            continue
        result[result_index] = _merge_options(result[result_index], option)
    return tuple(result)


def _deduplicate_slots(slots):
    result = []
    for slot in slots:
        options = _deduplicate_options(slot.options)
        if len(options) < 2:
            continue
        result.append(slot.model_copy(update={"options": options}))
    return tuple(result)


def _merge_evidence_assessments(
    first: CandidateEvidenceAssessment | None,
    second: CandidateEvidenceAssessment | None,
) -> CandidateEvidenceAssessment | None:
    if first is None:
        return second
    if second is None:
        return first
    explanations = tuple(dict.fromkeys((first.explanation.strip(), second.explanation.strip())))
    return CandidateEvidenceAssessment(
        source_types=_union((first.source_types, second.source_types)),
        applicability=(
            "applicable"
            if "applicable" in {first.applicability, second.applicability}
            else "uncertain"
        ),
        explanation=" ".join(item for item in explanations if item),
    )


def _union(values):
    return tuple(dict.fromkeys(item for group in values for item in group))
