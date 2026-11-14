"""A SQL-focused view of completed Online guidance, without changing source knowledge."""
import json


def render_sql_handoff(guidance: dict) -> str:
    """Keep resolved constraints; unselected rules are discovery evidence only.

    An equivalence finding needs no mandatory representative: either valid
    implementation may be used when that operation is required by the task.
    """
    decisions = [
        {key: item.get(key) for key in ('source', 'decision_question', 'should_be', 'must_not_be')}
        for item in guidance.get('interaction_guidance', ())
        if item['source'] != 'database_equivalent'
    ]
    definitions = [
        asset['summary'] for asset in guidance.get('ontology_assets', ())
        if asset.get('type') in {'concept', 'metric', 'event'} and asset.get('summary')
    ]
    return (
        '# Task constraints\n\n'
        'Apply each resolved choice only within its stated scope. Return exactly the requested '
        'answer fields in their requested order; entity bindings do not imply additional outputs.\n\n'
        + json.dumps(decisions, ensure_ascii=False, indent=2)
        + '\n\n# Business definitions (reference only)\n\n'
        + '\n\n'.join(definitions)
        + '\n\nEquivalent alternatives do not require adding an operation. Add joins, filters, '
        'aggregations and outputs only when the current task needs them. Unselected historical '
        'generation rules are not additional task requirements.\n'
    )
