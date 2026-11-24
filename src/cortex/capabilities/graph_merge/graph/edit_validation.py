"""Validate complete graph edit payloads before applying a local transaction."""
from cortex.application.ontology_graph import GraphManager
from cortex.schema import GraphEdge, GraphNode
from cortex.schema.semantic_validation import validate_semantic_properties
from cortex.schema.validation import _validate_properties

def edit_key(edit):
    kind = (
        "node"
        if edit["operation"].endswith("node") or edit["operation"] == "set_conflict"
        else "edge"
    )
    return kind, edit.get(kind + "_id") or edit[kind]["id"]


def content_errors(edits, schema):
    """Check all supplied objects, including complete conflict alternatives, early."""
    errors = []
    for edit in edits:
        try:
            kind, key = edit_key(edit)
            if edit["operation"].startswith("delete_"):
                continue
            if kind == "node":
                raw = edit[kind]
                for field, placeholder in [
                    ("name", raw.get("id")),
                    ("description", None),
                    ("aliases", []),
                    ("tags", []),
                ]:
                    if (
                        raw.get("alternatives")
                        and all(
                            field in a.get("overrides", {}) for a in raw["alternatives"]
                        )
                        and raw.get(field) != placeholder
                    ):
                        raise ValueError(
                            f"node.{field} must be {placeholder!r} because every alternative overrides it; put full differing values inside alternatives, not the common node"
                        )
            obj = (GraphNode if kind == "node" else GraphEdge).model_validate(
                edit[kind]
            )
            if kind == "node":
                variants = [
                    GraphManager.resolve_alternative(obj, a) for a in obj.alternatives
                ] or [obj]
                for variant in variants:
                    _validate_properties(
                        variant.properties,
                        schema.effective_node_fields(obj.node_type),
                        f"nodes.{key}.properties",
                    )
                    validate_semantic_properties(obj.node_type, variant.properties)
            elif (
                not isinstance(obj.properties.get("description"), str)
                or not obj.properties["description"].strip()
            ):
                raise ValueError("edge.properties.description must be nonempty")
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(
                {
                    "element": edit.get("node_id")
                    or edit.get("edge_id")
                    or (edit.get("node") or edit.get("edge") or {}).get("id"),
                    "path": "node.properties" if edit.get("node") else "edge",
                    "message": str(exc),
                }
            )
    return errors

