"""Evidence selection for the independent assessor; no LLM or graph mutations."""

from cortex.capabilities.sql_training.graph.snapshots import graph_diff
from cortex.capabilities.sql_training.learning.inputs import case_source
from cortex.capabilities.sql_training.llm.generation_rule_taxonomy import (
    operation_registry,
)
from cortex.capabilities.sql_training.training.assessment_views import (
    compact_edge,
    compact_node,
    group_context_facts,
    physical_properties,
    sql_attempts,
)
from cortex.capabilities.sql_training.training.quality_contract import (
    QUALITY_CRITERIA,
    required_quality_checks,
)
from cortex.schema.relation_contracts import authoring_schema, effective_schema
from cortex.schema.relationships import is_dependency_edge


def application_requirement(candidate, *, relation=None, owner_type=None, accepted_before=None):
    """Classify evidence needs from stored content, never Agent-written labels.

    Business structure and Concept representation bindings need semantic support.
    The Concept node still requires application of its request-relevant meaning;
    calculation operands and executable behavior require SQL evidence.
    Use the actual edge, including its managed anchor marker, rather than its
    compact prompt view to distinguish a business fact from an anchor override.
    """
    if candidate is None:
        return "none"
    if relation is not None:
        if owner_type == "concept" and relation.edge_type == "grounded_on":
            return "semantic"
        return "sql" if is_dependency_edge(relation) else "semantic"
    kind = candidate.get("node_type")
    if kind == "concept" and accepted_before is not None:
        def executable_content(value):
            from copy import deepcopy
            value = deepcopy(value)
            value.pop("sources", None)
            value.get("properties", {}).pop("relation_metadata", None)
            return value
        if (candidate.get("properties", {}).get("relation_metadata")
                != accepted_before.get("properties", {}).get("relation_metadata")
                and executable_content(candidate) == executable_content(accepted_before)):
            return "semantic"
    if kind == "event":
        return "semantic"
    return "sql"


def assessment_payload(*, record, schema, baseline, accepted, trial, groups, targets, validation, prior_evidence=None):
    writable_relations = authoring_schema(schema, trial.graph).edge_type_index()
    schema = effective_schema(schema, trial.graph)
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
    # Follow the affected semantic component and same-binding candidates. Shared
    # targets are evidence to compare, never an assertion of equivalence or a
    # reason to synthesize grounding edges. Do not fan out through physical JOINs.
    # Physical endpoints are included, but do not expand into the whole catalog.
    while True:
        previous = set(selected)
        for snapshot in (baseline, accepted, trial):
            for node in snapshot.graph.nodes:
                if node.id in selected:
                    selected.update(ref for group in node.properties.get("targets", []) for ref in group)
                    selected.update(node.properties.get("expression", {}).get("refs", {}).values())
            concepts = [node for node in snapshot.graph.nodes if node.node_type == "concept"]
            matching_inputs = {
                ref for node in concepts if node.id in selected
                for group in node.properties.get("targets", []) for ref in group
            }
            selected.update(matching_inputs)
            for node in concepts:
                if any(ref in matching_inputs for group in node.properties.get("targets", []) for ref in group):
                    selected.add(node.id)
            expandable = {a.id for a in snapshot.editable_assets} & selected
            for edge in snapshot.graph.edges:
                if edge.from_node_id in expandable:
                    selected.add(edge.to_node_id)
                if edge.to_node_id in expandable:
                    selected.add(edge.from_node_id)
                if edge.edge_type == "grounded_on" and edge.to_node_id in matching_inputs:
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
    used_relation_types = set(relation_types)
    relation_types.update(r.extends for r in schema.edge_types if r.id in used_relation_types and r.extends)
    same_baseline = baseline.revision == accepted.revision
    current_nodes = {a["id"]: a for a in nodes(trial, selected)}
    previous_nodes = {a["id"]: a for a in nodes(accepted, selected)}
    current_items = {**current_nodes, **{e["id"]: e for e in current_edges}}
    previous_items = {**previous_nodes, **{e["id"]: e for e in prior_edges(accepted, selected)}}
    members = sorted({m for g in groups for m in g.member_ids})
    known_targets = {t["target_id"] for t in targets}
    attempts = sql_attempts(validation)
    trial_relations = {edge.id: edge for edge in trial.graph.edges}
    owner_types = {node.id: node.node_type for node in trial.graph.nodes}
    units = []
    # Review executable semantics before lengthy relation inventories; preserve
    # the exact same units, evidence and acceptance-group membership.
    priority = {"metric": 0, "generation_rule": 1, "concept": 2, "event": 3, "dimension_value": 4}
    review_order = sorted(members, key=lambda key: (
        priority.get((current_items.get(key) or previous_items.get(key) or {}).get("node_type"), 5), key
    ))
    for member in review_order:
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
            "quality_requirements": required_quality_checks(
                candidate, relation=trial_relations.get(member)
            ),
            "change_kind": "delete" if candidate is None else "add" if before is None else "modify",
            "application_requirement": application_requirement(
                candidate, relation=trial_relations.get(member),
                accepted_before=before,
                owner_type=owner_types.get(trial_relations[member].from_node_id) if member in trial_relations else None,
            ),
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
        "fixed_targets": targets,
        "attempts": attempts,
        "units": units,
        "quality_rubric": {
            key: QUALITY_CRITERIA[key]
            for key in sorted({key for unit in units for key in unit["quality_requirements"]})
        },
        # One compact registry only when a reviewed or supporting Rule needs it.
        # Include all intent definitions so extensions can be checked against gaps.
        **({"operation_registry": operation_registry()} if any(
            node.get("node_type") == "generation_rule"
            for node in [*current_nodes.values(), *previous_nodes.values()]
        ) else {}),
        "physical_context_scope": "Only relevant endpoints are supplied; this is not a complete table catalog. A missing field here is not evidence that the database lacks that field or an identifier. Require evidence of the relevant scope for identity, grain and absence claims.",
        "physical_context": physical,
        "related_knowledge": {
            "scope": "Affected semantic components and shared Concept matching-input candidates; physical JOINs do not expand the catalog. Review these together with units, not as isolated assertions. This is not a claim of whole-database completeness.",
            "nodes": [a for a in current_nodes.values() if a["id"] not in members and a["node_type"] not in {"table", "column"}],
            # Changed relations already occur in units.candidate/accepted_before.
            # Keep one complete copy rather than exhausting the evidence budget.
            "edges": [e for e in current_edges if e["id"] not in members],
            "prior_edges": [e for e in old_edges if e["id"] not in members],
            "relation_definitions": [
                {"type": r.id, "compatibility_only": r.id not in writable_relations, **r.model_dump(mode="json", exclude={"id", "name", "fields"}, exclude_none=True)}
                for r in schema.edge_types if r.id in relation_types
            ],
        },
        "round_baseline": None if same_baseline else {"revision": baseline.revision, "nodes": nodes(baseline, round_changed), "edges": baseline_edges},
        "change_groups": [g.model_dump(mode="json") for g in groups],
        "prior_evidence": prior_evidence or [],
    }
