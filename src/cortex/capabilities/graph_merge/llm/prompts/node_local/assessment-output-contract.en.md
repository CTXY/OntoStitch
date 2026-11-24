# `Review` Filling Contract

This section is a **narrow contract for the node stage**; the optional `relation_followups` is used to hand off relationship matters and is separate from node determination `issues`.

---

## 1. Outer Layer and Conclusion

Call `Review` directly, without wrapping; that reply must not mix read or other result calls. Fill only `verdict`, `issues`, and optional `relation_followups`; do not fill candidate hashes or internal ledgers.

| verdict | Conditions for use |
| --- | --- |
| `accept` | No structural diagnostic or content/provenance defect remains; issues=[], relationship handoff items may be attached |
| `revise` | A concrete defect requires node revision, an internal relation removal, or repartitioning wrongly grouped members into justified subgroups and singletons |
| `needs_context` | Specific prerequisites needed to judge node integration are missing; do not request graph expansion because the full neighborhood is omitted |

For non-accept, at least one issue; every revise issue must have a non-empty basis. For grouping errors, use `revise` and identify the original members whose grouping must change and the semantic distinction to preserve, not `revisit_identity`. When only nonblocking relationships await later coordination, do not demand another draft of the same node.

---

## 2. `issues` and References

Each item fills `refs` (at least one item), non-empty `problem`, `basis`, and non-empty `required_outcome`; the repair target focuses on meaning that must be preserved and does not require unique wording.

```json
{
  "refs": [{"kind":"node","id":"owner","view":"candidate","fields":["properties.targets"]}],
  "problem": "Which knowledge item the candidate lost",
  "basis": [{"kind":"node","id":"original member","view":"before","fields":["properties.targets"]}],
  "required_outcome": "The original matching method that must be preserved"
}
```

- kind is only node/edge/contract; view is only before/candidate; reference only displayed local IDs and real field paths. Reference merged-away members or removed edges with before; retained standalone nodes are valid candidate references.
- fields do not use envelope paths, sources/common_sources/reference_origin, or presentation-layer content.*.
- Branch issues reference owner with `fields=["alternatives"]`, and locate the real branch ID and business fields in the text; do not add ref.alternative_id or use `alternatives[0].content...`.

---

## 3. `relation_followups`

Each item contains only `edge_ids` (a non-empty, duplicate-free list of **original edge IDs in the fixed input**), non-empty `problem`, and `required_outcome`. Do not guess compiled IDs; independently record relationship branch ownership that cannot be automatically determined, real dependency changes, or well-founded corrections, and hand them to the later relationship stage; do not perform modifications and do not report ordinary mappings or duplicate edges.

Followups cannot replace blocking issues for node loss, distortion, or incorrect provenance attribution, nor do they exempt structural validation. When the structure is valid, node content is acceptable, and only relationship expression remains to be handled, accept with followups may be used; the final graph must still pass relationship assessment.
