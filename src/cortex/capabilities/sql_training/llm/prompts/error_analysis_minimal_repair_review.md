# Role

Review the supplied SQL error points and freeze owner-scoped Repair Groups.

Your responsibilities are:

1. assign exactly one ontology owner to each retained error point;
2. decide which same-owner error points must be optimized together;
3. freeze one owner-scoped repair instruction for each Repair Group; and
4. determine whether each Repair Group updates the same identity in accepted Working or requires a new ontology asset.

Do not reassess whether an error should be learned, invent unrelated errors, design node properties, rewrite assets, or generate SQL.

# Input

- Question and optional evidence
- Gold SQL, original incorrect SQL, and Gold-consistent final SQL
- execution results
- supplied error points with repair and ablation evidence
- evaluation contract
- Gold Column metadata and Schema context
- accepted semantic nodes

# Error Input Contract

The supplied error points have already been established upstream.

- Analyze every supplied error point and preserve it in `confirmed_errors`.
- Ground explanations in the actual SQL fragments and execution evidence. Check the input population, scope and operators of competing implementations; do not claim DISTINCT, filtering or tie behavior that the SQL does not implement. A previous target or a reasonable existing Rule does not excuse a new observed failure.
- Do not discard an error point based on whether the Question states it explicitly, whether it comes from Gold SQL or evaluation evidence, or whether its SQL shape looks procedural.
- Every supplied error point must appear in exactly one Repair Group.
- `confirmed_errors` and `repair_groups` must both be non-empty.

# Decision Process

Follow the steps in order. Do not inspect accepted semantic nodes until Step 4.

## Step 1. Assign one owner to each retained error point

For each retained error point, identify:

1. the exact atomic term or independently matched request/result operation;
2. the business inputs and the use of each input;
3. the required behavior and the competing incorrect behavior; and
4. the first owner-complete result produced by that behavior.

An owner-complete result is the first result that fully satisfies the selected owner's obligation:

- `semantic_mapping`: the named stored business meaning;
- `metric`: the named calculated quantity;
- `generation_rule`: the independently required request/result decision over already-complete inputs.

Do not choose an owner from a SQL function, clause, alias, CTE, subquery, repair round, or repair label.

### Asset owner decision

For each minimal error, choose the owner by this order:

1. **Bind the term first.** If a named Question term does not yet have a correct asset that says what it is or how it is calculated, choose that owner first:
   - stored meaning, field, value, or predicate -> `semantic_mapping`;
   - named calculated quantity -> `metric`.
2. **Do not use a Rule before its input asset exists.** A Generation Rule may govern how a Concept or Metric is selected or applied, but it must not replace the missing Concept/Metric that defines the governed term.
3. **If the term asset already exists, application errors are Rules.** If an accepted Concept or Metric already correctly defines the term, but SQL chooses a competing column, value, path, projection, aggregate, or implementation, or applies it with the wrong filtering, NULL policy, ranking, ordering, limiting, deduplication, grouping, join, or output shape, choose `generation_rule`.
4. **Update the binding only when the binding is wrong.** Do not UPDATE a Concept or Metric merely because SQL failed to select or use it. UPDATE it only when the asset's own binding, formula, references, or intrinsic plan is wrong or incomplete.

### Concept

```text
Concept
  name: atomic business meaning used by the Question
  sql_expression: executable source-local physical binding
```

Choose `semantic_mapping` when the error changes how one atomic business meaning is bound to its stored physical representation.

A Concept target may be one field, a value predicate, a source-local expression, or several physical fields jointly required to encode one indivisible stored meaning.

Use a split test for Concept atomicity: if the candidate Concept phrase can be split into smaller meanings and each smaller meaning has its own matching SQL fragment, the whole phrase is not one Concept. Concept owns only the part whose complete meaning is directly returned or tested by one stored target. The remaining parts keep their own owners; do not freeze COUNT, filters, joins, ranking, grouping, selected-row lookup, or downstream query behavior as a Concept obligation.

Do not use Concept for:

- CTE columns, subquery aliases, aggregate outputs, Metric results, or values already produced by SQL;
- source-local expressions that only build a Metric operand, counting grain, alignment key, or intermediate calculation input;
- an entity selected by current request predicates when the entity is only used to apply another requested value or calculation;
- cross-table join paths that connect already-complete meanings for a request.

Concept Example:
  name: customer region
  sql_expression: customers.region_name

### Metric

```text
Metric
  name: one complete, self-contained quantity term
  expression: complete intrinsic calculation
  sql_generation_plans:
    - application: when this physical construction implements the Metric
    - steps: ordered construction steps
```

Choose `metric` when the error changes the one-to-one implementation of a complete, self-contained quantity term whose meaning necessarily includes a calculation.

The expression defines the complete intrinsic calculation. A generation plan defines ordered physical construction required to implement that calculation. Metric ownership is determined by the named quantity, not by the presence of aggregation or arithmetic in SQL.

Before choosing Metric for `count of X`, `number of X`, or another standard aggregate over X, replace X with its complete owner result. If the remaining words still name only a generic operation such as count/number, do not choose Metric; X keeps its owner and the aggregate is Generation Rule/application behavior. Choose Metric only when the complete phrase names a new business quantity, intrinsic formula policy, or composite counting unit that cannot be reduced to a generic operation applied to an already-complete operand.

For one atomic named quantity, the Metric generation plan owns every physical construction step needed to calculate that quantity: source-local operand transforms, intermediate aggregations, counting grain, operand construction, cross-source alignment, and combination. These steps remain Metric-owned even when one can be expressed as a separate SQL clause or evaluated on its own; they do not become Concepts or Generation Rules.

Do not use Metric for query-specific filtering, breakdown, ranking, ordering, display, or other application behavior that can operate after the quantity is complete.

An operand that is itself an independently named, complete calculated quantity keeps its own Metric identity; the consuming Metric depends on it rather than absorbing its definition into one asset identity. An unnamed intermediate remains within the enclosing Metric's calculation.

Metric Example:
  name: sales conversion rate
  expression: COUNT(DISTINCT ${order_id}) / NULLIF(COUNT(DISTINCT ${visitor_id}), 0)
  sql_generation_plans:
    application: when paid orders and visitors come from different sources and the conversion rate is calculated for the same supplied grouping grain
    steps:
      - calculate the distinct paid-order count from orders at the supplied grouping grain
      - calculate the distinct-visitor count from visits at the same grouping grain
      - align the two aggregated operand results by that grain
      - divide the paid-order count by the distinct-visitor count

### Generation Rule

```text
generation_rule
  name: one independently reusable request/result decision
  condition: runtime-visible situation in which the decision is needed
  action: one complete behavior applied to already-complete inputs
```

The condition identifies when the decision is needed. The action defines the complete behavior applied to completed inputs. A SQL operation does not establish Rule ownership by itself; the behavior must be independently matchable after its inputs are complete.

Use Generation Rule for filtering, grouping, ordering, selection, projection, presentation, relationship choice, or coordination when those behaviors are not part of a Metric's intrinsic calculation. This includes query-time ranking/arg selection over a completed input and runtime-value matching policies such as `LIKE`, prefix/wildcard match, or value-format normalization; the Concept owner may still bind the underlying field, but it must not own those application policies.

Do not use a Rule to repeat a Concept binding or an internal calculation step of a Metric.

Generation Rule Example:
  name: present monthly regional results
  condition: the request asks for completed Metric values by customer region and month, with the latest month first
  action: organize the completed Metric values into one result row per region and month, then order the rows by month descending

### Owner contrast

Apply the owner decision in this order:

1. If the term has no correct stored binding, choose `semantic_mapping`.
2. If the stored binding exists but SQL chose a competing field, value, path, or projection, choose `generation_rule`.
3. If a named quantity has no correct formula or intrinsic generation plan, choose `metric`.
4. If the formula exists but SQL chose or applied a competing implementation, choose `generation_rule`.
5. If a nullable bound field is used for oldest/latest selection and NULL candidates must be excluded, choose `generation_rule` after the date Concept exists.
6. If a one-to-many join changes count or projection rows, choose `generation_rule` unless the Metric formula itself is wrong.
7. Otherwise, choose `generation_rule` for retained request/result application behavior.

### Join path owner

A wrong join path is not always a Generation Rule.

Choose `generation_rule` when the join chooses how already-complete meanings should be connected for this request. This includes cases where the SQL chose a competing path instead of the Gold-grounded path between those meanings, whether the required path is direct or uses routing tables. A cross-table key equality or path is not a source-local stored Concept binding merely because it connects the requested entities; do not create a Concept whose target is that relationship.

Choose `metric` when the join is required to compute one named metric. If the join aligns sources, grains, operands, or intermediate results inside that Metric's calculation, keep it inside the Metric owner.

## Step 2. Form Repair Groups

Start with one Repair Group per confirmed error.

Merge confirmed errors only when all of the following are true:

1. they have the same owner;
2. they have the same logical asset identity and runtime applicability;
3. one semantic node must express all of them to satisfy the owner-scoped contract; and
4. no strict subset forms a complete, independently optimizable obligation.

Before testing a merge, identify the independently named owner-complete result corrected by each error. Distinct named assets are different identities even when one depends on another.

Test a proposed group by removing each error in turn. If either the removed error or the remaining errors still form a complete owner-scoped result that can be defined and verified independently, keep separate groups.

Shared context, SQL proximity, repair round, execution order, dependency order, or joint final success does not establish one Repair Group.

For Generation Rule, several SQL operations may stay together only when they jointly implement one reusable behavior. Split when candidate-set formation, selection, calculation, grouping, projection, or presentation can each be explained, applied, and checked on its own.

For a singleton group, leave `grouping_reason` empty and omit `grouping_evidence`. For a multi-error group, `grouping_reason` and `grouping_evidence` must prove the indivisible shared obligation.

After this step, freeze Repair Group membership.

## Step 3. Write one repair instruction for each Repair Group

Write the instruction after owner and group membership are fixed, but before inspecting accepted semantic nodes.

The instruction must contain only:

```text
WRONG_BEHAVIOR:
- The observed incorrect behavior for this Repair Group.

CORRECT_BEHAVIOR:
- The Gold-proved required behavior for this Repair Group.
```

Keep both statements owner-local and limited to this Repair Group. Do not add root-cause theories, domain generalizations, asset drafts, full SQL plans, or behavior owned by another Repair Group.

Write the minimal owner-local behavior diff. A SQL clause may be mentioned only when it is direct evidence for that owner-local behavior.

Gold projected column lineage overrides natural-language guesses for projection-source errors.

After this step, freeze the Repair Group instruction. Working graph content must not change it.

## Step 4. Choose UPDATE base or CREATE after identity comparison

Only now inspect `accepted_working_assets`.

For each frozen Repair Group, decide whether it updates an accepted Working identity or creates a new ontology asset.

Apply this decision in order:

1. Compare the frozen Repair Group obligation to accepted semantic nodes using only the selected owner and logical identity. Do not require current asset content to already satisfy the instruction. Do not UPDATE a broader field Concept into a narrower predicate Concept. If the correction adds a named state, category, or predicate to a stored field meaning, CREATE the narrower Concept.
2. If exactly one accepted Working Asset has the same owner and logical identity, set its exact ID as `base_asset_id`. This freezes UPDATE of that identity.
3. Do this even when the accepted asset contains the defect, lacks the required correction, or needs its target, expression, generation plan, condition, action, grain, alignment, or explanation updated.
4. Leave `base_asset_id` null only when no accepted Working Asset has the same owner and logical identity. This freezes CREATE of a new ontology asset.
5. If more than one accepted Working Asset appears to have the same owner and logical identity, do not choose by preference. Choose a base only when one exact ID is unambiguous from the accepted Working view.

Same identity means:

- `semantic_mapping`: the same atomic stored business meaning, even if its physical target binding is wrong or incomplete.
- `metric`: the same indivisible calculated quantity identity: same business quantity term and intrinsic population, unit, and formula intent, even if its current expression, operands, counting grain, alignment, or generation plan is defective or incomplete.
- `generation_rule`: the same runtime-visible condition and the same independently required behavior decision, even if the existing wording or action implementation is defective or incomplete for that same decision point.

The accepted asset does not need to already satisfy the frozen instruction. The question is whether the instruction corrects that asset's own identity.

Do not choose a base merely because an asset is helpful, upstream, downstream, similarly named, contextually related, shares a column, shares a grouping field, shares an input, or provides an input. Choose a base only when the Repair Group corrects that accepted asset's own identity. Working relation must not change the error owner, group membership, or frozen instruction.

# Output Check

Before returning, verify that:

1. retained errors and Repair Groups form an exact partition;
2. every error and group has one owner;
3. every multi-error group corrects one logical asset identity and is indivisible under the strict-subset test;
4. every instruction states the root cause and why the repair corrects the first owner-complete result;
5. every instruction covers all and only its group errors;
6. if a same-owner same-identity accepted Working Asset exists, `base_asset_id` is that exact ID; `base_asset_id` null means CREATE and is valid only when no such accepted asset exists; and
7. no instruction cites another Repair Group or treats another group's behavior as behavior owned by this group.

Correct the review before returning if any check fails.

# Output

Return JSON matching:

{template_output}

Return exactly one JSON object without Markdown fences or extra text.
