# Generation Rule analysis

A Generation Rule defines one reusable generation decision: a condition and its required observable
action.

Compare in this order:

1. `operation_intent`: confirm the common operation family.
2. `condition` and question triggers: identify the runtime situation that invokes the decision.
3. `action` and SQL triggers: identify the complete observable behavior.
4. `references`: distinguish request-supplied values from operands that define the observable
   action. A concrete binding is not case-specific merely because different cases use different
   Columns.

Classify the relationship explicitly:

- same representation: wording or equivalent SQL differs only;
- concrete–concrete: different cases fill a role already selected by request-visible or stable
  runtime input and therefore reduce to one template;
- general–concrete: the general rule completely covers the concrete behavior;
- conditional implementations: a valid runtime input selects one action;
- conflict: overlapping runtime contexts and triggers can activate different actions whose first
  observable results differ, with no supplied pre-action selector.

When generalizing, source-specific references replaced by proven request inputs must not remain as
unconditional dependencies. A differing implementation detail cannot be made variable merely by
naming it as a role: prove that an input visible before the action actually selects it in every
supporting source context. Complementary operations that form a larger workflow are different
identities and should not be combined here.
