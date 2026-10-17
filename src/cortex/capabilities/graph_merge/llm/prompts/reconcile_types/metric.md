# Metric analysis

A Metric defines a business quantity and the complete value-producing procedure needed to compute it.

Compare in this order:

1. Business quantity in `name`, `description`, `metric_kind`, `unit`, and `pattern`.
2. `expression`: operands, filters, arithmetic, aggregation, distinctness, null/zero behavior, and
   output value.
3. Each `sql_generation_plan`, step by step:
   - source tables and required filters;
   - input columns and semantic roles;
   - pre-aggregation and aggregation grain;
   - join keys and join order;
   - numerator and denominator grain;
   - final calculation and output grain.
4. `aggregation_policy`, `calculation_policy`, and `references` where they affect the value.

Equivalent SQL syntax and physical bindings may be generalized. First separate the Metric's
intrinsic calculation from the current Question's filters and requested breakdown. A filter or
grouping field seen in one case is not intrinsic merely because its SQL plan mentions it.

Different plans are conditional only when an independently meaningful request or Schema context
selects the correct plan. Requested breakdown can select the calculation grain when the Metric plan
requires its independently aggregated components to use the same requested groups. It does not by
itself select a grouped versus scalar internal denominator; those remain conflicting unless the
Question, Schema, or stable business policy states which denominator behavior applies.

Compare a Metric dependency by its complete supplied definition, not only by the caller's local
placeholder or plan text. A caller that references a Metric inherits that dependency's formula and
conditions. A locally inlined equivalent calculation and a dependency reference may therefore be
compatible.

When one formula has a branch selected by a request-visible business field, test each supplied case
under its observed field value. A case that does not activate the branch is not evidence against the
conditional formula.
