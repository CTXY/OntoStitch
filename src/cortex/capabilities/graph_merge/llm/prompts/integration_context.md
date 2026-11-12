## Reading the context

- `nodes` and `relations` are immutable input. Candidate lists describe the current graph. `{"input_ref":"ID"}` shares the complete input object; optional `overrides` replaces named top-level fields in full, including explicit nulls or empty lists. All other fields remain identical. Resolve node references against `nodes`, edge references against `relations`. Tools return full objects when needed.
- These wrappers compress input only. Edit commands still contain complete nodes/edges, never `input_ref` wrappers.
- `evidence` contains preloaded original NLQ/SQL/evidence; `evidence_directory` also lists sources available on demand. Read missing evidence when material; do not fetch already supplied bodies again.
- `physical_facts` contains relevant catalog definitions. Schema declarations are not observed data distributions. Other endpoints and facts remain available through tools.
- Use batch IDs for related reads. Candidate lookup accepts multiple seeds and returns a page per seed. Follow `next_offset`; a page is not the complete graph.
- Source handles belong to `view="input"`. For `view="candidate"`, use canonical IDs from `identity_map` or newly created IDs; merged-away handles are absent from that view.

## Targeted verification

- Verification means comparing supplied facts, not making a tool call for each check. Use the complete current Rule objects in `candidate_nodes` for `rationale_review_ids`; do not reread them just to confirm their text.
- `active_contracts` gives actual uses. A `contract_ref` resolves to the complete definition in `local_contracts`; an inline `contract` is the candidate definition. Different input/candidate versions remain explicit. No business equivalence is inferred by this sharing.
- For a missing physical column, use `inspect_graph(mode="nodes", table="exact table name or ID", column_names=["exact column name"])`. An absent column is reported explicitly. No database scan is performed.
- Relation pages prioritize semantic-to-semantic business edges before physical grounding, preserving pagination order. A partial page is not the full neighborhood.
- For relationships, select known edge IDs or filter `edge_types` (exact predicate IDs). Use `joins_with` for joins. A table's unfiltered adjacency contains ownership edges and is not a field lookup. Expand only to answer a concrete unresolved question; do not treat a partial page as complete.

## Submission

Use read tools only for missing information needed for a decision. Finish by calling **{output_name}** once with the complete structured result. Do not first print a prose or JSON draft of that result. Keep explanations concise and evidence-specific. Tool parameter and validation errors require correction, not a repeated unchanged submission.
