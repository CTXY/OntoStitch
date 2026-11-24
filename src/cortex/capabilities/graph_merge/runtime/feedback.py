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


def current_repair_fields(candidate, findings, *, context=None, before=None):
    """Read issue fields from their explicit view, never substitute old content."""
    from cortex.schema.graph import alternative_content
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
        issues = finding.get("issues", []) if "issues" in finding else [finding]
        for ref in (ref for issue in issues if isinstance(issue, dict) for ref in issue.get("refs", [])):
            view = ref.get("view", "candidate")
            if context is not None:
                from cortex.capabilities.graph_merge.consolidation.review import graph_view
                try:
                    nodes, edges, _ = graph_view(context, view, before=before, candidate=candidate)
                except ValueError:
                    nodes, edges = {}, {}
            else:
                state = candidate if view == "candidate" else before if view == "before" else None
                nodes = {item.id: item for item in state.graph.nodes} if state is not None else {}
                edges = {item.id: item for item in state.graph.edges} if state is not None else {}
            obj = (nodes if ref["kind"] == "node" else edges).get(ref["id"])
            for field in ref.get("fields", ()):
                key = (view, ref["kind"], ref["id"], field)
                if key in seen or field in {"sources", "common_sources", "reference_origin"}:
                    continue
                seen.add(key)
                row = {"kind": ref["kind"], "id": ref["id"], "view": view, "field": field,
                       "value": read(obj.model_dump(mode="json"), field) if obj else {"not_present": True}}
                if getattr(obj, "alternatives", ()) and (field.startswith("properties.") or field in {"name", "description", "aliases", "tags"}):
                    row["alternatives"] = {alt.alternative_id: read(alternative_content(obj, alt).model_dump(mode="json"), field) for alt in obj.alternatives}
                result.append(row)
    return result



def validation_feedback(error):
    """Preserve all faulty paths without repeating input bodies or library URLs."""
    from pydantic import ValidationError
    if isinstance(error, ValidationError):
        return '\n'.join(f"{'.'.join(map(str, item['loc'])) or 'result'}: {item['msg']}"
                         for item in error.errors(include_url=False, include_input=False))
    return str(error)
