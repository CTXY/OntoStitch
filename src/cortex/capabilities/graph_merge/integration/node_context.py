"""Shared complete-focus, light-neighbour transport for node writers and reviews."""

def compact_task_context(payload, context, candidate, ids, groups, *, for_review=True):
    payload["review_scope" if for_review else "revision_scope"] = "Only reviewed IDs receive verdicts. Other content is dependency context; omitted fields are readable, not absent. All slices use the same immutable candidate; code requires every slice."
    # Full focus/source members and formula operands stay intact. A group
    # neighbour is context, not a request to revalidate all its bindings.
    full = set(ids) | {member for g in groups for member in g["members"]}
    full.update(ref["id"] for row in payload.get("origin_links", []) for ref in row["source_refs"] if ref["kind"] == "node")
    from cortex.capabilities.graph_merge.integration.context import references
    for n in candidate.graph.nodes:
        if n.id in ids:
            full.update(references(n))
    for field in ("candidate_nodes", "nodes"):
        payload[field] = [row if row["id"] in full else
            {k: v for k, v in row.items() if k in {"id", "node_type", "name", "description", "aliases", "alternatives", "conflict_points", "sources"}} |
            {"properties": {k: v for k, v in row.get("properties", {}).items() if k in {"definition", "action", "condition"}}}
            for row in payload[field]]
        for row in payload[field]:
            if row["id"] not in full and row.get("description") == row.get("properties", {}).get("definition"):
                row.pop("description", None)
    current_ids = {node.id for node in candidate.graph.nodes}
    if "matched_groups" in payload:
        payload["matched_groups"] = [dict(group) for group in payload["matched_groups"]]
    for group in payload.get("matched_groups", []):
        group.pop("current_owner", None)
        present = set(group["members"]) & current_ids
        # Report a concrete current member, never infer a new/split successor.
        if len(present) == 1:
            group["current_owner"] = next(iter(present))
    current = {row["id"]: row for row in payload.get("candidate_nodes", [])}
    for row in payload.get("consumer_directory", []):
        if row.get("description") == current.get(row["id"], {}).get("description"):
            row.pop("description", None)
    for row in payload.get("physical_facts", []):
        row["properties"] = {k: v for k, v in row["properties"].items() if k not in {"source_metadata", "sample_values"}}
        for key in ("aliases", "tags", "description"):
            if not row.get(key):
                row.pop(key, None)
        row.pop("merge_status", None)
    guides = payload.get("node_guidance", {})
    if guides:
        from importlib.resources import files
        guide = files("cortex.capabilities.graph_merge.llm.prompts").joinpath("incremental_content_shared.md").read_text()
        payload["node_guidance"] = {"types": sorted(guides), "guidance": guide}
        if not for_review:
            payload["node_guidance"]["fields_by_type"] = {k: v["fields"] for k, v in guides.items() if "fields" in v}
    # Accepted source Rules are not re-audited during integration.
    for key in ("rationale_review_ids", "decision_review_ids", "rationale_scope"):
        payload.pop(key, None)
    scope = payload["task_scope"]
    # These describe the uncompressed controller packet, not the supplied view.
    # Keeping them invites a reviewer to fetch omitted neighbours to "complete"
    # an already complete matched-group review.
    for key in ("read_only_context", "original_characters", "soft_character_budget", "budget_exceeded_by_required_units"):
        scope.pop(key, None)
    for deferred in scope.get("deferred_context", []):
        deferred.pop("nodes", None)
        deferred["note"] = (
            "Optional context, not a reading checklist. Use next_query only when a specific decision lacks a premise; "
            "complete=false concerns the wider view, while required_units_complete describes the supplied task members."
        )
    payload.pop("neighbor_directory", None)
    payload.pop("evidence_directory", None)
    # Exact SourceReference objects stay in controller state. Reviewers cite
    # evidence IDs, as the writer does; no locator reconstruction is needed.
    from cortex.capabilities.graph_merge.integration.evidence_refs import compact_attribution
    for key in ("nodes", "candidate_nodes", "relations", "candidate_relations", "physical_facts", "deleted_nodes", "deleted_relations"):
        payload[key] = compact_attribution(payload.get(key, []), context)
    from cortex.capabilities.graph_merge.integration.evidence_refs import EvidencePresentation
    evidence_rows = EvidencePresentation(context).read([e["id"] for e in payload.pop("evidence", [])])["items"]
    if not for_review:
        payload["revision_scope"] = "Repair the supplied defects and necessary consequences. Complete focus/source members are provided; other nodes are context with on-demand full reads. Code retains all other matched-group aliases and pending edits; omitted content was not deleted."
    from cortex.capabilities.graph_merge.integration.edit_feedback import current_repair_fields
    payload["current_repair_fields"] = current_repair_fields(candidate, payload.get("feedback", []))
    payload["evidence"] = evidence_rows
    return payload

def revision_task(task, findings, aliases):
    """Use explicit reviewer references, never NL heuristics, to focus a repair."""
    from cortex.capabilities.graph_merge.integration.task_context import LocalTask
    refs = {row['id'] for row in findings.get('issue_refs', [])}
    if not refs:
        return task
    groups = [g for g in task.groups if any(aliases.get(k, k) in refs or k in refs for k in g['members'])]
    return LocalTask(task.id, task.phase, refs, groups=groups, checks=task.checks, feedback=task.feedback)
