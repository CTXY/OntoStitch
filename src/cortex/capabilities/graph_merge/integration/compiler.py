"""Compile an explicit semantic proposal; never infer business edges or predicates."""

from __future__ import annotations

import json
from dataclasses import dataclass

from cortex.application.merge_graph import (
    CanonicalIdMapping,
    GraphMergeResult,
    MergeElementRef,
    StructuralRewrite,
)
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.context import (
    PHYSICAL,
    remap_edge,
    remap_node,
)
from cortex.capabilities.graph_merge.transactions import apply_merge_state
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState
from cortex.schema.relation_contracts import merge_relation_contracts
from cortex.schema.relation_vocabulary import require_concrete_relation


def unique(values):
    return tuple(dict.fromkeys(values))


def identity_map(context, decision):
    mappings = dict(context.physical_map)
    used = set()
    old = {n.id for n in context.existing.graph.nodes}
    for group in decision.groups:
        members = set(group.members)
        if len(members) != len(group.members) or members & used:
            raise ValueError(
                "Identity groups must have distinct, non-overlapping members"
            )
        if not members <= context.read_ids:
            raise ValueError(
                f"Read identity members before deciding: {sorted(members - context.read_ids)}"
            )
        types = {context.nodes[key].node_type for key in members}
        if len(types) != 1 or types & PHYSICAL:
            raise ValueError(
                "Identity aliases require one semantic type; type correction is an explicit rewrite"
            )
        canonical = min(members, key=lambda key: (key not in old, key))
        mappings.update({key: canonical for key in members})
        used |= members
    distinctions = {frozenset(d.members) for d in decision.distinctions}
    for d in decision.distinctions:
        if len(set(d.members)) != 2 or not set(d.members) <= context.read_ids:
            raise ValueError("Distinction members must be two read semantic nodes")
        if mappings.get(d.members[0], d.members[0]) == mappings.get(
            d.members[1], d.members[1]
        ):
            raise ValueError("A pair cannot be both same and different identity")
    for a, b in context.mandatory:
        if (
            mappings.get(a, a) != mappings.get(b, b)
            and frozenset((a, b)) not in distinctions
        ):
            raise ValueError(
                f"Explicit identity decision needed for binding contest: {a}, {b}"
            )
    return mappings


def deduplicate_edges(edges):
    """Exact assertion equality only, including descriptions and branch scope."""
    by_signature, mappings = {}, {}
    for edge in edges:
        signature = (
            edge.edge_type,
            edge.from_node_id,
            edge.to_node_id,
            json.dumps(edge.properties, sort_keys=True, ensure_ascii=False),
        )
        previous = by_signature.get(signature)
        if previous:
            mappings[edge.id] = previous.id
            by_signature[signature] = previous.model_copy(
                update={"sources": unique((*previous.sources, *edge.sources))}
            )
        else:
            by_signature[signature] = edge
            mappings[edge.id] = edge.id
    return {e.id: e for e in by_signature.values()}, mappings


def mapped_base(context, decision):
    mappings = identity_map(context, decision)
    nodes = {}
    for n in context.nodes.values():
        key = mappings.get(n.id, n.id)
        if key not in nodes:
            nodes[key] = remap_node(context.nodes[key], mappings)
        current = nodes[key]
        nodes[key] = current.model_copy(
            update={
                "sources": unique((*current.sources, *n.sources)),
                "aliases": list(
                    dict.fromkeys(
                        [
                            *current.aliases,
                            *n.aliases,
                            *([n.name] if n.name != current.name else []),
                        ]
                    )
                ),
            }
        )
    edges, edge_map = deduplicate_edges(
        remap_edge(e, mappings) for e in context.edges.values()
    )
    nodes = {key: remap_node(n, {}, edge_map) for key, n in nodes.items()}
    return nodes, edges, mappings, edge_map


@dataclass
class Preview:
    state: GraphState
    node_map: dict[str, str]
    edge_map: dict[str, str]
    impacts: list[dict]


def compile_proposal(context, decision, proposal, *, strict=True):
    nodes, edges, node_map, edge_map = mapped_base(context, decision)
    before_nodes, before_edges = dict(nodes), dict(edges)
    changes_before = {
        (e.kind, e.id) for change in proposal.changes for e in change.before
    }
    edited = set()
    unexplained = []
    known_sources = {s for n in context.raw_nodes.values() for s in n.sources}
    known_sources |= {s for e in context.raw_edges.values() for s in e.sources}
    for n in context.raw_nodes.values():
        for a in n.alternatives:
            known_sources.update(a.sources)
    authorized_nodes = {node_map.get(key, key) for key in context.read_ids}
    authorized_edges = {
        edge_map.get(e.id, e.id)
        for e in context.edges.values()
        if e.from_node_id in context.read_ids or e.to_node_id in context.read_ids
    }
    for edit in proposal.edits:
        operation = edit.model_dump(exclude_none=True)
        kind = operation.get("operation")
        is_node = kind in {"create_node", "update_node", "delete_node", "set_conflict"}
        if not is_node and kind not in {"create_edge", "update_edge", "delete_edge"}:
            raise ValueError(f"Unsupported operation {kind!r}")
        label, cls, objects, aliases, allowed = (
            ("node", GraphNode, nodes, node_map, authorized_nodes)
            if is_node
            else ("edge", GraphEdge, edges, edge_map, authorized_edges)
        )
        deletion = kind.startswith("delete_")
        if deletion:
            raw_id = operation.get(label + "_id")
            key = aliases.get(raw_id, raw_id)
            obj = objects.get(key)
        else:
            raw = dict(operation.get(label) or {})
            raw_id = raw.get("id")
            key = aliases.get(raw_id, raw_id)
            raw["id"] = key
            previous = objects.get(key)
            if previous is not None and "sources" not in raw:
                raw["sources"] = [s.model_dump(mode="json") for s in previous.sources]
            obj = cls.model_validate(raw)
            obj = (
                remap_node(obj, node_map, edge_map)
                if is_node
                else remap_edge(obj, node_map)
            )
        if obj is None:
            raise ValueError(f"Cannot {kind}: missing {key}")
        if (label, key) in edited:
            raise ValueError(f"One explicit operation per element per proposal: {key}")
        edited.add((label, key))
        if kind.startswith("create_") and key in objects:
            raise ValueError(f"Use update for existing {key}")
        if not kind.startswith("create_") and key not in objects:
            raise ValueError(f"Use create for new {key}")
        if key in objects and key not in allowed:
            raise ValueError(f"Read {label} and its owner before modifying {key}")
        previous = (before_nodes if is_node else before_edges).get(key)
        if previous is not None and (
            (is_node and previous.node_type in PHYSICAL)
            or (
                not is_node
                and before_nodes[previous.from_node_id].node_type in PHYSICAL
                and before_nodes[previous.to_node_id].node_type in PHYSICAL
            )
        ):
            raise ValueError(
                "Merge Agent cannot modify existing physical catalog objects"
            )
        if is_node and previous is not None and obj.node_type != previous.node_type:
            raise ValueError(
                f"Existing node type cannot change for {key}; create a new ID, "
                "delete the old node, repair its references and record the rewrite"
            )
        if is_node and obj.node_type in PHYSICAL:
            raise ValueError("Merge Agent cannot edit physical catalog nodes")
        if (
            not is_node
            and obj.from_node_id in nodes
            and obj.to_node_id in nodes
            and nodes[obj.from_node_id].node_type in PHYSICAL
            and nodes[obj.to_node_id].node_type in PHYSICAL
        ):
            raise ValueError("Merge Agent cannot modify physical catalog edges")
        if not set(obj.sources) <= known_sources:
            raise ValueError(
                f"Invented source on {key}; use provided source references"
            )
        if is_node:
            if obj.consolidation_status == "unresolved":
                raise ValueError(
                    "Uncertain preservation is not a successful integration result"
                )
            for a in obj.alternatives:
                if not set(a.sources) <= known_sources:
                    raise ValueError(f"Invented alternative source on {key}")
        # An edited source object must have an explicit semantic disposition.
        original_ids = {
            ref
            for ref in (context.raw_nodes if is_node else context.raw_edges)
            if aliases.get(ref, ref) == key
        }
        if original_ids and not any(
            (label, ref) in changes_before for ref in original_ids
        ):
            unexplained.append({"operation": kind, "source_ids": sorted(original_ids)})
        if deletion:
            del objects[key]
        else:
            objects[key] = obj
    if unexplained:
        raise ValueError(f"Explain substantive edits in changes.before: {unexplained}")
    # No deletion cascades: missing endpoints/refs must be fixed explicitly.
    edges, final_edge_map = deduplicate_edges(edges.values())
    edge_map = {
        key: final_edge_map.get(value, value) for key, value in edge_map.items()
    }
    nodes = {key: remap_node(n, {}, final_edge_map) for key, n in nodes.items()}
    contracts = merge_relation_contracts(context.contracts, proposal.relation_contracts)
    candidate = GraphState(
        graph=GraphSpace(
            nodes=tuple(nodes.values()),
            edges=tuple(edges.values()),
            relation_contracts=contracts,
        )
    )
    if strict:
        GraphManager(context.schema).validate_state(candidate)
    else:
        from cortex.schema.validation import validate_graph_state
        validate_graph_state(candidate, context.schema)
    # Normalize incoming/edited neighborhoods without forcing unrelated legacy cleanup.
    affected_nodes = {
        node_map.get(key, key) for key in context.input_node_map.values()
    } | {key for kind, key in edited if kind == "node"}
    edited_edges = {key for kind, key in edited if kind == "edge"}
    for edge in candidate.graph.edges:
        if (
            edge.id in edited_edges
            or edge.from_node_id in affected_nodes
            or edge.to_node_id in affected_nodes
        ):
            if strict:
                require_concrete_relation(edge.edge_type)
    # Validate correspondence references, including actual alternative selection.
    for change in proposal.changes:
        for element in change.before:
            objects = context.raw_nodes if element.kind == "node" else context.raw_edges
            if element.id not in objects:
                raise ValueError(f"Unknown before handle {element.id}")
            if element.alternative_id and (
                element.kind != "node"
                or element.alternative_id
                not in {a.alternative_id for a in objects[element.id].alternatives}
            ):
                raise ValueError(f"Unknown before alternative {element.alternative_id}")
        for element in change.after:
            aliases = node_map if element.kind == "node" else edge_map
            key = aliases.get(element.id, element.id)
            objects = nodes if element.kind == "node" else edges
            if key not in objects:
                raise ValueError(f"Unknown after handle {element.id}")
            if element.alternative_id and (
                element.kind != "node"
                or element.alternative_id
                not in {a.alternative_id for a in objects[key].alternatives}
            ):
                raise ValueError(f"Unknown after alternative {element.alternative_id}")
    # A node alias never silently selects/remaps a competing version.
    before_alternatives = {
        (e.id, e.alternative_id)
        for c in proposal.changes
        for e in c.before
        if e.kind == "node" and e.alternative_id
    }
    for old in context.raw_nodes.values():
        current = nodes.get(node_map.get(old.id, old.id))
        current_alternatives = (
            {a.alternative_id: a for a in current.alternatives} if current else {}
        )
        normalized = remap_node(old, node_map, edge_map)
        for alternative in normalized.alternatives:
            target = current_alternatives.get(alternative.alternative_id)
            if (target is None or alternative.overrides != target.overrides) and (
                old.id,
                alternative.alternative_id,
            ) not in before_alternatives:
                raise ValueError(
                    f"Explicit branch correspondence required for {old.id}/{alternative.alternative_id}"
                )
    for kind, originals, mapping, final in (
        ("node", context.raw_nodes, node_map, nodes),
        ("edge", context.raw_edges, edge_map, edges),
    ):
        for key in originals:
            if mapping.get(key, key) not in final and (kind, key) not in changes_before:
                raise ValueError(
                    f"Unaccounted source {kind} {key}; no safe mapping or explicit rewrite"
                )
    impacts = []
    for key, n in before_nodes.items():
        if n == nodes.get(key):
            continue
        source_ids = {raw for raw in context.nodes if node_map.get(raw, raw) == key}
        for raw in source_ids:
            for ref in context.consumers.get(raw, ()):
                impacts.append(
                    {"changed_node": key, "consumer": node_map.get(ref, ref)}
                )
        for e in before_edges.values():
            if key in (e.from_node_id, e.to_node_id):
                impacts.append(
                    {"changed_node": key, "relation": e.id, "preserved": e.id in edges}
                )
    return Preview(candidate, node_map, edge_map, impacts)


def commit_result(context, proposal, preview, *, run_id):
    # Short handles reduce model noise; they are never the final source identity.
    def publish_ids(items, existing_ids, input_map):
        inverse = {value: key for key, value in input_map.items()}
        occupied = set(existing_ids) | {
            item.id for item in items if item.id not in inverse
        }
        result = {}
        for item in items:
            if item.id not in inverse or item.id in existing_ids:
                continue
            public = inverse[item.id]
            while public in occupied:
                public = "merged::" + public
            occupied.add(public)
            result[item.id] = public
        return result

    final_nodes = publish_ids(
        preview.state.graph.nodes,
        {n.id for n in context.existing.graph.nodes},
        context.input_node_map,
    )
    final_edges = publish_ids(
        preview.state.graph.edges,
        {e.id for e in context.existing.graph.edges},
        context.input_edge_map,
    )
    published = preview.state.model_copy(
        update={
            "graph": preview.state.graph.model_copy(
                update={
                    "nodes": tuple(
                        remap_node(n, final_nodes, final_edges)
                        for n in preview.state.graph.nodes
                    ),
                    "edges": tuple(
                        remap_edge(e, final_nodes, final_edges)
                        for e in preview.state.graph.edges
                    ),
                }
            )
        }
    )
    node_aliases = {
        key: final_nodes.get(value, value) for key, value in preview.node_map.items()
    }
    node_aliases.update(
        {key: value for key, value in final_nodes.items() if key not in node_aliases}
    )
    edge_aliases = {
        key: final_edges.get(value, value) for key, value in preview.edge_map.items()
    }
    edge_aliases.update(
        {key: value for key, value in final_edges.items() if key not in edge_aliases}
    )
    manager = GraphManager(context.schema, context.existing)
    base_revision = manager.revision
    actual = apply_merge_state(manager, published, request_id=f"integration:{run_id}")
    # GraphManager may normalize default edges. It must not silently remove authored meaning.
    actual_nodes = {n.id for n in actual.graph.nodes}
    actual_edges = {e.id for e in actual.graph.edges}
    missing = {
        e.id for e in published.graph.edges if e.reference_origin != "default"
    } - actual_edges
    if missing:
        raise ValueError(
            f"Commit unexpectedly removed authored relations: {sorted(missing)}"
        )

    def semantic_content(state):
        return (
            {
                n.id: n.model_dump(mode="json", exclude={"sources"})
                for n in state.graph.nodes
                if n.node_type not in PHYSICAL
            },
            {
                e.id: e.model_dump(mode="json", exclude={"sources", "reference_origin"})
                for e in state.graph.edges
                if e.reference_origin != "default"
            },
        )

    if semantic_content(actual) != semantic_content(published):
        raise ValueError(
            "Commit changed assessed semantic content; no result published"
        )
    # Catalog ownership is regenerated by GraphManager under public node IDs.
    # Recover its correspondence from endpoints, never use this shortcut for
    # authored business assertions whose scope/properties may differ.
    actual_node_index = {n.id: n for n in actual.graph.nodes}
    catalog_index = {}
    for edge in actual.graph.edges:
        # An authored ownership edge suppresses default generation for the same
        # catalog fact. It can be the destination, but only default source edges
        # enter this repair below; authored joins/business edges are not remapped.
        if all(
            actual_node_index[key].node_type in PHYSICAL
            for key in (edge.from_node_id, edge.to_node_id)
        ):
            signature = (edge.edge_type, edge.from_node_id, edge.to_node_id)
            catalog_index.setdefault(signature, []).append(edge.id)
    catalog_aliases = {}
    for edge in context.raw_edges.values():
        if edge.reference_origin != "default" or not all(
            context.raw_nodes[key].node_type in PHYSICAL
            for key in (edge.from_node_id, edge.to_node_id)
        ):
            continue
        signature = (
            edge.edge_type,
            node_aliases.get(edge.from_node_id, edge.from_node_id),
            node_aliases.get(edge.to_node_id, edge.to_node_id),
        )
        matches = catalog_index.get(signature, ())
        if len(matches) != 1:
            raise ValueError(
                f"Expected one committed catalog edge for {edge.id}; found {len(matches)}"
            )
        catalog_aliases[edge.id] = matches[0]
        catalog_aliases[edge_aliases.get(edge.id, edge.id)] = matches[0]
    edge_aliases = {
        key: catalog_aliases.get(value, value) for key, value in edge_aliases.items()
    }
    edge_aliases.update(catalog_aliases)
    inverse_nodes = {v: k for k, v in context.input_node_map.items()}
    inverse_edges = {v: k for k, v in context.input_edge_map.items()}

    def reference(e, *, after):
        inverse = inverse_nodes if e.kind == "node" else inverse_edges
        mapping = node_aliases if e.kind == "node" else edge_aliases
        return MergeElementRef(
            kind=e.kind,
            id=mapping.get(e.id, e.id) if after else inverse.get(e.id, e.id),
            view="result" if after else "incoming" if e.id in inverse else "existing",
            alternative_id=e.alternative_id,
        )

    changed = {(e.kind, e.id) for c in proposal.changes for e in c.before}

    def mapping_rows(source, mapping, final, kind):
        return tuple(
            CanonicalIdMapping(input_id=old, canonical_id=mapping.get(handle, handle))
            for old, handle in source.items()
            if mapping.get(handle, handle) in final
            and (
                (kind, handle) not in changed
                or (kind == "node" and handle in preview.node_map)
            )
        )

    return GraphMergeResult(
        run_id=run_id,
        state=actual,
        diagnostics=(),
        node_mappings=mapping_rows(
            context.input_node_map, node_aliases, actual_nodes, "node"
        ),
        edge_mappings=mapping_rows(
            context.input_edge_map, edge_aliases, actual_edges, "edge"
        ),
        existing_node_mappings=mapping_rows(
            {n.id: n.id for n in context.existing.graph.nodes},
            node_aliases,
            actual_nodes,
            "node",
        ),
        existing_edge_mappings=mapping_rows(
            {e.id: e.id for e in context.existing.graph.edges},
            edge_aliases,
            actual_edges,
            "edge",
        ),
        rewrites=tuple(
            StructuralRewrite(
                before=tuple(reference(e, after=False) for e in c.before),
                after=tuple(reference(e, after=True) for e in c.after),
                explanation=c.explanation,
                base_revision=base_revision,
                result_revision=manager.revision,
            )
            for c in proposal.changes
        ),
    )
