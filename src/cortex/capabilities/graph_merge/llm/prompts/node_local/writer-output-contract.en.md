# `submit_patch` Filling Contract

This contract permits multiple local subgroup integrations, unchanged singletons, and explicit removal of original internal relations. All results belong to one draft and one atomic Assessment; other relationship work remains a followup.

---

## 1. Invocation and Submission Modes

- **Direct submission**: Parameters are flat, without `patch` / `arguments` / `result` wrappers. The result call must be the only call in that reply; reads may be initiated in another reply as a group.
- **Successful result**: `explanation` is required; `failure` is omitted or JSON `null`, never the string `"null"`. Fill the complete current `integrate_groups`, `keep_separate_ids`, `relation_removals`, and any necessary `relation_followups`.
- **Complete partition**: Every original member appears exactly once, either in one integrate_groups item or in keep_separate_ids. Subgroups are disjoint, contain at least two original members, and contain their owner. No outside IDs, omissions or overlaps are allowed. keep_separate_ids contains only unchanged singleton members, not an unmerged subgroup.
- **Any number of justified results**: integrate_groups may contain zero, one or several items, up to the number allowed by the supplied members. All-singleton output is valid only when keep_separate_ids lists every original member. Empty integrate_groups plus empty keep_separate_ids is not a successful result.
- **Cannot complete**: Fill only a specific failure and explanation when an authorized decisive prerequisite cannot be obtained. A grouping mistake should be corrected in the partition, not reported as a schema failure.
- **Required explanations**: The result, every integration, and every source route require their own non-empty explanation. One explanation does not replace the others.
- **Field boundaries**: Do not submit generic graph edits, `sources`, `common_sources`, `overrides`, or internal version ledgers. Node references in `properties` may use only original group members or provided `endpoint_directory` IDs; do not guess global IDs.

---

## 2. Required `integrate_groups` Content

| Field | Filling requirement |
| --- | --- |
| `owner_id` | For each subgroup, use the original preferred owner if that subgroup contains it; otherwise use that subgroup’s first member in the supplied original member order |
| `member_ids` | At least 2 distinct original members, including this subgroup’s owner; jointly with all other subgroups and singleton IDs, cover the original member set exactly once |
| `content` or `alternatives` | Choose one: a complete normal result, or at least two complete branches |
| `conflict_points` | Non-empty for conflict results; omitted or empty for normal content. Never combine normal `content` with unresolved conflict points |
| `source_routes` | Each fixed manifest unit belonging to this subgroup’s members exactly once; omit other subgroups’ and singleton units. Copy IDs unchanged: the controller must not re-key them when a result owner changes |
| `common_evidence_ids` | Optional; only known evidence that the original knowledge is preserved in every complete branch |
| `explanation` | Non-empty, briefly describing the integration decision |

### Complete Content

`content` and each branch's `content` must contain **name, description, aliases, tags, properties**: name non-empty; description a string or null; aliases/tags string lists; properties satisfy this round's type schema.

**content is a complete replacement, not a field increment.** It does not include id, node_type, or evidence_ids; knowledge must not be inherited by omitting fields, and only differences or overrides must not be submitted.

### Branches and Difference Points

Each alternative contains `alternative_id`, complete `content`, and optional `evidence_ids`. Branch IDs are non-empty and unique; all evidence IDs must be known.

Each branch must satisfy at least one support qualification: its own evidence_ids are non-empty; source routes point to the branch; a common route exists. **Filling only common_evidence_ids does not satisfy the qualification check**; being qualified still requires semantic support for the complete branch.

`conflict_points` uses the following structure; point_id is unique, each text is non-empty, each point has at least two choices, references actual output branches, and distinguishes at least two branches:

```json
[
  {
    "point_id": "p1",
    "aspect": "Contested specification",
    "description": "Specific difference that cannot be unified under the same scope",
    "choices": [
      {"alternative_ids": ["A"], "statement": "Position A"},
      {"alternative_ids": ["B"], "statement": "Position B"}
    ]
  }
]
```

---

## 3. `source_routes`

Each entry fills `source_unit_id`, `destination`, and non-empty `explanation`. Copy the original manifest IDs for integrated members exactly; node or evidence IDs cannot be used instead. Separate members retain their original sources automatically and do not need routes in the integration.

| destination | Legal use |
| --- | --- |
| `{"kind":"result"}` | Original knowledge is preserved in the normal result; cannot be used for conflict results |
| `{"kind":"alternatives","alternative_ids":["A"]}` | Original knowledge is preserved in the specified complete output branch; the list is non-empty and references only actual branches |
| `{"kind":"common"}` | Original unit knowledge is preserved in every complete output branch; cannot be used for the normal result |
| `{"kind":"discard"}` | Only for old alternatives with specific correction or complete redundancy basis; normal_content/common units must not be discarded |

Only kind=alternatives carries alternative_ids. When routing an old branch to a preserved result, use result/alternatives/common; do not discard its knowledge or provenance merely because wording differs. When text is duplicated but still carries provenance or relationships that must be preserved, route it to a preserved result rather than discard; do not use this to discard well-founded relationships.

---

## 4. Internal Relation Removal

`relation_removals` contains only `edge_id` and non-empty `reason`:

```json
[{"edge_id":"E1","reason":"Why this original internal assertion is erroneous or redundant, and where any supported knowledge remains."}]
```

- The edge must be displayed in the original `relations`, with both original endpoints in this group. No removal permission is granted for other edges or graph objects.
- A merge-induced self-relation is not sufficient reason to remove a valid assertion. Either justify removal using the original meanings and evidence, or partition the wrongly grouped members into justified subgroups and singletons.
- Preserve supported relation meaning and its actual provenance disposition. Evidence occurring elsewhere does not prove preservation. Do not convert a removed relation's evidence into unsupported blanket branch support.
- `reason` explains this edit; durable node definitions contain business knowledge, not repair logs. Do not also list a removed edge as a followup.

## 5. `relation_followups`

Fill only for other relationship issues requiring later coordination; usually omitted:

```json
[
  {
    "edge_ids": ["E1"],
    "problem": "After the current node integration, which semantics of this relationship still need handling",
    "required_outcome": "The meaning-preservation goal that the later relationship stage must complete"
  }
]
```

edge_ids precisely references **original edge IDs in the fixed input**, is non-empty and duplicate-free, and does not guess compiled IDs; both text fields are non-empty. It records handoff items, does not perform edge modifications, and does not indicate that the relationship has been fixed. Do not report ordinary ID redirects, future endpoint merges, or strict duplicate edges.

---

## 6. Revisions

Each submission completely replaces the draft relative to the fixed original group, including every subgroup, the current singleton list and relation removals. Omitted previous operations are withdrawn; rejected candidates never become a baseline. Address current Assessment feedback in this local draft, preserve unresolved followups, and do not mistake historical diagnostics for current facts.
