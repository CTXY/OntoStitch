# Guidance for This Type Group: Event

## 1. Integration goal

Integrate the definition, roles, and scope of the same business occurrence **type**, preserving "what happened, who participated, and how time/place and affected objects are related". Shared date columns, participants, or JOIN paths do not prove the same type; retain actually different types such as "application" and "approval" separately in this local task.

## 2. Field-filling rules

- **`properties.definition`**: required non-empty text describing the business occurrence, scope, and known grain.
- **`properties.targets`**: optional two-dimensional physical ID list; each inner list is a complete match, non-empty, and within the group there are no duplicates. Different matching methods are not combined into a union group; the outer "same group + same expression" must not repeat.
- **`properties.target_expressions`**: optional object; keys are canonical, in-bounds target index strings (`"0"`, `"1"`, etc.), and values are non-empty complete condition/join expressions. Renumber synchronously after groups are reordered; when there are no targets or targets=[], leave no expression entries.
- Events can connect to already-bound Concepts through existing business relationships; do not fabricate targets to cover fields, and do not treat a non-null date as a complete criterion for event occurrence.

---

## 3. Merging, generalization, and conflicts

- Complementary role descriptions and compatible matching methods for the same event type can be integrated; preserve pairings among roles, not just isolated participant fields.
- Participants or time windows in requests can be retained as filter parameters; fixed participation meanings or business scopes in the event definition cannot be generalized away.
- When the same type has verified incompatible definitions or role associations within overlapping scopes, retain complete branches and do not combine participants and occurrence conditions across branches. When necessary preconditions are insufficient, supplement sources; do not guess from neighboring fields.

### Complete content example

Assume the sources confirm the same order-submission occurrence, one describes the customer subject and the other describes the order object, and the original relationship confirms that the two are paired; this can be written as the following `NodeContent`:

```json
{
  "name": "Order submission",
  "description": "The business occurrence type in which a customer submits an order to the system, with order as the grain.",
  "aliases": ["Place order submission"],
  "tags": ["Order process"],
  "properties": {
    "definition": "A customer submits an order; the customer is the subject, the order is the affected object, and both belong to the same submission occurrence."
  }
}
```

The original role relationships in this example continue to be inherited; if the actual input has valid targets, they must be retained as-is, and must not be deleted because this example omits optional fields.
