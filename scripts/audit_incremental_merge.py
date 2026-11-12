"""Independently audit a published merge using public inputs and result only.

No compiler, model decisions or private short-handle maps are trusted. This is a
mechanical preservation audit, not proof of business-semantic equivalence.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from cortex.application.merge_graph import GraphMergeResult
from cortex.application.ontology_graph import GraphManager
from cortex.schema import GraphState, SchemaDefinition, load_root_schema
from cortex.schema.references import remap_references
from cortex.schema.relation_metadata import remap_metadata_edges
from cortex.schema.relation_vocabulary import require_concrete_relation


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sources(obj, alternative_id=None):
    alternatives = getattr(obj, "alternatives", ())
    if alternative_id:
        return set(
            next(a for a in alternatives if a.alternative_id == alternative_id).sources
        )
    return set(obj.sources) | {s for a in alternatives for s in a.sources}


def _content(content, node_map, edge_map):
    data = content.model_dump(mode="json")
    data["properties"] = remap_metadata_edges(
        remap_references(data["properties"], node_map), edge_map
    )
    return data


def _scope(owner, alternative_id, edges, node_map=None):
    node_map = node_map or {}
    records = []
    for edge in edges.values():
        if edge.from_node_id != owner:
            continue
        allowed = edge.properties.get("alternative_ids", [])
        if allowed and alternative_id not in allowed:
            continue
        props = {k: v for k, v in edge.properties.items() if k != "alternative_ids"}
        records.append(
            (
                edge.edge_type,
                node_map.get(edge.to_node_id, edge.to_node_id),
                remap_references(props, node_map),
            )
        )
    return sorted(records, key=_json)


def audit_result(existing, incoming, result, schema):
    """Audit source dispositions, exact references, endpoints and real branches."""
    errors, warnings = [], []

    def fail(code, **details):
        errors.append({"code": code, **details})

    manager = GraphManager(schema)
    try:
        manager.validate_state(result.state)
        for edge in result.state.graph.edges:
            require_concrete_relation(edge.edge_type)
    except (ValueError, TypeError, KeyError) as exc:
        fail("invalid_final_state", error=str(exc))

    graphs = {
        "existing": existing.graph,
        "incoming": incoming.graph,
        "result": result.state.graph,
    }
    objects = {
        view: {
            kind: {o.id: o for o in getattr(graph, plural)}
            for kind, plural in (("node", "nodes"), ("edge", "edges"))
        }
        for view, graph in graphs.items()
    }
    dispositions = set()

    def resolve(ref, expected_view=None):
        if expected_view == "source" and ref.view not in {"existing", "incoming"}:
            raise ValueError("before must address an original input, not a result")
        if expected_view == "result" and ref.view != "result":
            raise ValueError("after must address a final result")
        obj = objects[ref.view][ref.kind][ref.id]
        if ref.alternative_id and (
            ref.kind != "node"
            or ref.alternative_id not in {a.alternative_id for a in obj.alternatives}
        ):
            raise ValueError("unknown alternative reference")
        return obj

    for index, change in enumerate(result.rewrites):
        if not change.before or not change.explanation.strip():
            fail(
                "invalid_rewrite",
                index=index,
                error="before and explanation must be nonempty",
            )
        try:
            before, after = set(), set()
            for ref in change.before:
                obj = resolve(ref, "source")
                dispositions.add((ref.view, ref.kind, ref.id, ref.alternative_id))
                before.update(_sources(obj, ref.alternative_id))
            for ref in change.after:
                after.update(_sources(resolve(ref, "result"), ref.alternative_id))
            if change.after and (missing := before - after):
                fail(
                    "rewrite_source_loss",
                    index=index,
                    missing=[
                        s.model_dump(mode="json")
                        for s in sorted(missing, key=lambda s: s.model_dump_json())
                    ],
                )
        except (KeyError, ValueError, StopIteration) as exc:
            fail("invalid_rewrite_reference", index=index, error=str(exc))

    maps, coverage = {}, {}
    for view, prefix in (("incoming", ""), ("existing", "existing_")):
        maps[view] = {}
        for kind in ("node", "edge"):
            rows = getattr(result, prefix + kind + "_mappings")
            mapping = {r.input_id: r.canonical_id for r in rows}
            maps[view][kind] = mapping
            if len(mapping) != len(rows):
                fail("duplicate_mapping", view=view, kind=kind)
            for old, new in mapping.items():
                if old not in objects[view][kind] or new not in objects["result"][kind]:
                    fail(
                        "invalid_mapping_endpoint",
                        view=view,
                        kind=kind,
                        input_id=old,
                        canonical_id=new,
                    )
                    continue
                source, target = objects[view][kind][old], objects["result"][kind][new]
                if kind == "node" and source.node_type != target.node_type:
                    fail("unsafe_type_alias", view=view, input_id=old, canonical_id=new)
                if (view, kind, old, None) not in dispositions and (
                    missing := _sources(source) - _sources(target)
                ):
                    fail(
                        "mapping_source_loss",
                        view=view,
                        kind=kind,
                        input_id=old,
                        missing=[s.model_dump(mode="json") for s in missing],
                    )
            missing = [
                key
                for key in objects[view][kind]
                if key not in mapping and (view, kind, key, None) not in dispositions
            ]
            if missing:
                fail("unaccounted_source_objects", view=view, kind=kind, ids=missing)
            coverage[view + "_" + kind] = {
                "total": len(objects[view][kind]),
                "mapped": len(mapping),
                "explicitly_rewritten": sum(
                    (view, kind, k, None) in dispositions for k in objects[view][kind]
                ),
            }

    # Direct edge aliases cannot silently change endpoints. Reconstructed edges
    # belong in explicit rewrites, which may legitimately be many-to-many.
    default_edges = 0
    root_contracts = schema.edge_type_index()
    retained_nodes = {
        view: {
            ref.id
            for change in result.rewrites
            for ref in change.before
            if ref.view == view
            and ref.kind == "node"
            and any(
                after.kind == "node" and after.id == ref.id and after.view == "result"
                for after in change.after
            )
        }
        for view in ("existing", "incoming")
    }

    def destination(view, key):
        return maps[view]["node"].get(
            key,
            key
            if key in retained_nodes[view] and key in objects["result"]["node"]
            else None,
        )

    for view in ("existing", "incoming"):
        nm, em = maps[view]["node"], maps[view]["edge"]
        source_contracts = root_contracts | {
            c.id: c for c in graphs[view].relation_contracts
        }
        final_contracts = root_contracts | {
            c.id: c for c in graphs["result"].relation_contracts
        }
        for old, new in em.items():
            if old not in objects[view]["edge"] or new not in objects["result"]["edge"]:
                continue
            a, b = objects[view]["edge"][old], objects["result"]["edge"][new]
            expected = (
                destination(view, a.from_node_id),
                destination(view, a.to_node_id),
            )
            if None in expected or expected != (b.from_node_id, b.to_node_id):
                fail("unsafe_edge_alias", view=view, input_id=old, canonical_id=new)
            same_predicate = a.edge_type == b.edge_type
            if not same_predicate:
                ac, bc = (
                    source_contracts.get(a.edge_type),
                    final_contracts.get(b.edge_type),
                )
                same_predicate = bool(
                    ac
                    and bc
                    and ac.model_dump(exclude={"id"}) == bc.model_dump(exclude={"id"})
                )
            if not same_predicate:
                fail(
                    "unsafe_predicate_alias", view=view, input_id=old, canonical_id=new
                )
            if a.reference_origin == "default" and all(
                objects[view]["node"][key].node_type
                in {"table", "column", "dimension_value"}
                for key in (a.from_node_id, a.to_node_id)
            ):
                default_edges += 1

    conflicts = []
    for owner in result.state.conflict_nodes:
        variants, signatures = [], []
        for alternative in owner.alternatives:
            try:
                resolved = manager.resolve_alternative(owner, alternative)
                scope = _scope(
                    owner.id, alternative.alternative_id, objects["result"]["edge"]
                )
                props = dict(resolved.properties)
                if owner.node_type == "generation_rule":
                    props.pop("reason", None)
                    substantive = {"properties": props}
                else:
                    substantive = {
                        "description": resolved.description,
                        "properties": props,
                    }
                signatures.append(_json((substantive, scope)))
                variants.append(
                    {
                        "alternative_id": alternative.alternative_id,
                        "resolved_content": resolved.model_dump(mode="json"),
                        "sources": [
                            s.model_dump(mode="json") for s in alternative.sources
                        ],
                        "active_outgoing_scope": scope,
                    }
                )
            except (ValueError, TypeError, KeyError) as exc:
                fail(
                    "unresolvable_alternative",
                    node_id=owner.id,
                    alternative_id=alternative.alternative_id,
                    error=str(exc),
                )
        if len(set(signatures)) != len(signatures):
            fail("duplicate_substantive_alternatives", node_id=owner.id)
        conflicts.append(
            {
                "node_id": owner.id,
                "node_type": owner.node_type,
                "conflict_points": [
                    p.model_dump(mode="json") for p in owner.conflict_points
                ],
                "alternatives": variants,
            }
        )

    # Check a pre-existing conflict has not silently lost a branch or its sources.
    for view in ("existing", "incoming"):
        nm, em = maps[view]["node"], maps[view]["edge"]
        for old in objects[view]["node"].values():
            if not old.alternatives:
                continue
            current = objects["result"]["node"].get(destination(view, old.id))
            for alt in old.alternatives:
                branch_rewrite = (
                    view,
                    "node",
                    old.id,
                    alt.alternative_id,
                ) in dispositions
                current_alt = (
                    next(
                        (
                            a
                            for a in current.alternatives
                            if a.alternative_id == alt.alternative_id
                        ),
                        None,
                    )
                    if current
                    else None
                )
                if current_alt is None:
                    if not branch_rewrite:
                        fail(
                            "source_alternative_lost",
                            view=view,
                            node_id=old.id,
                            alternative_id=alt.alternative_id,
                        )
                    continue
                before_content = _content(manager.resolve_alternative(old, alt), nm, em)
                after_content = manager.resolve_alternative(
                    current, current_alt
                ).model_dump(mode="json")
                old_scope = _scope(
                    old.id, alt.alternative_id, objects[view]["edge"], nm
                )
                new_scope = _scope(
                    current.id, current_alt.alternative_id, objects["result"]["edge"]
                )
                changed = before_content != after_content or old_scope != new_scope
                if (
                    changed
                    and not branch_rewrite
                    and (view, "node", old.id, None) not in dispositions
                ):
                    fail(
                        "source_alternative_changed_without_disposition",
                        view=view,
                        node_id=old.id,
                        alternative_id=alt.alternative_id,
                    )
                if not branch_rewrite and set(alt.sources) - set(current_alt.sources):
                    fail(
                        "source_alternative_source_loss",
                        view=view,
                        node_id=old.id,
                        alternative_id=alt.alternative_id,
                    )

    return {
        "status": "failed" if errors else "passed",
        "errors": errors,
        "warnings": warnings,
        "coverage": coverage,
        "default_physical_edge_mappings_checked": default_edges,
        "final_counts": {
            "nodes": len(result.state.graph.nodes),
            "edges": len(result.state.graph.edges),
            "node_types": dict(Counter(n.node_type for n in result.state.graph.nodes)),
            "conflict_nodes": len(conflicts),
        },
        "resolved_conflicts": conflicts,
        "scope": "Mechanical final-state, public correspondence, exact SourceReference and branch-preservation checks. Rewrite groups do not establish pairwise equivalence or per-result evidential support. Semantic order-equivalence requires separate review.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path, nargs="?")
    for name in ("existing", "incoming", "result", "schema", "output"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    root = args.run_directory
    workflow = (
        json.loads((root / "workflow.json").read_text())
        if root and (root / "workflow.json").exists()
        else {}
    )
    request = workflow.get("request", {})

    def state_input(path, name):
        if path:
            return GraphState.model_validate_json(path.read_text())
        if name in request:
            return GraphState.model_validate(request[name])
        fallback = root / (name + "_state.json") if root else None
        if fallback and fallback.exists():
            return GraphState.model_validate_json(fallback.read_text())
        parser.error(f"Supply --{name}; no auditable original {name} input found")

    existing, incoming = (
        state_input(args.existing, "existing"),
        state_input(args.incoming, "incoming"),
    )
    result_path = args.result or (root / "merge_report.json" if root else None)
    if result_path is None:
        parser.error("Supply --result or run_directory")
    result = GraphMergeResult.model_validate_json(result_path.read_text())
    schema = (
        SchemaDefinition.model_validate_json(args.schema.read_text())
        if args.schema
        else SchemaDefinition.model_validate(request["schema"])
        if "schema" in request
        else load_root_schema()
    )
    report = audit_result(existing, incoming, result, schema)
    target = args.output or (root / "incremental_audit.json" if root else None)
    if target:
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(_json({k: v for k, v in report.items() if k != "resolved_conflicts"}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
