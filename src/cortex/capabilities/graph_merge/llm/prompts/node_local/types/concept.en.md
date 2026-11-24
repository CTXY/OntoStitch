# Guidance for This Type Group: Concept

## 1. Integration goal

Integrate the definition and complete matching method of the same business object, category, attribute, or term. Preserve representational roles: for example, a school can be represented by code or name, but "school entity" is not automatically equal to "school name attribute", and code/name do not automatically become interchangeable counting bases.

## 2. Field-filling rules

| Field | Filling requirement |
| --- | --- |
| `properties.definition` | Optional; when necessary, express the unified definition, roles, and distinguishing conditions, without negating still-valid source meanings. |
| `properties.targets` | **Non-empty two-dimensional physical ID list**; each inner list is one complete joint matching method, and different outer groups are different methods, not a default `SELECT` column list. |
| `properties.target_expressions` | Optional object; keys are index strings of the current outer group, such as `"0"`, `"1"`, and values are the necessary complete condition/join expressions for that group. |
| `properties.mapping_type` | Optional string; normalize when compatible; retain substantive differences in the definition or branches, and do not change it into an array. |

### Match-group validation

- Use real physical IDs allowed in this round; groups are non-empty with no duplicates, and natural-language column names are not used in place of IDs.
- Retain compatible complete groups; do not splice independent methods into one joint group. Duplicate detection compares both endpoints and expressions; the same endpoints with different conditions are not duplicates.
- After reordering or deduplication, synchronously rebuild the expression index; keys must be canonical and in bounds, and values must be non-empty text. Expressions retain conditions, use catalog SQL names or declared aliases, do not use graph IDs as SQL identifiers, and do not reference physical columns outside the group.
- `targets` does not automatically create `grounded_on`. Resolving direct-binding edge issues must not accidentally delete valid matching methods.

---

## 3. Merging, generalization, and conflicts

- Synonymous definitions or compatible supplements can be rewritten into a complete definition; compatible code/name representations are retained as independent match groups, with roles explained.
- Fixed business definitions are not deleted as incidental parameters; for example, you cannot generalize "completed orders" and lose the completed status.
- There is currently no generic request-parameter selector. Mutually exclusive matching schemes cannot be flattened into a union with a phrase like "choose by context"; when existing fields cannot express it, retain the verified complete competing definitions or report the missing preconditions.
- Incompatible definitions or matching explanations retain complete branches under the common contract, each keeping its match groups and expressions.

### Complete content example

Assume the sources confirm that school code and school name represent the same school entity; this can be written as the following `NodeContent`; actual IDs are based on this round's input:

```json
{
  "name": "School",
  "description": "School entity; code is used for identification and name is used for display; do not assume name uniqueness based on this.",
  "aliases": ["School"],
  "tags": [],
  "properties": {
    "targets": [["column.schools.code"], ["column.schools.name"]]
  }
}
```

The two groups cannot be changed into one joint match group. If the group order is adjusted later, the original groups' `target_expressions` must be renumbered accordingly.

## 4. Business meaning across relations

Keep supported meaning about several relationships in the owning Concept definition. Each relation description retains its own role, condition and limitations. Repeated source assertions are consolidated into one relation with combined evidence by the controller; they do not create additional business structure.
