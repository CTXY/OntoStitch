# Incremental two-stage ontology integration

## Purpose and scope

Merge case graphs without confusing identity, implementation differences and graph organization. Preserve compatible knowledge and complete genuine conflict alternatives. A shared physical endpoint is a retrieval signal, not an identity decision. No database, case identifier, column name or topic-specific rule participates in production semantic decisions.

This implementation retains three roles: Identity, Node Consolidation with independent Node Assessment, and Graph Reconciliation. Reconciliation evaluates and repairs graph organization itself; a separate repeated whole-graph semantic Assessment is not added.

## State and boundaries

- **Immutable inputs:** original existing/incoming nodes, relations, branches and exact SourceReferences. Source objects are never deleted.
- **Current workspace:** stable internal IDs and the last accepted graph. Initially it contains physical-aligned inputs with separate business nodes. Imported unresolved grounding duplication or abstract relations can remain until addressed; hard shape, references and branch integrity must hold after a local acceptance. The workspace is not a published valid ontology.
- **Active batch:** a current-relative patch, pending invalid objects, confirmed aliases and its last valid draft. Failure does not partially apply edits to current.
- **Lineage:** true single-target identity aliases are separate from many-to-many rewrite groups. An origin group is possible provenance, not evidence that every source supports every output.

SQLite atomically stores current, lineage, revision, history and open tasks. No SQLite transaction stays open during a model call. Stale revisions cannot overwrite a newer snapshot. Explicit experimental resume validates an input/schema/evidence fingerprint and restores accepted work, not an invalid pending patch. Completed consolidation batches are skipped. Reconciliation currently starts a fresh graph check after resume; its repeated calls must be included in recovery costs rather than reported as a fresh merge's cost.

Only final publication assigns public IDs and invokes the strict GraphManager transaction. Local acceptance does not call the final exporter.

Feedback on a completed workspace can start a new local transaction with explicit current `target_ids` and `issues`. The same input/schema/evidence fingerprint is required. It skips Identity and completed consolidation, reviews the named patch and reopens only its outside impacts. An unfinished workspace cannot use this local-only shortcut. The feedback is semantic input to the Agent, never a code-level instruction to delete or relink a specific kind of object.

## Workflow

1. **Prepare.** Namespace inputs, align exact physical identities, retrieve source evidence. Preserve all business definitions separately.
2. **Identity.** The Agent compares referents using definitions, bindings, relation roles and QA. Output disjoint identity groups and meaningful distinctions. Code validates handles/types and selects canonical IDs. Different descriptions, implementations or policies of the same identity go to content integration; different identities stay separate.
3. **Consolidate.** Code packs complete matched groups by content size. In each atomic batch the Agent reconciles definitions, targets, implementations and complete conflict alternatives. Alias projection is mechanical; the Agent must also repair necessary consumers. Independent Node Assessment checks actual accepted-candidate content, evidence, minimality and relevant dependencies. Rejection repairs the same pending batch, not the whole accumulated proposal.
4. **Reconcile.** The first pass covers every new/unchecked business node, including unmatched input. Signals recall parallel assertions, triangles, same-role neighbours, competing decisions, source projection differences and value concepts. They are questions, never automatic ontology edits. The Agent checks ownership, hierarchy, relation meaning, branches, redundant assertions and related decisions, and submits a local repair. Content/core-dependency changes trigger targeted independent Node Assessment; structure itself is evaluated by Reconciliation.
5. **Repeat actual impacts.** Reopen changed definitions and both endpoints of added/removed/changed relations, affected contracts and declared consumers. A new neighbour can invalidate a prior check even when the previously read object was unchanged. A no-change review closes that task. Budget exhaustion fails explicitly; it does not turn uncertainty into acceptance.
6. **Publish once.** Require strict final graph validation, concrete authored predicates, complete source dispositions and exact source preservation. Compose original-to-final lineage, then export public IDs. Audit the public result independently, including default physical edge correspondence and resolved conflict branches.

A concrete late identity counterexample fails the unpublished batch and requires rebuilding it from immutable sources; merely reversing aliases cannot undo fused semantics safely.

Before each matched batch, code projects only still-live members. An absent member must already have a recorded rewrite into the confirmed canonical owner; otherwise the controller fails before calling the model. The full original group is still supplied for content comparison and independent assessment. This retains earlier coupled repairs without inferring identity from provenance or restoring retired copies. Controller-owned projection and lineage assumptions are preflight-validated, not repeatedly presented to the Agent as repairable tool errors.

## Local graph selection

Each task has **editable targets**, full relevant objects, and **read-only context**. Code never uses accumulated read history as scope or write permission.

The initial packet contains:

- complete target/group definitions and relevant original versions;
- adjacent assertions, endpoint definitions and all compatible physical claimants;
- declared consumers, complete relation-metadata groups and full branch contents;
- other Rules using the same operands, including different intents;
- complete relevant target groups and physical ownership definitions;
- relevant QA, active predicate contracts and node guidance;
- only operation intents actually relevant to current targets, with dynamic guidance available for further definitions.

These are explicit retrieval rules, not recursive graph expansion. Independent context neighbours do not automatically become editable. `extend_task(ids, reason)` names a necessary coupled change and expands authorization; bounded extension prevents an unrelated whole-graph rewrite. Complete conflict/identity/rewrite groups are never cut to satisfy a size limit. Large adjacent catalogues remain available through paginated read tools.

The first version is serial. It does not add distributed transactions, a task taxonomy or another planning Agent.

## Agent interfaces

| Role | Inputs | Tools | Output | Code responsibilities |
|---|---|---|---|---|
| Identity | Incoming seeds, retrieved candidates, roles and relevant evidence | Inspect nodes/relations/candidates/contracts; evidence; guidance | `IdentityDecision(groups, distinctions)` | Validate complete disjoint groups, source handles and semantic types; select canonical IDs |
| Consolidation | Complete matched groups, current local graph, originals and QA | Scoped reads; necessary scope extension | `BatchResult` | Save pending objects by stable kind/ID; validate nested fields, references and sources; compact complete alternatives; compute actual delta |
| Node Assessment | Actual candidate, targeted content/dependencies, source evidence and relevant consumers | Read-only inspection/evidence/guidance | `NodeReview(verdict, issues, issue_refs, rationale_checks)` | Enforce requested Rule-reason coverage; accept/revise/rebuild control |
| Reconciliation | Current local graph, originals, named signals, consumers and QA | Scoped reads; scope extension | `BatchResult` | Validate edits and active inspections, reopen affected tasks, commit accepted workspace batches |

`BatchResult` contains only required local edits, nontrivial `changes`, optional complete `conflicts`, semantic `edge_merges`, new relation contracts, and current inspection/rationale conclusions. It does not repeat current, origins or the cumulative graph. Valid submission finishes the task directly: there is no extra StageCompletion call.

The valid candidate already includes saved valid writes. `saved_patch` is a small operation/ID directory plus explicit dispositions. `inspect_pending` retrieves invalid saved objects or dispositions that cannot be read from the candidate. Rejected installed-predicate redefinitions are never saved, so they cannot poison subsequent local corrections. New conflict owners and replacement edges may be created and referenced atomically in the same batch; validity does not depend on the order of edits in the submitted list.

Ordinary same-ID updates receive automatic self-dispositions. Deletions require explicit merge/rewrite/discard. A complete node/edge write uses one operation-specific shape; it must not repeat node_id/edge_id alongside the object. New relation contracts cannot replace installed meanings.

`edits` executes modifications; `changes` only records correspondence. A replacement that omits a current input from its outputs cannot leave that input wholly unchanged in the candidate. Submission reports all such objects and requires explicit edits or a truthful retained-output correspondence; it never silently deletes them. Remapping an already absent archived source remains valid without a new deletion. Related-decision signal identities include group membership so a changed group is not mistaken for an already reviewed pattern.

Code supplies a fresh `new_id_prefix` for each active batch. The Agent appends local node/edge names instead of guessing the next numeric input ID, which may belong to an unread physical object. The prefix stays stable across that batch's assessment revisions; existing and pending objects retain their IDs.

`edge_merges` means the Agent has confirmed the same assertion in compatible scope. Code unions exact source references into the named survivor; it never infers that confirmation. Historical-source correction and current-object rewrite remain separate correspondence groups. This avoids mixing immutable and current references.

## Validation and assessment economy

- Independent action review is separate from Rule-reason support. Node Assessment returns one `decision_checks` record per requested Rule, listing independently variable decisions and concrete issues. Acceptance requires one decision and no unresolved issues; code checks declared coverage, not business meanings. Alternative implementations of one decision are not counted as independent decisions.
- Negative Rule-reason findings quote clauses from the actual current reason or resolved current alternative. Submission verifies that the quotes occur there, preventing archived same-ID text from being assessed as current. Older local assessment findings are supplied separately from the latest unresolved feedback to expose contradictory repair requests. Source preservation does not require restoring unsupported historical observations. Compact common-field placeholders are storage details; resolved alternatives carry the semantic requirements.
- Shared-operand Rules are grouped into one related-decision signal per operand, regardless of intent label. `decision_neighborhoods` indexes current Rule IDs, complete branch IDs and applicability scopes without repeating their definitions. It exposes ordinary policies that could bypass a saved alternative, as well as selection/presentation consumers with different tags.
- Reconciliation covers affected knowledge once even without structural signals. Ordinary complete no-change conclusions do not automatically trigger independent Assessment. Explicit unresolved joint-decision inspections still receive targeted review even without edits; an empty patch cannot close rejected feedback. Merely retaining a Rule in the local context no longer forces repeated assessment. See the reconciliation protocol below.

- Malformed internal fields fail at submission, before final publication. Errors identify the object/path. Valid pending siblings remain saved for repair.
- Tool views are explicit `input` or `candidate`; modes are enumerated. Missing IDs, revision and pagination completeness are returned. No fallback silently substitutes a source definition for a current one.
- A valid complete plain-JSON result goes through the same model/schema/graph validation as tool output and can finish without another model call. Malformed JSON is rejected, not heuristically repaired. Every malformed tool-call ID receives a tool response before the next API request.
- Node Assessment has compact findings; three mandatory global essays are removed. Rule reasons are checked against actual evidence for the requested/changed Rules, including alternatives. Correct action does not establish a correct reason.
- Semantic deletions receive independent assessment using their before-content and source evidence. Named joint-decision inspections receive independent assessment even when Reconciliation proposes no edits; a no-change compatibility claim can itself be wrong. Assessment receives those inspections, prior findings and the candidate's staged lineage.
- A source-only delta is not automatically safe. New or removed supporting evidence reopens relevant content review. Reuse is limited to evidence actually reviewed for that target, using resolved QA content identity; QA/qa provenance strings are still preserved exactly. Loading evidence or inspecting neighbours is not equivalent to reviewing them.
- Inspection defaults use explicit task roots, not accumulated read history. Writers have four exploratory read rounds per episode and Node Assessment three; after that only submission and necessary scope extension remain available. Essential missing information must produce a concrete failure. These bounds limit exploration, not the required completeness of conflict or identity groups. A separate merge-wide call budget prevents unlimited repair loops.
- Trace records provider calls, responses, errors, tool arguments/results, task inputs and workspace revisions. Report all experimental attempts separately; offline tests cannot prove model-quality equivalence.

## Administrator walkthrough (evaluation only)

Cases 35/36/63/87 are experimental inputs, not code branches.

1. Identity may recognize school administrator and administrator full name as the same referent when name columns identify the role in the case. Name and email remain different semantics.
2. Consolidation preserves all justified name representations. The conflicting first-slot versus all-slots selection is represented by complete sourced alternatives rather than two ordinary conflicting rules.
3. Reconciliation places administrator email on the administrator role and charter number on the charter-school concept. It checks business ownership, not storage adjacency.
4. Selection and layout remain independent decisions. A layout Rule must not silently fix a slot count inconsistent with the selected policy; same-operand Rules are available even when their intent tags differ.
5. A source edge removed in a previous rewrite may actually survive as an `applies_to` edge. Repair names that surviving edge and preserves the precise original reference, instead of pointing only to its neighbouring conflict node.

Correctness checks cover exact source dispositions and resolved alternative behavior. Semantic evaluation additionally compares ownership, branch attribution and compatible consumers across input orders; byte-for-byte node names/IDs are not an order-invariance criterion.

## Implementation map

- `integration/incremental.py`: controller, local tasks, assessment triggering and single final publication.
- `integration/batch.py`: pending/current-relative patch and local validation.
- `integration/task_context.py`: task-local packets and actual impact analysis.
- `integration/lineage.py`: original-to-final identity and structural dispositions.
- `integration/workspace.py`: atomic serial workspace snapshots.
- `llm/incremental.py` and `llm/prompts/incremental_*.md`: bounded tool loop and centralized Markdown role prompts.
- `scripts/run_incremental_merge_review.py`: official-endpoint batch/order experiments.
- `scripts/audit_incremental_merge.py`: independent public-result preservation audit.

Experimental results, including unsuccessful attempts and provider-reported token usage, belong in the run report rather than this design contract.

See [real-model validation](ontology-merge-incremental-validation.md) for the controlled comparison, independently reviewed four-case results, failed attempts and total development cost.
