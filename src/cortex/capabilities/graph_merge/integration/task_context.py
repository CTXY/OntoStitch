"""Task-local graph views and mechanical impact analysis.

Read history never defines a task boundary. Sources remain immutable; aliases and
explicit lineage only retrieve provenance, never establish business equivalence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from cortex.capabilities.graph_merge.integration.context import PHYSICAL, references
from cortex.capabilities.graph_merge.integration.scope import content_consumers
from cortex.schema import GraphNode, GraphState
from cortex.schema.relation_contracts import effective_schema

_CORE_DEPENDENCIES = frozenset({"grounded_on", "depends_on", "applies_to"})


@dataclass
class LocalTask:
    id: str
    phase: str
    targets: set[str]
    checks: list[dict] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)
    feedback: list[dict | str] = field(default_factory=list)


def _indexes(state):
    return ({n.id: n for n in state.graph.nodes}, {e.id: e for e in state.graph.edges})


def _semantic(obj):
    # Branch sources change the justification of a competing version and remain
    # semantic. Only ordinary object-level provenance is excluded here.
    return obj.model_dump(mode="json", exclude={"sources", "reference_origin"})


def _changed(before, after):
    return {
        key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
    }


def _semantic_changes(before, after):
    return {
        key
        for key in before.keys() | after.keys()
        if key not in before
        or key not in after
        or _semantic(before[key]) != _semantic(after[key])
    }


def _metadata_edges(node):
    props = [node.properties] + [
        {
            k.removeprefix("properties."): v
            for k, v in a.overrides.items()
            if k.startswith("properties.")
        }
        for a in node.alternatives
    ]
    return {
        edge
        for p in props
        for group in p.get("relation_metadata", ())
        for edge in group["edges"]
    }


def _contract_affected_edges(before, after):
    old = {c.id: c for c in before.graph.relation_contracts}
    new = {c.id: c for c in after.graph.relation_contracts}
    changed = _changed(old, new)
    while True:
        descendants = {
            c.id for c in (*old.values(), *new.values()) if c.extends in changed
        }
        if descendants <= changed:
            break
        changed |= descendants
    return {
        e.id
        for e in (*before.graph.edges, *after.graph.edges)
        if e.edge_type in changed
    }


def affected_query_roots(before: GraphState, after: GraphState) -> set[str]:
    """Changed definitions and both endpoints of added/removed semantic edges.

    Sources-only updates do not reopen neighbourhood queries. Callers separately
    handle new evidence; this function does not certify metadata as safe.
    """
    old_nodes, old_edges = _indexes(before)
    new_nodes, new_edges = _indexes(after)
    result = _semantic_changes(old_nodes, new_nodes)
    for key in _semantic_changes(old_edges, new_edges) | _contract_affected_edges(
        before, after
    ):
        for edges in (old_edges, new_edges):
            edge = edges.get(key)
            if edge is not None:
                result.update((edge.from_node_id, edge.to_node_id))
    return result


def _core_dependency(predicate, state):
    contracts = {c.id: c for c in state.graph.relation_contracts}
    seen = set()
    while predicate and predicate not in seen:
        if predicate in _CORE_DEPENDENCIES:
            return True
        seen.add(predicate)
        contract = contracts.get(predicate)
        predicate = contract.extends if contract else None
    return False


def semantic_review_targets(
    before: GraphState, after: GraphState, task_targets, *, evidence_changed=False
):
    """Return semantic targets and provenance flags, never a blanket shared-rule closure.

    ``metadata_only`` describes the object delta, not permission to skip review.
    Source additions/removals must be checked against the evidence previously
    reviewed for this decision. The caller owns that evidence comparison.
    """
    task_targets = set(task_targets)
    old_nodes, old_edges = _indexes(before)
    new_nodes, new_edges = _indexes(after)
    changed_nodes = _semantic_changes(old_nodes, new_nodes)
    changed_edges = _semantic_changes(old_edges, new_edges)
    contract_edges = _contract_affected_edges(before, after)
    affected = set(changed_nodes)
    for key in changed_edges | contract_edges:
        for edges, state in ((old_edges, before), (new_edges, after)):
            edge = edges.get(key)
            if edge is not None and _core_dependency(edge.edge_type, state):
                affected.add(edge.from_node_id)
    # Direct declared consumers include expression refs, relation groups and all
    # applies_to intents. Do not recursively flood the graph through a hub.
    dependency_changes = changed_nodes | changed_edges
    for nodes in (old_nodes, new_nodes):
        affected.update(
            row["consumer"]
            for row in content_consumers(nodes.values(), dependency_changes)
        )
    for edges in (old_edges, new_edges):
        affected.update(
            e.from_node_id
            for e in edges.values()
            if e.edge_type in _CORE_DEPENDENCIES and e.to_node_id in changed_nodes
        )
    if evidence_changed:
        affected.update(task_targets)
        for edges in (old_edges, new_edges):
            for edge in edges.values():
                if edge.id in task_targets:
                    affected.update((edge.from_node_id, edge.to_node_id))
    targets = {
        key
        for key in affected
        if key in new_nodes and new_nodes[key].node_type not in PHYSICAL
    }
    provenance_changed = False
    added_sources = set()
    removed_sources = set()
    for old, new in ((old_nodes, new_nodes), (old_edges, new_edges)):
        for key in old.keys() & new.keys():
            a, b = old[key], new[key]
            added_sources.update(set(b.sources) - set(a.sources))
            removed_sources.update(set(a.sources) - set(b.sources))
            provenance_changed |= a.sources != b.sources or getattr(
                a, "reference_origin", None
            ) != getattr(b, "reference_origin", None)
    contracts_changed = {c.id: c for c in before.graph.relation_contracts} != {
        c.id: c for c in after.graph.relation_contracts
    }
    semantic_changed = changed_nodes | changed_edges | contract_edges
    return {
        "targets": targets,
        "semantic_changed_ids": semantic_changed,
        "metadata_only": not semantic_changed
        and not contracts_changed
        and not evidence_changed,
        "contracts_changed": contracts_changed,
        "provenance_changed": provenance_changed,
        "added_sources": tuple(
            sorted(added_sources, key=lambda s: s.model_dump_json())
        ),
        "removed_sources": tuple(
            sorted(removed_sources, key=lambda s: s.model_dump_json())
        ),
        "requires_evidence_check": bool(added_sources or removed_sources),
        "evidence_changed": evidence_changed,
    }


def _source_refs(origins, kind, key):
    if origins is None:
        return []
    values = (
        origins.origin_refs(kind, key)
        if hasattr(origins, "origin_refs")
        else origins.get((kind, key), origins.get(key, ()))
    )
    values = values or ()
    if isinstance(values, (str, dict)) or hasattr(values, "id"):
        values = [values]
    result = []
    for value in values:
        if isinstance(value, str):
            result.append((kind, value, None))
        elif isinstance(value, dict):
            result.append(
                (value.get("kind", kind), value["id"], value.get("alternative_id"))
            )
        else:
            result.append((value.kind, value.id, value.alternative_id))
    return result


def _alias(origins, kind, key):
    mapping = getattr(origins, "node_map" if kind == "node" else "edge_map", {})
    seen = set()
    while key in mapping and key not in seen:
        seen.add(key)
        key = mapping[key]
    return key


def _physical_closure(ids, lookup):
    result = {key for key in ids if key in lookup and lookup[key].node_type in PHYSICAL}
    pending = list(result)
    while pending:
        node = lookup[pending.pop()]
        # Catalog ownership only, never every column referenced by a table.
        refs = [node.properties.get("column_ref"), node.properties.get("table_ref")]
        for key in refs:
            if (
                key in lookup
                and lookup[key].node_type in PHYSICAL
                and key not in result
            ):
                result.add(key)
                pending.append(key)
    return result


def _summary(node, **extra):
    return {
        "id": node.id,
        "node_type": node.node_type,
        "name": node.name,
        "description": node.description,
        **extra,
    }


def _check_source_ids(checks):
    ids = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"source_id", "evidence_id"} and isinstance(item, str):
                    ids.add(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(checks)
    return ids


def _bound_packet(
    payload,
    required_nodes,
    required_edges,
    source_nodes,
    source_edges,
    candidate_roots,
    input_roots,
    *,
    budget=24000,
):
    """Defer optional adjacency, never split a required semantic unit.

    This is a soft whole-packet budget: complete focus, evidence, contracts and
    declared semantic groups can exceed it. Every deferred view is explicit and
    remains accessible through existing graph inspection, starting at offset 0
    because inspector ordering differs from this packet's ordering.
    """

    def size():
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    original_size = size()
    if original_size <= budget:
        return
    original = {
        key: payload[key]
        for key in (
            "candidate_nodes",
            "candidate_relations",
            "nodes",
            "relations",
            "physical_facts",
        )
    }
    requirements = {
        "candidate_nodes": required_nodes,
        "candidate_relations": required_edges,
        "nodes": source_nodes,
        "relations": source_edges,
    }
    for key, required in requirements.items():
        payload[key] = [row for row in original[key] if row["id"] in required]
    # Directories are hints, not an alternative full copy of a hub's neighbours.
    payload["neighbor_directory"] = [
        {key: row[key] for key in ("id", "node_type", "name")}
        for row in payload["neighbor_directory"][:40]
    ]
    needed = {
        ref
        for key in ("candidate_nodes", "nodes")
        for row in payload[key]
        for ref in references(GraphNode.model_validate(row))
    }
    needed.update(
        endpoint
        for key in ("candidate_relations", "relations")
        for row in payload[key]
        for endpoint in (row["from_node_id"], row["to_node_id"])
    )
    physical = {
        row["id"]: GraphNode.model_validate(row) for row in original["physical_facts"]
    }
    keep_physical = _physical_closure(needed, physical)
    payload["physical_facts"] = [
        row for row in original["physical_facts"] if row["id"] in keep_physical
    ]
    evidence_ids = _check_source_ids(payload["inspection_signals"])
    for key in requirements:
        for row in payload[key]:
            evidence_ids.update(ref["source_id"] for ref in row.get("sources", ()))
            for alternative in row.get("alternatives", ()):
                evidence_ids.update(ref["source_id"] for ref in alternative["sources"])
    payload["evidence"] = [
        row
        for row in payload["evidence"]
        if row["id"] in evidence_ids
        or any(ref["source_id"] in evidence_ids for ref in row["source_refs"])
    ]
    retained_evidence = {row["id"] for row in payload["evidence"]}
    payload["evidence_directory"] = [
        row for row in payload["evidence_directory"] if row["id"] in retained_evidence
    ]
    loaded_edges = {row["id"] for row in payload["candidate_relations"]}
    current_predicates = {row["edge_type"] for row in payload["candidate_relations"]}
    source_predicates = {row["edge_type"] for row in payload["relations"]}
    payload["active_contracts"] = [
        {**row, "used_by": [key for key in row["used_by"] if key in loaded_edges]}
        for row in payload["active_contracts"]
        if row["contract"]["id"] in current_predicates
    ]
    payload["local_contracts"] = [
        row for row in payload["local_contracts"] if row["id"] in source_predicates
    ]
    payload["task_scope"]["read_only_context"] = sorted(
        {
            row["id"]
            for key in ("candidate_nodes", "candidate_relations")
            for row in payload[key]
        }
        - set(payload["task_scope"]["current_targets"])
    )
    # Full optional definitions can be fetched by ID; keep a small discovery page.
    deferred = []
    for node_key, edge_key, view, roots in (
        ("candidate_nodes", "candidate_relations", "candidate", candidate_roots),
        ("nodes", "relations", "input", input_roots),
    ):
        loaded_nodes = {row["id"] for row in payload[node_key]}
        loaded_edges = {row["id"] for row in payload[edge_key]}
        missing_nodes = [
            row for row in original[node_key] if row["id"] not in loaded_nodes
        ]
        missing_edges = [
            row for row in original[edge_key] if row["id"] not in loaded_edges
        ]
        if missing_nodes or missing_edges:
            deferred.append(
                {
                    "view": view,
                    "unloaded_nodes": len(missing_nodes),
                    "unloaded_relations": len(missing_edges),
                    "nodes": [
                        {key: row[key] for key in ("id", "node_type", "name")}
                        for row in missing_nodes[:40]
                    ],
                    "directory_has_more": len(missing_nodes) > 40,
                    "next_query": {
                        "mode": "relations",
                        "view": view,
                        "ids": sorted(roots),
                        "offset": 0,
                        "limit": 40,
                    },
                    "note": "Inspect adjacent relation pages, then request complete endpoint nodes by ID; skip already loaded IDs.",
                }
            )
    payload["more_relations"] = len(payload["relations"]) < len(original["relations"])
    payload["more_candidate_relations"] = len(payload["candidate_relations"]) < len(
        original["candidate_relations"]
    )
    payload["task_scope"].update(
        {
            "complete": payload["task_scope"]["complete"] and not deferred,
            "required_units_complete": not payload["missing_source_refs"]
            and not payload["task_scope"]["missing_targets"],
            "node_count": len(payload["candidate_nodes"]),
            "edge_count": len(payload["candidate_relations"]),
            "deferred_context": deferred,
            "soft_character_budget": budget,
            "original_characters": original_size,
        }
    )
    # Contracts/QA and indivisible groups are deliberately never cut to meet size.
    payload["task_scope"]["budget_exceeded_by_required_units"] = size() > budget


def _task_distinctions(context, current, payload, task, origins, source_ids):
    """Carry local identity reasoning without turning provenance into identity.

    Member handles stay in the immutable input namespace. Successor references
    are explicitly recorded aliases or lineage-group destinations, never name,
    predicate, or natural-language matches.
    """
    decision = getattr(context, "identity_decision", None) or {}
    if hasattr(decision, "model_dump"):
        decision = decision.model_dump(mode="json")
    distinctions = decision.get("distinctions", ())
    if not distinctions:
        return
    relevant = set(task.targets) | set(source_ids)
    relevant.update(
        ref["id"]
        for row in payload["origin_links"]
        for ref in row["source_refs"]
        if ref["kind"] == "node"
    )
    rows = [
        dict(row) if isinstance(row, dict) else row.model_dump(mode="json")
        for row in distinctions
    ]
    rows = [row for row in rows if relevant.intersection(row["members"])]
    if not rows:
        return
    member_ids = {key for row in rows for key in row["members"]}
    successors = {key: set() for key in member_ids}
    for node in current.graph.nodes:
        for kind, original, _ in _source_refs(origins, "node", node.id):
            if kind == "node" and original in successors:
                successors[original].add(node.id)
    current_ids = {node.id for node in current.graph.nodes}
    for member in member_ids:
        target = _alias(origins, "node", member)
        if target in current_ids:
            successors[member].add(target)
    payload["identity_distinctions"] = [
        {
            **row,
            "current_refs": {
                key: [
                    {"kind": "node", "id": current_id}
                    for current_id in sorted(successors[key])
                ]
                for key in row["members"]
            },
        }
        for row in rows
    ]
    payload["identity_distinctions_scope"] = (
        "Members and reasons are immutable input analysis, not instructions or new evidence. current_refs are recorded aliases/possible lineage-group successors, not an identity inference."
    )


def build_task_context(
    context, current: GraphState, task: LocalTask, origins=None, before=None
):
    """Build complete local objects without read-history or unrestricted adjacency.

    ``origins`` may be the session lineage object exposing origin_refs/node_map/
    edge_map, or a mapping from current IDs to original references. Full graph
    state is retained by code; only this task packet is sent to the model.
    """
    context.candidate = current
    context.phase = task.phase
    nodes, edges = _indexes(current)
    old_nodes, old_edges = _indexes(before) if before is not None else ({}, {})
    root_nodes = {_alias(origins, "node", key) for key in task.targets}
    root_nodes &= nodes.keys()
    root_edges = {_alias(origins, "edge", key) for key in task.targets}
    root_edges &= edges.keys()
    group_members = {
        member for group in task.groups for member in group.get("members", [])
    }
    source_roots = {("node", key) for key in group_members}
    origin_links = []
    for kind, targets in (("node", root_nodes), ("edge", root_edges)):
        raw = context.raw_nodes if kind == "node" else context.raw_edges
        for key in sorted(targets):
            refs = _source_refs(origins, kind, key)
            if not refs and key in raw:
                refs = [(kind, key, None)]
            source_roots.update((k, v) for k, v, _ in refs)
            origin_links.append(
                {
                    "kind": kind,
                    "id": key,
                    "source_refs": [
                        {"kind": k, "id": v, "alternative_id": a} for k, v, a in refs
                    ],
                }
            )
    for key in task.targets:
        if key in context.raw_nodes:
            source_roots.add(("node", key))
        if key in context.raw_edges:
            source_roots.add(("edge", key))
    selected_edges = root_edges | {
        e.id
        for e in edges.values()
        if e.from_node_id in root_nodes or e.to_node_id in root_nodes
    }
    selected_nodes = root_nodes | {
        key
        for eid in selected_edges
        for key in (edges[eid].from_node_id, edges[eid].to_node_id)
    }
    consumer_rows = content_consumers(nodes.values(), root_nodes | root_edges)
    selected_nodes.update(row["consumer"] for row in consumer_rows)
    required_edges = root_edges | {
        key for key in selected_edges if edges[key].edge_type in _CORE_DEPENDENCIES
    }
    required_nodes = root_nodes | {row["consumer"] for row in consumer_rows}
    required_nodes.update(
        key
        for eid in root_edges
        for key in (edges[eid].from_node_id, edges[eid].to_node_id)
    )
    # Other decisions using a root Rule's operand are relevant even when intents
    # differ (e.g. a presentation decision hard-coding a selected representation).
    operands = {
        e.to_node_id
        for e in edges.values()
        if e.edge_type == "applies_to" and e.from_node_id in root_nodes
    }
    for edge in edges.values():
        if edge.edge_type == "applies_to" and edge.to_node_id in operands:
            selected_edges.add(edge.id)
            required_edges.add(edge.id)
            required_nodes.update((edge.from_node_id, edge.to_node_id))
            selected_nodes.update((edge.from_node_id, edge.to_node_id))
            if edge.from_node_id not in root_nodes:
                consumer_rows.append(
                    {"consumer": edge.from_node_id, "shared_operand": edge.to_node_id}
                )
    # Relation groups are indivisible, but only declared overlapping groups expand.
    while True:
        prior = set(selected_edges)
        for node in nodes.values():
            if (
                node.id in root_nodes or _metadata_edges(node) & selected_edges
            ) and _metadata_edges(node):
                selected_nodes.add(node.id)
                required_nodes.add(node.id)
                required_edges.update(_metadata_edges(node) & edges.keys())
                selected_edges.update(_metadata_edges(node) & edges.keys())
        if prior == selected_edges:
            break
    selected_nodes.update(
        key
        for eid in selected_edges
        for key in (edges[eid].from_node_id, edges[eid].to_node_id)
    )
    selected_nodes.update(
        ref
        for key in tuple(selected_nodes)
        for ref in references(nodes[key])
        if ref in nodes
    )
    # Retrieve all known direct owners of a touched physical endpoint. Shared
    # bindings are recall signals, not an automatic identity decision.
    bound = {
        ref
        for key in root_nodes
        for ref in references(nodes[key])
        if ref in nodes and nodes[ref].node_type in PHYSICAL
    }
    contests = []
    for endpoint in sorted(bound):
        owners = {
            e.from_node_id
            for e in edges.values()
            if e.edge_type == "grounded_on"
            and e.to_node_id == endpoint
            and nodes[e.from_node_id].node_type == "concept"
        }
        if len(owners) > 1:
            selected_nodes.update(owners)
            selected_edges.update(
                e.id
                for e in edges.values()
                if e.edge_type == "grounded_on" and e.to_node_id == endpoint
            )
            required_nodes.update(owners)
            required_edges.update(
                e.id
                for e in edges.values()
                if e.edge_type == "grounded_on" and e.to_node_id == endpoint
            )
            contests.append({"physical_id": endpoint, "owners": sorted(owners)})
    required_nodes.update(k for k in selected_nodes if nodes[k].alternatives)
    required_nodes.update(
        key
        for eid in required_edges
        for key in (edges[eid].from_node_id, edges[eid].to_node_id)
    )
    required_nodes.update(
        ref
        for key in tuple(required_nodes)
        for ref in references(nodes[key])
        if ref in nodes
    )
    # Physical definitions include every complete target/formula, plus ownership.
    physical_ids = set(selected_nodes) | {
        ref for key in selected_nodes for ref in references(nodes[key])
    }
    lookup = {**context.nodes, **nodes}
    physical_ids.update(
        ref
        for key in task.targets
        if key in old_nodes and key not in nodes
        for ref in references(old_nodes[key])
    )
    physical_ids = _physical_closure(physical_ids, lookup)
    source_nodes, source_edges = set(), set()
    missing_sources = []
    for kind, key in source_roots:
        key = (
            context.input_node_map.get(key, key)
            if kind == "node"
            else context.input_edge_map.get(key, key)
        )
        raw = context.raw_nodes if kind == "node" else context.raw_edges
        if key not in raw:
            missing_sources.append({"kind": kind, "id": key})
        elif kind == "node":
            source_nodes.add(key)
        else:
            source_edges.add(key)
    primary_source_nodes = set(source_nodes)
    required_source_edges = set(source_edges)
    required_source_edges.update(
        e.id
        for e in context.edges.values()
        if (
            e.from_node_id in primary_source_nodes
            or e.to_node_id in primary_source_nodes
        )
        and e.edge_type in _CORE_DEPENDENCIES
    )
    for key in primary_source_nodes:
        required_source_edges.update(
            _metadata_edges(context.nodes.get(key, context.raw_nodes[key]))
        )
    source_edges.update(
        e.id
        for e in context.edges.values()
        if e.from_node_id in primary_source_nodes
        or e.to_node_id in primary_source_nodes
    )
    for key in tuple(source_nodes):
        source_edges.update(
            _metadata_edges(context.nodes.get(key, context.raw_nodes[key]))
        )
    source_edges &= context.edges.keys()
    source_nodes.update(
        key
        for eid in source_edges
        for key in (context.edges[eid].from_node_id, context.edges[eid].to_node_id)
        if key in context.nodes and context.nodes[key].node_type not in PHYSICAL
    )
    physical_ids |= _physical_closure(
        {
            ref
            for key in source_nodes
            for ref in references(context.nodes.get(key, context.raw_nodes.get(key)))
        }
        | {
            key
            for eid in source_edges
            for key in (context.edges[eid].from_node_id, context.edges[eid].to_node_id)
        },
        lookup,
    )
    evidence_ids = _check_source_ids(task.checks)
    evidence_objects = [nodes[k] for k in root_nodes] + [edges[k] for k in root_edges]
    evidence_objects += [
        old_nodes[k] for k in task.targets if k in old_nodes and k not in nodes
    ]
    evidence_objects += [context.raw_nodes[k] for k in primary_source_nodes] + [
        context.raw_edges[k] for k in source_edges
    ]
    for obj in evidence_objects:
        evidence_ids.update(s.source_id for s in obj.sources)
        evidence_ids.update(
            s.source_id for a in getattr(obj, "alternatives", ()) for s in a.sources
        )
    evidence = [
        row
        for row in context.evidence_rows()
        if row["id"] in evidence_ids
        or any(s["source_id"] in evidence_ids for s in row["source_refs"])
    ]
    contracts = effective_schema(context.schema, current.graph).edge_type_index()
    contracts.update({c.id: c for c in context.contracts if c.id not in contracts})
    predicates = {edges[k].edge_type for k in selected_edges} | {
        context.edges[k].edge_type for k in source_edges
    }
    source_only_predicates = predicates - {edges[k].edge_type for k in selected_edges}
    target_types = {
        nodes[k].node_type for k in root_nodes if nodes[k].node_type not in PHYSICAL
    }
    target_types |= {
        context.raw_nodes[k].node_type
        for k in primary_source_nodes
        if context.raw_nodes[k].node_type not in PHYSICAL
    }
    target_types |= {
        old_nodes[k].node_type
        for k in task.targets
        if k in old_nodes and old_nodes[k].node_type not in PHYSICAL
    }
    from cortex.capabilities.sql_training.llm.generation_rule_taxonomy import (
        operation_registry,
    )
    from cortex.capabilities.sql_training.llm.prompts.asset_contract import (
        render_ontology_asset_contract,
    )

    guidance = {
        kind: {
            "guidance": render_ontology_asset_contract([kind]),
            "fields": {
                key: value.model_dump(mode="json")
                for key, value in context.schema.effective_node_fields(kind).items()
            },
        }
        for kind in sorted(target_types)
        if kind in {"concept", "metric", "generation_rule", "event"}
    }
    intents = {
        intent
        for key in root_nodes
        for intent in nodes[key].properties.get("operation_intents", [])
    }
    intents.update(
        intent
        for key in primary_source_nodes
        for intent in context.raw_nodes[key].properties.get("operation_intents", [])
    )
    registry = operation_registry()
    known = {key: registry[key] for key in sorted(intents) if key in registry}
    missing_targets = sorted(
        task.targets
        - nodes.keys()
        - edges.keys()
        - context.raw_nodes.keys()
        - context.raw_edges.keys()
        - old_nodes.keys()
        - old_edges.keys()
    )
    payload: dict[str, Any] = {
        "phase": task.phase,
        "task_id": task.id,
        "task_scope": {
            "targets": sorted(task.targets),
            "current_targets": sorted(root_nodes | root_edges),
            "read_only_context": sorted(
                (selected_nodes | selected_edges) - root_nodes - root_edges
            ),
            "missing_targets": missing_targets,
            "view": "candidate",
            "revision": hashlib.sha256(current.model_dump_json().encode()).hexdigest(),
            "node_count": len(selected_nodes - physical_ids),
            "edge_count": len(selected_edges),
            "complete": not missing_sources and not missing_targets,
        },
        "nodes": [
            context.nodes.get(k, context.raw_nodes[k]).model_dump(mode="json")
            for k in sorted(source_nodes)
        ],
        "relations": [
            context.edges[k].model_dump(mode="json") for k in sorted(source_edges)
        ],
        "candidate_nodes": [
            nodes[k].model_dump(mode="json")
            for k in sorted(selected_nodes)
            if nodes[k].node_type not in PHYSICAL
        ],
        "candidate_relations": [
            edges[k].model_dump(mode="json") for k in sorted(selected_edges)
        ],
        "relations_total": len(source_edges),
        "more_relations": False,
        "candidate_relations_total": len(selected_edges),
        "more_candidate_relations": False,
        "physical_facts": [
            lookup[k].model_dump(mode="json") for k in sorted(physical_ids)
        ],
        "origin_links": origin_links,
        "binding_contests": contests,
        "missing_source_refs": missing_sources,
        "consumer_directory": [
            _summary(
                nodes[row["consumer"]],
                **{k: v for k, v in row.items() if k != "consumer"},
            )
            for row in consumer_rows
        ],
        "neighbor_directory": [
            _summary(nodes[k])
            for k in sorted(selected_nodes - root_nodes - physical_ids)
        ],
        "evidence": evidence,
        "evidence_directory": [
            {
                "id": row["id"],
                "source_ids": sorted({s["source_id"] for s in row["source_refs"]}),
            }
            for row in evidence
        ],
        "local_contracts": [
            contracts[k].model_dump(mode="json") for k in sorted(source_only_predicates)
        ],
        "active_contracts": [
            {
                "contract": contracts[k].model_dump(mode="json"),
                "used_by": sorted(
                    eid for eid in selected_edges if edges[eid].edge_type == k
                ),
            }
            for k in sorted(predicates - source_only_predicates)
        ],
        "node_guidance": guidance,
        "operation_registry": known,
        "unknown_operation_intents": sorted(intents - known.keys()),
        "feedback": list(task.feedback),
        "inspection_signals": list(task.checks),
    }
    if task.groups:
        payload["matched_groups"] = list(task.groups)
    if task.phase == "node_assess":
        payload["reviewed_deleted_ids"] = sorted(
            k
            for k in task.targets
            if k in old_nodes
            and k not in nodes
            and old_nodes[k].node_type not in PHYSICAL
        )
        payload["reviewed_node_ids"] = sorted(
            k for k in root_nodes if nodes[k].node_type not in PHYSICAL
        )
        payload["rationale_review_ids"] = sorted(
            k for k in root_nodes if nodes[k].node_type == "generation_rule"
        )
    if before is not None:
        payload["actual_changes"] = {
            label: {
                "created": sorted(b.keys() - a.keys()),
                "deleted": sorted(a.keys() - b.keys()),
                "updated": sorted(
                    key for key in a.keys() & b.keys() if a[key] != b[key]
                ),
            }
            for label, a, b in (
                ("nodes", old_nodes, nodes),
                ("relations", old_edges, edges),
            )
        }
        payload["deleted_nodes"] = [
            old_nodes[k].model_dump(mode="json")
            for k in sorted(old_nodes.keys() - nodes.keys())
        ]
        payload["deleted_relations"] = [
            old_edges[k].model_dump(mode="json")
            for k in sorted(old_edges.keys() - edges.keys())
        ]
    required_source_nodes = primary_source_nodes | {
        k for k in source_nodes if context.raw_nodes[k].alternatives
    }
    required_source_nodes.update(
        key
        for eid in required_source_edges
        if eid in context.edges
        for key in (context.edges[eid].from_node_id, context.edges[eid].to_node_id)
    )
    _task_distinctions(context, current, payload, task, origins, required_source_nodes)
    _bound_packet(
        payload,
        required_nodes,
        required_edges,
        required_source_nodes,
        required_source_edges,
        root_nodes | root_edges,
        primary_source_nodes | {key for kind, key in source_roots if kind == "edge"},
    )
    return payload
