"""Lossless prompt sharing and bounded, relevant evidence preloading."""

import json

from cortex.schema import GraphEdge, GraphNode


def prompt_json(value):
    """Compact prompt JSON; whitespace adds no evidence."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def bounded_items(items, *, budget=12000):
    """Keep whole objects; callers retain tool access to everything omitted."""
    kept, used = [], 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False))
        if used + size <= budget:
            kept.append(item)
            used += size
    return kept


def pack_payload(payload):
    """Share input bodies, with whole-field replacements for changed objects."""
    result = dict(payload)
    for source, fields, model in (
        ("nodes", ("candidate_nodes", "deleted_nodes"), GraphNode),
        ("relations", ("candidate_relations", "deleted_relations"), GraphEdge),
    ):
        index = {
            row["id"]: model.model_validate(row) for row in payload.get(source, [])
        }
        for field in fields:
            if field not in payload:
                continue
            result[field] = []
            for row in payload[field]:
                original = index.get(row["id"])
                packed = row
                if original is not None and not (
                    field == "candidate_nodes"
                    and row["id"] in payload.get("rationale_review_ids", ())
                ):
                    before = original.model_dump(mode="json")
                    after = model.model_validate(row).model_dump(mode="json")
                    overrides = {
                        key: value
                        for key, value in after.items()
                        if before[key] != value
                    }
                    shared = {"input_ref": row["id"]}
                    if overrides:
                        shared["overrides"] = overrides
                    if len(prompt_json(shared)) < len(prompt_json(row)):
                        packed = shared
                result[field].append(packed)
    contracts = {c["id"]: c for c in payload.get("local_contracts", [])}
    if "active_contracts" in payload:
        result["active_contracts"] = [
            {"contract_ref": row["contract"]["id"], "used_by": row["used_by"]}
            if contracts.get(row["contract"]["id"]) == row["contract"]
            else row
            for row in payload["active_contracts"]
        ]
    return result


def expand_payload(payload):
    """Resolve shared bodies for offline audits; no model interpretation needed."""
    result = dict(payload)
    for source, fields, model in (
        ("nodes", ("candidate_nodes", "deleted_nodes"), GraphNode),
        ("relations", ("candidate_relations", "deleted_relations"), GraphEdge),
    ):
        index = {row["id"]: row for row in payload.get(source, [])}
        for field in fields:
            if field in payload:
                result[field] = [
                    {
                        **model.model_validate(index[row["input_ref"]]).model_dump(
                            mode="json"
                        ),
                        **row.get("overrides", {}),
                    }
                    if "input_ref" in row
                    else row
                    for row in payload[field]
                ]
    contracts = {c["id"]: c for c in payload.get("local_contracts", [])}
    if "active_contracts" in payload:
        result["active_contracts"] = [
            {"contract": contracts[row["contract_ref"]], "used_by": row["used_by"]}
            if "contract_ref" in row
            else row
            for row in payload["active_contracts"]
        ]
    return result
