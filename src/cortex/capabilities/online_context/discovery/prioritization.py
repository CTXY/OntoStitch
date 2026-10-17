"""Deterministic source-first clarification ranking, independent of discovery scope."""

from itertools import combinations

from cortex.application.online_context import ChoicePoint, SourceCategory
from cortex.capabilities.online_context.discovery.candidates import _deduplicate_slots

_WEIGHTS: dict[SourceCategory, int] = {"H": 4, "M": 2, "L": 1}


def rank_choice_points(points: tuple[ChoicePoint, ...]) -> tuple[ChoicePoint, ...]:
    """Score distinct implementations; Python's stable sort preserves discovery ties."""
    ranked = []
    for point in _deduplicate_slots(points):
        if any(option.evidence_assessment is None for option in point.options):
            raise ValueError("every clarification candidate requires an evidence assessment")
        source_sets = [set(option.evidence_assessment.source_types) for option in point.options]
        pairs = {
            frozenset((left_source, right_source))
            for left, right in combinations(source_sets, 2)
            for left_source in left
            for right_source in right
        }
        category: SourceCategory
        if (
            frozenset(("ontology_asset",)) in pairs
            or frozenset(("ontology_asset", "initial_sql")) in pairs
        ):
            category = "H"
        elif (
            frozenset(("ontology_asset", "database_schema")) in pairs
            or frozenset(("initial_sql", "database_schema")) in pairs
        ):
            category = "M"
        elif frozenset(("database_schema",)) in pairs:
            category = "L"
        else:
            raise ValueError("candidates do not form a supported evidence-source contrast")
        count = sum(
            option.evidence_assessment.applicability == "applicable" for option in point.options
        )
        relevance = sum(option.evidence_assessment.relevance_score for option in point.options)
        score = _WEIGHTS[category] * (1 + relevance / (relevance + 1))
        ranked.append(
            point.model_copy(
                update={
                    "source_category": category,
                    "applicable_candidate_count": count,
                    "candidate_relevance_sum": relevance,
                    "priority_score": score,
                    "priority_reason": f"{category}; candidate relevance sum R={relevance:g}",
                    "evidence_priority": None,
                    "evidence_priority_reason": "",
                }
            )
        )
    return tuple(sorted(ranked, key=lambda item: -item.priority_score))


def select_questions(
    points: tuple[ChoicePoint, ...],
    budget: int | None,
) -> tuple[ChoicePoint, ...]:
    """The caller supplies ranked points. One displayed question costs one unit."""
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, int) or budget < 0
    ):
        raise ValueError("question budget must be a nonnegative integer")
    return points if budget is None else points[:budget]
