# Task

Independently assess the identity decision and actual candidate graph against immutable input and source evidence. A proposal's explanation is a claim to verify. Use IntegrationReview: accept, revise, or revisit_identity, with concise decision_check, quality_check, preservation_check and actionable issues. The actual candidate is provided; edit commands are omitted because they repeat its content.

Code supplies actual_changes independently of the author's changes. Review every created/updated object, deleted before-image and changed relation contract. All edge writes are included; only unchanged adjacency is paginated. Pure additions need review even without a rewrite explanation.

## 1. Are the decisions correct?

Check business identity separately from description/implementation/policy compatibility. Shared fields and neighbors alone do not prove identity. Different content of the same business identity must not be separated merely to avoid conflict. Distinct independent obligations must not be forced into one Rule.

Check actual differences: synonymous/compatible information is integrated; conditional behavior has relevant identifiable conditions; genuinely incompatible requirements retain coherent alternatives; corrections follow source evidence. Different outputs allowed by the task are not necessarily conflicts. Do not require identical incidental SQL syntax.

Check related Rules jointly, including ones Identity kept distinct: name the shared decision, determine whether their conditions overlap, and compare the required behavior in that overlap. Inspect obligations inside compound actions, not only titles or intent tags. Selection of answer content is not presentation of fixed content. Each Rule matching its own Gold does not prove the Rules can coexist. Check a generalized binding/selection policy against relevant source cases even when another case expresses its choice only through Concept bindings and Gold, with no parallel Rule. Missing a separate Rule is not permission to erase that source choice. Additional Concept targets do not resolve a selection policy. Reject ordinary conflicting Rules unless supported conditions separate them or explicit alternatives preserve the competition. Use decision_check for this conclusion; cite both IDs and the conflicting obligations in issues. Do not invent a request, role or policy unsupported by the source cases to manufacture or hide a conflict.

## 2. Is the candidate knowledge valid?

For each ID in `rationale_review_ids`, return one `rationale_checks` entry: whether its reason is supported and one sentence comparing the claimed justification with the actual evidence. Inspect common and alternative reasons. Do not substitute action correctness or source preservation for this comparison. If any reason relies on an unsupported premise, return revise and identify the unsupported premise in issues. Code checks coverage, not the truth of your conclusion.

Report three short findings in `quality_check` without repeating those entries:

1. **Node meaning and justification.** Review retained incoming nodes and affected existing nodes. Check NL minimality, defining Metric computation, atomic Rule action and supported Event structure. Verify reasons/definitions independently of executable behavior: a Gold SQL implementation supports that task choice, not an empirical claim that other columns differ. A possible difference cannot establish actual non-equivalence. Read type guidance when needed.
2. **Individual relations.** Check endpoint roles, business meaning and supported limits against the normalized Concept referents. Names/codes may represent objects; titles, definitions and incident roles must agree. Attribute relations require the direct business bearer or evidenced applicable subtype; storage in its table and object adjacency do not prove ownership. has_attribute is abstract: actual edges need concrete business predicates. Recheck retained incident edges after identity merges, not only edited edges. Do not require a tree or infer exclusive ownership. Descriptions explain the business relation, not just storage. Do not invent exclusions, cardinality or observed database facts.
3. **Active predicate vocabulary.** `active_contracts` joins each used contract to its actual edge IDs. Compare predicates with potentially equivalent roles, including unchanged contracts. Different source prefixes are not distinct semantics; same names or endpoint types are not proof of equivalence. If definitions, direction and supported limits agree, reuse one predicate across its uses. Preserve predicates with different meanings or scopes. Unused imported contracts alone are not a defect.

Extraction status, valid IDs and preserved source mappings do not establish these semantic checks. Check retained text as well as edits; a correct action does not validate an unsupported reason. Do not reject wording merely because it includes “may” or “could”: identify the unsupported claim it is used to justify.

Rule generalization may remove incidental conditions when no provided training case contradicts it. Do not reject or narrow such a rule solely for a hypothetical counterexample; cite actual training evidence for a claimed counterexample. This does not permit inventing observed database facts or ignoring explicit source limits.

## 3. Does the actual rewrite preserve supported meaning?

Compare source nodes/edges, changes, candidate and affected consumers. No supported source meaning may disappear through aliasing, content replacement, edge deduplication or restructuring. Do not demand all imaginable missing knowledge. Corrections need not preserve source errors.

An old edge expressed by a path needs the whole path with valid contracts/scope. A split Rule needs all its independent obligations and explicitly repaired consumers. Nonempty after is not proof of coverage. A path witness does not mean each new edge is supported by every old source. Exclusive alternatives cannot jointly witness one asserted fact. Preserve relation_metadata semantics; do not mechanically expand member lists.

Compare the supplied source NLQ/SQL/evidence with actual candidate definitions and relations; use tools for missing material facts, not to reconfirm already supplied objects. Code checks structural integrity, not business truth. Do not invent observed NULL/uniqueness facts. Inspect duplicate assertions across predicates: keeping generic and specific edges does not preserve extra knowledge if one is fully implied by the other under their contracts and scope. Preserve any distinct supported limits when consolidating.

## Revision focus

When `previous_review` and `changes_since_review` are supplied, first verify the prior issues, then inspect new changes and their consequences. The full current review scope remains available: unchanged retained knowledge is not automatically trusted, and all cumulative writes/deletions remain visible. Expand through tools when a change affects other meanings or consumers.

## Feedback

Return revisit_identity only for a concrete identity error; revise for content, structure, unsupported claims or missing evidence reads, including errors inherited unchanged. Specify the lost/incorrect meaning and needed correction in `issues`; put the involved node, edge and contract IDs in `issue_refs` (kind/id). These select the next revision context; include the objects needed to interpret the issue. Use supplied IDs, never labels or guessed IDs. Leave both empty on accept. Accept only when all three checks pass; no uncertain/unknown success. Keep review focused on this transaction's input knowledge and impacts, not unrelated global cleanup. Read conflict-writing guidance only when actual competing alternatives need inspection.


Keep each summary check to the substantive findings and relevant IDs. Do not inventory unchanged objects or repeat `rationale_checks` in decision_check, quality_check and preservation_check. Keep each rationale explanation to one evidence comparison; issues state the correction needed.
