"""Mechanical field diffs; never infer business intent from prose."""






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
                from cortex.capabilities.graph_merge.graph.views import graph_view
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
