"""Mechanical field diffs; never infer business intent from prose."""
import hashlib
import json


def changed_fields(before, after):
    changes = []
    for kind in ("nodes", "edges"):
        old = {x.id: x.model_dump(mode="json") for x in getattr(before.graph, kind)}
        new = {x.id: x.model_dump(mode="json") for x in getattr(after.graph, kind)}
        for key in sorted(old.keys() | new.keys()):
            if key not in old or key not in new:
                changes.append({"id": key, "operation": "created" if key in new else "deleted"})
                continue
            for field in old[key].keys() | new[key].keys():
                a, b = old[key].get(field), new[key].get(field)
                if a == b:
                    continue
                if field == "properties":
                    for prop in sorted(a.keys() | b.keys()):
                        if a.get(prop) != b.get(prop):
                            changes.append({"id": key, "field": f"properties.{prop}", "before": a.get(prop), "after": b.get(prop)})
                elif field not in {"sources", "reference_origin"}:
                    changes.append({"id": key, "field": field, "before": a, "after": b})
    return changes


def review_key(payload):
    # Feedback is prior judgment, not new graph/evidence. All evaluated content,
    # before/after changes, scope and contracts remain part of this key.
    value = {k: v for k, v in payload.items() if k != "feedback"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def current_repair_fields(candidate, findings):
    """Put the current value beside old feedback, not another copy of old assertions."""
    from cortex.schema.graph import alternative_content
    objects = {(kind, item.id): item for kind, items in (("node", candidate.graph.nodes), ("edge", candidate.graph.edges)) for item in items}
    result, seen = [], set()
    def read(value, path):
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                return {"not_present": True}
            value = value[key]
        return value
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        for ref in finding.get("issue_refs", []):
            obj = objects.get((ref["kind"], ref["id"]))
            for field in ref.get("fields", ()):
                key = (ref["kind"], ref["id"], field)
                if key in seen or field in {"sources", "reference_origin"}:
                    continue
                seen.add(key)
                row = {"kind": ref["kind"], "id": ref["id"], "field": field,
                       "current": read(obj.model_dump(mode="json"), field) if obj else {"deleted": True}}
                if getattr(obj, "alternatives", ()) and (field.startswith("properties.") or field in {"name", "description", "aliases", "tags"}):
                    row["current_alternatives"] = {alt.alternative_id: read(alternative_content(obj, alt).model_dump(mode="json"), field) for alt in obj.alternatives}
                result.append(row)
    return result



def validation_feedback(error):
    """Preserve all faulty paths without repeating input bodies or library URLs."""
    from pydantic import ValidationError
    if isinstance(error, ValidationError):
        return '\n'.join(f"{'.'.join(map(str, item['loc'])) or 'result'}: {item['msg']}"
                         for item in error.errors(include_url=False, include_input=False))
    return str(error)
