# Return the assessment result directly
When the necessary material has been fully provided, return JSON directly:
```json
{"explanation":"Explain the judgment on both the reasonableness of the local graph and the retention of original knowledge", "feedback":[]}
```
Return an empty `feedback` only when both aspects pass; otherwise each item is:
```json
{"problem":"Specifically what is wrong", "required_outcome":"The result that must be achieved, with equivalent implementations allowed", "refs":[{"view":"candidate","kind":"edge","id":"actual edge ID","field":"evidence_ids"}]}
```
- Each feedback item must have at least one real public reference. `view` is `before / candidate`; `kind` is `node / edge / contract`, and `field` may be omitted. Referenced fields must exactly match the input; do not use private storage paths or array indices. For branch references use node `alternatives`, and state the branch ID in the text.
- Both endpoints of a missing-edge reference must be nodes that actually exist in the current view; when a node is missing, reference an existing anchor to explain the gap, and do not fabricate an ID for an object to be created.
- Deleted objects reference before, current objects reference candidate; all references concern the current ontology graph.
- If the judgment depends on a missing edge, use `{kind:"missing_relation",view:"candidate",from_node_id,edge_type,to_node_id}`; add a starting `alternative_id` when necessary. The Controller verifies the actual missing relation, and any returned counterexample must be incorporated into the judgment.
- Recheck each previous feedback item: do not report resolved items again, and explain specifically what still exists; do not repeat old judgments merely because the draft was not modified.
- The sources of replaced edges should be retained item by item on the real relationship successors; the same evidence appearing on other nodes does not prove that relationship sources have migrated. When withdrawing an edge, check whether before proves the original assertion is erroneous; do not discard evidenced knowledge merely because it is outdated.
- When authorized material is truly missing, read it first; if it still cannot be obtained, state the decisive gap in feedback. All feedback goes back to the same Writer; do not fill classification, routing, version, hash, or other adjudication fields.
- Each reply should do only batch reads or a final JSON; the final result must not mix tool calls, Markdown fences, or wrapper layers. Format feedback only corrects the result format and is not treated as a graph defect.
