from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class GenerationRuleReferenceImplementation:
    name: str
    sql_patterns: tuple[str, ...]
    result_semantics: str


@dataclass(frozen=True)
class GenerationRuleTaxonomyItem:
    intent_class: str
    description: str
    reference_implementations: tuple[GenerationRuleReferenceImplementation, ...]


DEFAULT_GENERATION_RULE_TAXONOMY: tuple[GenerationRuleTaxonomyItem, ...] = (
    GenerationRuleTaxonomyItem(
        intent_class="joinability",
        description=(
            "Choose the table path or key relationship that connects already-complete meanings. "
            "The path may be direct or may use one routing table."
        ),
        reference_implementations=(
            GenerationRuleReferenceImplementation(
                "direct_join_relationship",
                ("<left> JOIN <right> ON <key_equality>",),
                "Connect the requested entities through the direct relationship key.",
            ),
            GenerationRuleReferenceImplementation(
                "join_through_routing_table",
                (
                    ("<left> JOIN <routing> ON <first_key_equality> "
                     "JOIN <right> ON <second_key_equality>"),
                ),
                "Connect the requested entities by the relationship recorded in the third table.",
            ),
            GenerationRuleReferenceImplementation(
                "direct_join_with_different_keys",
                ("<left> JOIN <right> ON <different_relationship_key>",),
                "May connect a different population or relationship even for the same endpoints.",
            ),
        ),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="population_average_comparison",
        description="Compare an already-complete stored meaning or Metric with its average over the same requested population; keep averaging and comparison in the Rule, not a helper Metric.",
        reference_implementations=(
            GenerationRuleReferenceImplementation(
                "population_average_threshold",
                ("<value> > (SELECT AVG(<value>) FROM <source> WHERE <population_filter>)",),
                "Compute the average over the requested population before testing each row against it; equivalent CTE or window implementations are allowed.",
            ),
        ),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="count_aggregation",
        description="Return the number of requested entities or values.",
        reference_implementations=(
            GenerationRuleReferenceImplementation(
                "count_rows",
                ("COUNT(*)",),
                "Count input rows, including rows whose individual fields are NULL.",
            ),
            GenerationRuleReferenceImplementation(
                "count_non_null_values",
                ("COUNT(<value>)",),
                "Count non-NULL values of one expression.",
            ),
            GenerationRuleReferenceImplementation(
                "count_distinct_entities",
                ("COUNT(DISTINCT <entity_id>)",),
                "Count unique entities instead of repeated or joined rows.",
            ),
        ),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="existence_check",
        description="Return whether at least one qualifying result exists.",
        reference_implementations=(
            GenerationRuleReferenceImplementation(
                "boolean_existence",
                ("EXISTS(<query>)", "COUNT(*) > 0"),
                "Return a boolean existence result.",
            ),
            GenerationRuleReferenceImplementation(
                "encoded_existence",
                ("CASE WHEN EXISTS(<query>) THEN <yes> ELSE <no> END",),
                "Encode existence with the requested result labels.",
            ),
        ),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="boolean_output_encoding",
        description="Return the requested yes/no answer as output labels.",
        reference_implementations=(
            GenerationRuleReferenceImplementation(
                "yes_no_case",
                ("CASE WHEN <condition> THEN 'YES' ELSE 'NO' END",),
                "Return one label that encodes whether the condition is true.",
            ),
            GenerationRuleReferenceImplementation(
                "yes_no_iif",
                ("IIF(<condition>, 'YES', 'NO')",),
                "Return one label that encodes whether the condition is true.",
            ),
        ),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="result_deduplication",
        description=(
            "Return each requested output item or output tuple once when the SQL source can "
            "produce duplicate rows."
        ),
        reference_implementations=(
            GenerationRuleReferenceImplementation(
                "distinct_projection",
                ("SELECT DISTINCT <projection>",),
                "Collapse duplicate output rows after producing the requested projection.",
            ),
            GenerationRuleReferenceImplementation(
                "deduplicate_combined_rows",
                ("SELECT DISTINCT <projection> FROM <joined_or_combined_source>",),
                "Collapse duplicate output tuples from a joined or combined source.",
            ),
        ),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="extreme_value_selection",
        description="Return one scalar minimum or maximum value, not its carrying row.",
        reference_implementations=(
            GenerationRuleReferenceImplementation(
                "scalar_maximum", ("MAX(<value>)",), "Return one maximum scalar value."
            ),
            GenerationRuleReferenceImplementation(
                "scalar_minimum", ("MIN(<value>)",), "Return one minimum scalar value."
            ),
        ),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="top_k_selection",
        description=(
            "Return entity rows associated with the leading or trailing K values. K may be one. "
            "This fixes ranking and row inclusion, not which physical field representations are output. "
            "Passing requested attributes through is allowed; an additional fixed field-set policy is a separate decision."
            " For competition ranking, rank is one plus the number of eligible rows strictly ahead: "
            "counting strictly better rows can preserve ties. Check identical eligibility, grouping and NULL ordering "
            "before treating an alternative to a window function as incorrect."
        ),
        reference_implementations=(
            GenerationRuleReferenceImplementation(
                "tie_aware_group_rank",
                ("SELECT <output> FROM (SELECT <columns>, RANK() OVER (PARTITION BY <group> ORDER BY <score> DESC) AS r FROM <eligible_rows>) ranked WHERE r <= <k>",),
                "Competition rank retains all rows sharing a qualifying rank; preserve the eligible population and NULL ordering.",
            ),
            GenerationRuleReferenceImplementation(
                "strict_superior_count",
                ("(SELECT COUNT(*) FROM <eligible_rows> other WHERE <same_group> AND <strictly_better_in_requested_order>) < <k>",),
                "Competition rank equals one plus the number of eligible rows strictly ahead, so this can implement rank <= K and retain ties. Eligibility, grouping and NULL ordering must match; ordinary SQL comparisons with NULL do not automatically implement the requested ordering.",
            ),
            GenerationRuleReferenceImplementation(
                "ordered_row_limit",
                (
                    "ORDER BY <score> DESC LIMIT <k>",
                    "ORDER BY <score> ASC LIMIT <k>",
                ),
                "Return at most K ordered rows; boundary ties may be dropped.",
            ),
            GenerationRuleReferenceImplementation(
                "extreme_value_predicate",
                (
                    "<score> = (SELECT MAX(<score>) FROM <source>)",
                    "<score> = (SELECT MIN(<score>) FROM <source>)",
                ),
                "Return every entity row tied at the scalar extreme.",
            ),
        ),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="rank_output",
        description="Return an ordinal or tie-aware rank value for each requested result row.",
        reference_implementations=(
            GenerationRuleReferenceImplementation(
                "unique_row_number",
                ("ROW_NUMBER() OVER (ORDER BY <score>)",),
                "Give every row a different sequential position.",
            ),
            GenerationRuleReferenceImplementation(
                "rank_with_gaps",
                ("RANK() OVER (ORDER BY <score>)",),
                "Give tied rows the same rank and leave gaps afterward.",
            ),
            GenerationRuleReferenceImplementation(
                "dense_rank",
                ("DENSE_RANK() OVER (ORDER BY <score>)",),
                "Give tied rows the same rank without later gaps.",
            ),
        ),
    ),
)


# Additional decision intents share the same admission standard in Direct and Repair.
ADDITIONAL_GENERATION_RULE_TAXONOMY: tuple[GenerationRuleTaxonomyItem, ...] = (
    GenerationRuleTaxonomyItem(
        intent_class="column_binding_choice",
        description="Choose which supported fields, joint field groups or representations participate in the answer when evidence establishes a consequential choice beyond static Concept matching. This selects content, not its column order, formatting or row/column output shape. Choosing a field set does not also decide how the already-selected values are arranged; that is an independent decision. Do not repeat an already resolved term-to-field/value mapping.",
        reference_implementations=(GenerationRuleReferenceImplementation(
            "semantic_field_selection", ("SELECT <requested_meaning_column>",),
            "Preserve the requested meaning rather than substituting a similarly named identifier, amount, status or time field.",
        ),),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="aggregation_grain",
        description="Preserve the requested counting or aggregation unit across repeated records and joins; distinguish row, object and event grain.",
        reference_implementations=(GenerationRuleReferenceImplementation(
            "aggregate_before_expansion", ("WITH <per_unit> AS (<aggregate_at_requested_grain>) SELECT <result> FROM <per_unit>",),
            "Aggregate each requested unit before a later expansion can multiply its measure; equivalent implementations are valid.",
        ),),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="null_semantics",
        description="Preserve an evidenced NULL meaning in filtering, aggregation or arithmetic rather than treating missing values as a documented value without support.",
        reference_implementations=(GenerationRuleReferenceImplementation(
            "explicit_missingness", ("<value> IS NULL", "COUNT(<value>)"),
            "Choose the operation matching the requested missingness semantics; the examples are alternatives, not a combined recipe.",
        ),),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="temporal_alignment",
        description="Choose the supported occurrence time, interval boundaries and temporal alignment for the requested business event or duration.",
        reference_implementations=(GenerationRuleReferenceImplementation(
            "event_time_selection", ("<occurrence_time> >= <start> AND <occurrence_time> < <end>",),
            "Use the relevant event time and evidenced boundary convention; do not infer a universal date policy from one request.",
        ),),
    ),
    GenerationRuleTaxonomyItem(
        intent_class="population_scope",
        description="Preserve the requested population and intrinsic metric scope when evidence distinguishes the intended group or side of a calculation.",
        reference_implementations=(GenerationRuleReferenceImplementation(
            "scoped_population_filter", ("SELECT <calculation> FROM <source> WHERE <supported_population>",),
            "Apply each condition to its intended population without broadening or narrowing an independently defined metric.",
        ),),
    ),
)

DEFAULT_GENERATION_RULE_TAXONOMY += ADDITIONAL_GENERATION_RULE_TAXONOMY


def operation_registry(
    items: Iterable[GenerationRuleTaxonomyItem] = DEFAULT_GENERATION_RULE_TAXONOMY,
) -> dict[str, str]:
    """Compact shared labels; extensions live in this registry, not model-made schemas."""
    items = tuple(items)
    result = {item.intent_class: item.description for item in items}
    if len(result) != len(items) or any(not key.strip() or not value.strip() for key, value in result.items()):
        raise ValueError("operation registry requires unique nonempty intents and descriptions")
    return result



def render_generation_rule_taxonomy(
    items: Iterable[GenerationRuleTaxonomyItem] = DEFAULT_GENERATION_RULE_TAXONOMY,
) -> str:
    lines = [
        "# Operation Registry",
        "",
        (
            "The Registry labels reusable request operations and observed-error corrections with "
            "result-changing SQL implementations. Each reference implementation is a complete "
            "alternative, not a behavior fragment to combine with another implementation."
        ),
    ]
    for item in items:
        lines.extend(["", f"## `{item.intent_class}`", item.description])
        for implementation in item.reference_implementations:
            patterns = "; ".join(f"`{pattern}`" for pattern in implementation.sql_patterns)
            lines.append(
                f"- `{implementation.name}`: {patterns}. {implementation.result_semantics}"
            )
    return "\n".join(lines)
