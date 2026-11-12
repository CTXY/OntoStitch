# Repair steps

1. Read fixed Diagnosis targets and Assessment feedback. Resolve the identified gap while preserving established correct behavior. Assessment-only repair may have no Diagnosis targets. Suggested SQL is evidence, not proof of a graph change; do not repeat Diagnosis.
2. Locate missing or defective knowledge and make the smallest coherent correction. You may revise Concept, Event, Metric, GenerationRule or edges; a business relation defect does not automatically require another Rule.
3. Preserve valid nonempty Concept matching groups and explicit physical links. Shared targets alone are not duplicate Concepts; check actual grounded_on endpoint uniqueness. For Metric, preserve real calculation, exact operands and supported grain/NULL behavior.
4. A confirmed SQL error may justify a corrective Rule alongside correct definitions. Consult the registry for action boundaries and operation_intents; existing or new labels may apply. Preserve condition/action/reason and use actual-error signals for SQL triggers.
5. Revert unsupported tentative edits. Do not invent events, roles, lifecycle, data rules or dependencies to make replay pass. A supported semantic correction may be useful when SQL is already correct.
6. Inspect only relevant nodes or database facts needed for material uncertainty. Reuse guides and existing Values; once the supported correction is known, edit nodes and explicit edges together and hand off. Do not re-extract the entire case or expand fixed targets.
