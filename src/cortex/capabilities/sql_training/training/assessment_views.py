"""Lossless fact grouping and task-scoped views for Assessment inputs."""

import json


def compact_node(value):
    """Remove empty presentation fields without changing stored node content."""
    if isinstance(value, dict):
        return {k: compact_node(v) for k, v in value.items() if v is not None and v != [] and v != {} and v != ""}
    if isinstance(value, list):
        return [compact_node(v) for v in value]
    return value


def compact_edge(edge):
    return {
        "id": edge.id, "type": edge.edge_type,
        "from": edge.from_node_id, "to": edge.to_node_id,
        **({"properties": edge.properties} if edge.properties else {}),
    }


def group_context_facts(facts):
    groups = {}
    for fact in facts:
        content = {k: v for k, v in fact.items() if k != "attempt_id"}
        key = json.dumps(content, sort_keys=True)
        group = groups.setdefault(key, {"attempt_ids": [], **content})
        group["attempt_ids"].append(fact["attempt_id"])
    return list(groups.values())


def sql_attempts(validation):
    rows = []
    for attempt in validation.get("attempts", []):
        correct = attempt.get("correct")
        executed = True if correct is True or attempt.get("predicted_result") is not None else None
        row = {"attempt": attempt["attempt"], "sql": attempt.get("sql"), "execution_ok": executed, "result_equal": correct}
        if correct is not True:
            row["difference"] = {
                key: attempt[key] for key in ("reason", "gold_result", "predicted_result")
                if attempt.get(key) is not None
            }
        rows.append(row)
    return rows


def physical_properties(props, *, include_values):
    result = {k: getattr(props, k) for k in ("table_name", "column_name", "data_type", "nullable", "unit", "references") if hasattr(props, k)}
    metadata = {}
    for source, fields in getattr(props, "source_metadata", {}).items():
        wanted = {"business_name", "description", "data_format"}
        if include_values:
            wanted.add("value_description")
        metadata[source] = {key: value for key, value in fields.items() if key in wanted and value}
    if metadata:
        result["source_metadata"] = metadata
    return compact_node(result)
