# Metric requirements for integration

A Metric defines one business quantity by its complete calculation. Its expression, description,
pattern and plans must describe the same quantity. Preserve formula operators, operand meanings,
population, units, grain, NULL/zero policy, intrinsic conditions and required calculation order.

Explicit expression operators, aggregation_policy and original plan calculation steps are behavioral
commitments. For example, SUM(Price/Amount) cannot become Price/Amount by declaring SUM external,
nor become AVG because the name says average. General request composition may be removed only when
its ownership is established and the source's necessary computation remains reconstructible.

An expression must contain its full formula; plan prose does not replace an operation. Bind formula
roles to supplied Column/Metric IDs and retain their needed reference closure. A raw Column reference
does not denote an unstated derived calculation. Expand dependencies before comparing behavior.
Plans are coherent source-to-result programs: preserve input grain, filters, aggregate-before-join
ordering and null handling when required. Parameterize request-only selection/grouping naturally;
do not union case-shaped plans or invent new paths. Complementary plans need justified selectors.
Use qualified table/column names in plan prose and preserve stable exact identity when applicable.
