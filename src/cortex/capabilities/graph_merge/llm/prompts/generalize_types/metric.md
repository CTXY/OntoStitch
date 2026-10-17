For Metric knowledge, preserve calculation behavior, unit, grain, filters, aggregation policy, and
calculation policy. In equivalent mode, canonicalize semantically equal expressions and generation
plans without widening scope. In compatible mode, generalize only when formula variants have
reusable evidence-supported conditions. Preserve the expression's bound operands and their
canonical references; `physical_dependencies` is a legacy field, not an output property. Every input SQL
generation plan must map to a canonical output plan; never collapse incompatible formulas or grain.

For conditionally compatible formulas, emit one canonical Metric and retain one complementary SQL
generation plan per distinct implementation. Write each plan's `application` as the exact observed
request-semantic scope supplied by the residual selector, or as a reusable generalization consistent
with all supplied selectors and evidence. A partial set of known scopes does not disprove a broader
boundary. Do not encode applicability by inspecting expression text, SQL text, plan IDs, case IDs,
or source IDs. Preserve the formula, filters, and null handling of every observed plan.

Before deciding that two formulas cannot be combined, expand supplied Metric dependencies and
substitute each representation's observed request scope. A conditional formula is lossless when
each observed case selects behavior equivalent to its source, including when a source inlines only
the branch active in that case. Keep the business-field condition in the canonical expression and
plan; do not specialize the whole Metric to one observed branch.
