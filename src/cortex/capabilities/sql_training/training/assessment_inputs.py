"""Evidence selection for the independent assessor; no LLM or graph mutations."""

from cortex.capabilities.sql_training.graph.snapshots import graph_diff
from cortex.schema.relationships import is_dependency_edge
from cortex.capabilities.sql_training.learning.inputs import case_source
from cortex.capabilities.sql_training.training.assessment_views import (
    compact_node, compact_edge, group_context_facts, physical_properties, sql_attempts,
)


def assessment_payload(*, record, schema, baseline, accepted, trial, groups, targets, validation, prior_evidence=None):
    changed = set(graph_diff(accepted, trial))
    round_changed = set(graph_diff(baseline, trial))
    selected = set(changed)
    for group in groups:
        selected.update(group.member_ids)
    for attempt in validation.get("attempts", []):
        selected.update(attempt.get("selected_ids", []))
    # Changed/deleted relations require their endpoints even if node definitions did not change.
    for snapshot in (baseline, accepted, trial):
        for edge in snapshot.graph.edges:
            if edge.id in selected:
                selected.update((edge.from_node_id, edge.to_node_id))
    # Follow knowledge dependencies in both directions for regression review.
    # Physical endpoints are included, but do not expand into the whole catalog.
    while True:
        previous = set(selected)
        for snapshot in (baseline, accepted, trial):
            expandable = {a.id for a in snapshot.editable_assets} & selected
            for edge in snapshot.graph.edges:
                if edge.from_node_id in expandable:
                    selected.add(edge.to_node_id)
                if edge.to_node_id in expandable:
                    selected.add(edge.from_node_id)
        if selected == previous:
            break

    value_columns = {
        ref for snapshot in (baseline, accepted, trial)
        for asset in snapshot.assets if asset.id in selected and asset.node_type.value == "dimension_value"
        for ref in asset.properties.references
    }

    def nodes(snapshot, ids):
        rows = []
        for asset in snapshot.assets:
            if asset.id not in ids:
                continue
            if asset.node_type.value in {"table", "column"}:
                props = asset.properties
                rows.append({"id": asset.id, "node_type": asset.node_type.value, "name": asset.name,
                             "properties": physical_properties(props, include_values=asset.id in value_columns)})
            else:
                rows.append(compact_node(asset.model_dump(mode="json", exclude={"sources"}, exclude_none=True)))
        return rows

    def edges(snapshot, ids):
        return [compact_edge(e)
                for e in snapshot.graph.edges
                if e.id in ids or (e.from_node_id in ids and e.to_node_id in ids)]

    current_edges = edges(trial, selected)
    def prior_edges(snapshot, ids):
        return [compact_edge(e)
                for e in snapshot.graph.edges
                if e.id in ids or e.from_node_id in ids or e.to_node_id in ids]

    old_edges = prior_edges(accepted, changed)
    baseline_edges = prior_edges(baseline, round_changed)
    relation_types = {e["type"] for e in [*current_edges, *old_edges, *baseline_edges]}
    same_baseline = baseline.revision == accepted.revision
    current_nodes = {a["id"]: a for a in nodes(trial, selected)}
    previous_nodes = {a["id"]: a for a in nodes(accepted, selected)}
    current_items = {**current_nodes, **{e["id"]: e for e in current_edges}}
    previous_items = {**previous_nodes, **{e["id"]: e for e in prior_edges(accepted, selected)}}
    members = sorted({m for g in groups for m in g.member_ids})
    known_targets = {t["target_id"] for t in targets}
    attempts = sql_attempts(validation)
    units = []
    for member in members:
        candidate, before = current_items.get(member), previous_items.get(member)
        if candidate is None and before is None:
            raise ValueError(f"Assessment group member is absent from both snapshots: {member}")
        facts = []
        for attempt in validation.get("attempts", []):
            roots = set(attempt.get("selected_ids", []))
            available = set(attempt.get("context_ids", []))
            available_edges = set(attempt.get("context_edge_ids", []))
            via = []
            for root in roots:
                reached = {root} if root in available else set()
                while True:
                    old = set(reached)
                    for edge in trial.graph.edges:
                        if edge.from_node_id in reached and edge.to_node_id in available and edge.id in available_edges and is_dependency_edge(edge):
                            reached.add(edge.to_node_id)
                    if reached == old:
                        break
                edge = next((e for e in trial.graph.edges if e.id == member), None)
                if member != root and (member in reached or (edge and edge.id in available_edges and edge.from_node_id in reached)):
                    via.append(root)
            facts.append({
                "attempt_id": attempt["attempt"],
                "raw_retrieved": member in attempt["raw_retrieved_ids"] if "raw_retrieved_ids" in attempt else None,
                "selected": member in attempt["selected_ids"] if "selected_ids" in attempt else None,
                "included_via": sorted(via),
                "present_in_context": (member in available if member in current_nodes else member in available_edges) if "context_ids" in attempt and (member in current_nodes or "context_edge_ids" in attempt) else None,
            })
        units.append({
            "unit_id": member,
            "change_kind": "delete" if candidate is None else "add" if before is None else "modify",
            "candidate": candidate,
            **({"accepted_before": before} if before is not None else {}),
            "target_ids": sorted({t for g in groups if member in g.member_ids for t in g.target_ids if t in known_targets}),
            "context_facts": group_context_facts(facts),
        })
    physical = [a for a in current_nodes.values() if a["node_type"] in {"table", "column"}]
    # Provide metadata only for physical endpoints in scope, without duplicating identities.
    for item in physical:
        metadata = record.get("gold_column_metadata", {}).get(item["id"], {})
        for key in ("description", "unit", "semantic_role", "key_role"):
            if metadata.get(key) is not None:
                item["properties"][key] = metadata[key]
    return {
        "case": case_source(record),
        "candidate_revision": trial.revision,
        "accepted_revision": accepted.revision,
        "units": units,
        "physical_context": physical,
        "related_knowledge": {
            "nodes": [a for a in current_nodes.values() if a["id"] not in members and a["node_type"] not in {"table", "column"}],
            "edges": current_edges,
            "prior_edges": old_edges,
            "relation_definitions": [{"type": r.id, "description": r.description} for r in schema.edge_types if r.id in relation_types],
        },
        "round_baseline": None if same_baseline else {"revision": baseline.revision, "nodes": nodes(baseline, round_changed), "edges": baseline_edges},
        "change_groups": [g.model_dump(mode="json") for g in groups],
        "fixed_targets": targets,
        "attempts": attempts,
        "prior_evidence": prior_evidence or [],
    }
