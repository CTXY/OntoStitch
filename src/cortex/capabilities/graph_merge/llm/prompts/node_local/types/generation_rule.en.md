# Guidance for This Type Group: GenerationRule

## 1. Integration Goal

Integrate the same reusable SQL generation decision, comparing its result obligations, applicable conditions, and complete behavior. Different SQL formulations may be equivalent; operations that co-occur in the same source do not necessarily belong to the same decision. Names, retrieval signals, and intent tags all cannot prove equivalence.

## 2. Field Filling Specification

| Field | Filling requirement |
| --- | --- |
| `properties.condition` | **Non-empty string**; business/request conditions identifiable before the decision; do not use case IDs or circular expressions as conditions. |
| `properties.action` | **Non-empty string**; complete implementable behavior, preserving operands, computation order, and boundaries already present in the source; when parameterizing, explain the binding and provide a SQL pattern when necessary. |
| `properties.reason` | **Non-empty string**; preserve well-founded decision reasons and result differences; the field name is not `rationale`. |
| `properties.operation_intents` | Optional string list, merging compatible tags; the field name is plural, and tags cannot replace action. |
| `properties.triggers` | **Required object**; retrieval signal format is below; it is not an execution condition. |

`triggers` permits only the following structure; the three lists may each be empty, but not all empty; list elements are non-empty strings:

```json
{
  "question": {"phrases": ["phrase with basis in the original question"]},
  "sql": {"columns": [], "operations": []}
}
```

`triggers.sql.columns` uses real column IDs from this round's directory. Merge existing signals from sources and deduplicate; do not add fields such as `keywords` and do not fabricate signals. Hitting a trigger does not mean all conditional branches should be executed at the same time.

---

## 3. Merging, Generalization, and Conflict

- Compatible descriptions, necessary operand information, and equivalent SQL patterns for the same decision may be integrated. Do not, for the sake of brevity, push calculations or result obligations owned by the source onto an undefined "upstream".
- Request parameters (such as K, time window, or threshold) may be generalized only when the source supports their role and value range; explain how to bind them in condition/action, and do not add a `parameters` field. Natural-language K is not the same as a Metric's `${key}` node reference.
- `COUNT(*)` and `COUNT(DISTINCT patient_id)` must compare counting objects and uniqueness; `RANK`, `DENSE_RANK`, and `LIMIT` must compare tie-breaking strategies. Merge only when equivalence is proven; preserve complete competing branches when behavior within the same scope is proven different; supplement evidence when unknown.
- Conditional integration must completely preserve behavior under each condition, for example whether unmatched objects are preserved determines the JOIN strategy; do not generalize "aggregate first, then JOIN" to any order.
- Each conflict branch retains its own condition, action, reason, triggers, and intent; do not mix branches.

### Complete Content Example

Assume two sources respectively request the top 3 and top 5 customers, and it is confirmed that K is a request parameter and that granularity and tie-breaking strategy are the same; the following `NodeContent` may be written:

```json
{
  "name": "Select top K customers",
  "description": "Select the requested number of customers in descending order of customer sales; when sales are tied, order by customer ID ascending.",
  "aliases": [],
  "tags": ["customer ranking"],
  "properties": {
    "condition": "The request selects the top K customers by the established per-customer sales amount, where K is a positive integer given by the request; the sources use the same tie-breaking strategy.",
    "action": "On the per-customer sales result with one row per customer, sort by sales descending and customer ID ascending, then take the requested K rows; when generating SQL, replace K with the request value.",
    "reason": "Maintain the result obligation of selecting customers; substituting K=3 or K=5 restores the original behavior.",
    "operation_intents": ["top_k_selection"],
    "triggers": {"question": {"phrases": ["top"]}, "sql": {"columns": [], "operations": []}}
  }
}
```

This strategy must come from the actual input and is not used as the default strategy for all ranking rules.
