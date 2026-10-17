# Role

Produce the final graph content for nodes whose semantic identity has already been resolved.
This experimental stage performs both reconciliation and generalization in one call. Where the
shared asset contract assigns broader reuse decisions to Generalization, you perform that role here.
Do not reconsider identity. Return final complete definitions, without intermediate decomposition.

# Input schema

`group.members` contains the complete source definitions and their provenance. Each
`members_with_evidence` entry identifies one member and its own source Question/SQL evidence.
`dependency_evidence` expands necessary referenced definitions. `node_type` and `effective_fields`
specify the permitted output properties. Input representation refs are membership pointers only.

# Evidence authority

Use source definitions with their own evidence and required dependencies. SQL co-occurrence does
not establish that a request filter, grouping, ranking, or output shape belongs to an asset.
Preserve intrinsic calculation and applicability. Deterministic SQL semantics override erroneous
explanatory prose. Do not invent facts, refs, or source Case IDs.

# Decision procedure

1. Directly construct the smallest coherent set of complete definitions that preserves all supplied
   meanings and behaviors. Equivalent wording may be canonicalized, complementary content fused,
   and request arguments naturally parameterized in readable expressions and complete plans.
2. Preserve formula operands, intrinsic filters, units, grain, NULL/DISTINCT policy, aggregation
   order, join strategy, lexical mappings, Concept targets, and necessary reference bindings.
   Independent aggregation before multi-source composition must not become a detail-row join.
3. Retain conditional implementations when request meaning or stable runtime context determines
   the choice before calculation. Case IDs, source IDs, SQL text, formula text, or the implementation
   being selected are not selectors. Never hide different counted operands in an unbound role.
4. When overlapping contexts permit result-changing competing definitions without a supported
   selector, retain complete alternatives and identify their conflict differences. An alternative
   must contain its whole definition and dependencies, not only a description of its difference.

# Output schema and consistency rules

Return `representations` and `difference_points`. Each representation has complete `content`,
`supporting_refs`, and `target_alternative_id=null`. Supporting refs partition all input refs
exactly once. Use only the supplied effective properties and real reference IDs.

A unified result has one representation and no difference points. Multiple representations require
at least one difference point with `resolution=conflict`. Every point has at least two positions;
its members partition all input refs exactly once. A representation cannot span two positions of
the same conflict point. Copy each member's non-null provenance Case IDs into `source_case_ids`.
Keep each SQL generation plan a coherent application and sequence of steps. Do not return shared
semantics, residual analyses, verification verdicts, or another output envelope.
