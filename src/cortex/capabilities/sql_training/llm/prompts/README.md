# Extraction prompts

One Learning Agent owns the case's tentative Working graph. Startup instructions stay short; type and relation guidance load on demand through the existing inspection tools.

## Instruction ownership

| File | Purpose |
| --- | --- |
| asset_contract.py | Shared semantic definitions and atomicity rules rendered into authoring and Assessment |
| learning/system.md | Case purpose, graph model, stage slot and delivery |
| learning/direct.md | Initial extraction steps |
| learning/repair.md | Error-driven and Assessment-only correction steps |
| learning/types/concept.md | Minimal meanings, nonempty matching groups and explicit binding examples |
| learning/types/event.md | Evidence threshold, participant/context structure and specific role-edge examples |
| learning/types/metric.md | Actual computation, Concept/Metric operands, expression.refs and explicit dependencies |
| learning/types/generation_rule.md | Atomic behavior, registry reference, multiple/new intents and explicit scope |
| learning/relations.md | Fixed meanings, business predicates, descriptions and edge lifecycle |
| learning/values.md | Verified physical Value proposal and reuse |
| learning/assessment.md | Independent quality, application, target resolution and regression review |

Use the same definitions in authoring and Assessment. Exact field schemas come from runtime models; do not duplicate full schemas in prose. Each guide includes a complete node example and separate explicit edge operations. Example operands/endpoints must already exist or be created in the same edit; examples do not establish case evidence.

## Node and relation contract

Concept requires nonempty targets. Targets are nested matching groups: the IDs inside one group jointly implement a match. Optional target_expressions keyed by group index preserve complete predicates/correlations. Different Concepts may share targets; uniqueness concerns direct grounded_on edges to a physical endpoint, not target occurrence counts. One Concept may connect to multiple endpoints.

Metric must define a genuine business calculation. Named stored inputs are Concepts. expression.refs remains an operand binding map and can refer to Concepts, Metrics or supported physical inputs. Generic references fields are not used to stand in for relationships. Formula, names, units, grain, NULL behavior and supported plans must agree.

Event requires supported occurrence structure: what happens, actual participating objects/roles and related time or place. It is not justified by a verb, timestamp or JOIN alone. No Entity/Process/Constraint type or qualifier field is introduced. Evidence-supported business edges can organize the Event without an invented boolean event predicate.

GenerationRule defines one atomic reusable generation behavior. The operation registry helps reason about behavior boundaries as well as choose tags. operation_intents accepts multiple fitting labels and meaningful new labels; new-label meaning belongs in existing description/reason text. Tags do not replace condition/action or prove quality.

All relations are explicit edge_operations with nonempty properties.description. Describe actual roles, supported conditions, missing-value limits and prohibited implications when relevant. Reuse meaningful predicates; different participant roles need distinguishable semantics, not two unexplained generic participant links. A new predicate's general meaning differs from a particular edge's description.

Optional deterministic missing-edge completion may use retained formula/catalog bindings after Agent authoring. It is not the principal graph builder and cannot guess hierarchy, event roles or expand every overlapping target into a direct edge. Reading saved graphs must not infer business edges from refs. System provenance and conflict metadata remain code-owned.

## Case context and evidence

Question, Gold and evidence have different roles: business meaning, implementation, and supporting interpretation. Relevant physical context provides exact IDs, SQL names and metadata; on-demand graph/database reads resolve material uncertainty. The semantic index is bounded and is not proof that unlisted knowledge is absent.

Database exploration is read-only and must not update metadata. Do not require a new exploration-evidence subsystem, per-edge probe attachment or assessor re-exploration. Use existing case, metadata, graph and replay evidence; uncertain strong claims remain inconclusive instead of becoming accepted facts through confident wording.

No domain-specific state/attribute/category decision tree is imposed. Keep school-specific examples as illustrations. Preserve source disagreements and distinguish observed data patterns from universal business claims. No type quota, node-count target or necessary-knowledge coverage score is used; observed errors guide missing-knowledge repair.

## Delivery and Assessment

Save complete nodes and explicit edges through edit_graph, then handoff. Nodes, Values and edges participate in version fingerprints, diffs, Assessment, acceptance, rollback and recovery. Interdependent edits are atomic. Direct newly added Question triggers must be contiguous supported phrases; Direct SQL-side triggers stay empty. Repair may use observed-error signals.

Assessment reports unit quality, application across the supplied attempts, fixed-target resolution and regressions separately. Correct replay does not validate invented business structure. A semantic relation may be useful without adding SQL syntax, while executable definitions still need application evidence. The controller retains the SQL-plus-Assessment acceptance gate.

Progressive guide schemas omit annotation text but retain actual validation keywords, property names and literal enum/const values. Load guidance for the next coherent edit, write, then inspect other needed types. Prompt/contract changes invalidate incompatible checkpoints; restart cached prompt loaders after changes.

## Maintenance checks

- Update shared definitions, the relevant type guide and Assessment together when semantics change.
- Check complete JSON examples against runtime models and edge operations against the graph manager.
- Keep every edge description specific and consistent with predicate direction and endpoint meanings.
- Check Concept minimality, actual Metric calculation, Rule atomicity, Event evidence and whole-graph role/hierarchy consistency.
- Preserve exact counted operands, DISTINCT, NULL behavior and source alignment; current result equality is not semantic proof.
- Do not introduce legacy unbound Concepts, refs-generated business relations, qualifier fields or stage-specific intent gates.

The shared operation registry is identical in Direct and Repair. Require an evidenced consequential implementation decision, a minimal implementable action and fitting intents; justify extensions. Linked business names are allowed. Runtime parameters remain reusable.
