"""Atomic reconciliation proposals and mechanical dependency grouping."""

from pydantic import Field

from cortex.capabilities.graph_merge.integration.batch import BatchResult
from cortex.capabilities.graph_merge.integration.models import Model
from cortex.capabilities.graph_merge.integration.task_context import (
    _indexes,
    _metadata_edges,
)


class RepairUnit(Model):
    unit_id: str = Field(min_length=1)
    checked_node_ids: tuple[str, ...] = ()
    patch: BatchResult


class ReconciliationBatch(Model):
    units: tuple[RepairUnit, ...] = Field(min_length=1)
    coupled_units: tuple[tuple[str, ...], ...] = ()
    failure: str | None = None


def _combine(units):
    """Combine dependent complete patches; conflicting writes are rejected."""
    fields = BatchResult.model_fields
    data = {k: [] for k in fields if k != "failure"}
    for unit in units:
        patch = unit.patch
        if patch.failure:
            raise ValueError(patch.failure)
        if any(getattr(patch, k) for k in fields if k.startswith("remove_")):
            raise ValueError(
                "Return complete replacement units; remove_* is not used in reconciliation batches"
            )
        for key, values in data.items():
            for value in getattr(patch, key):
                if value not in values:
                    values.append(value)
    edits = {}
    for edit in data["edits"]:
        raw = edit.model_dump(mode="json")
        kind = "node" if "node" in raw["operation"] else "edge"
        key = (kind, raw.get(kind, {}).get("id") or raw.get(kind + "_id"))
        if key in edits and edits[key] != raw:
            raise ValueError(
                f"Conflicting writes for {key}; return one complete repair"
            )
        edits[key] = raw
    return BatchResult.model_validate(data)


def _footprint(unit, state):
    """Mechanical dependencies, including deletions, contracts and dispositions."""
    _nodes, edges = _indexes(state)
    writes, reads = set(), set()
    for edit in unit.patch.edits:
        raw = edit.model_dump(mode="json")
        kind = "node" if "node" in raw["operation"] else "edge"
        obj = raw.get(kind)
        key = raw.get(kind + "_id") if obj is None else obj["id"]
        writes.add((kind, key))
        if kind == "edge":
            for edge in (
                obj,
                edges[key].model_dump(mode="json") if key in edges else None,
            ):
                if edge:
                    reads.update(
                        ("node", edge[k]) for k in ("from_node_id", "to_node_id")
                    )
                    reads.add(("contract", edge["edge_type"]))
        elif obj:
            from cortex.capabilities.graph_merge.integration.context import references
            from cortex.schema import GraphNode

            node = GraphNode.model_validate(obj)
            reads.update(("node", k) for k in references(node))
            reads.update(("edge", k) for k in _metadata_edges(node))
    for conflict in unit.patch.conflicts:
        writes.add(("node", conflict.node_id))
        from cortex.capabilities.graph_merge.integration.context import references
        from cortex.schema import GraphNode

        for alternative in conflict.alternatives:
            if conflict.node_id in _nodes:
                resolved = _nodes[conflict.node_id].model_copy(
                    update=alternative.content.model_dump()
                )
                reads.update(("node", key) for key in references(resolved))
    for contract in unit.patch.relation_contracts:
        writes.add(("contract", contract.id))
        if contract.extends:
            reads.add(("contract", contract.extends))
    for change in unit.patch.changes:
        writes.update(("origin:" + ref.kind, ref.id) for ref in change.before)
        reads.update((ref.kind, ref.id) for ref in change.after)
    for merge in unit.patch.edge_merges:
        writes.update(("edge", k) for k in (*merge.source_ids, merge.survivor_id))
    # A Rule's unchanged applies_to edge can carry a semantic dependency on a
    # simultaneously rewritten operand; property refs alone would miss it.
    changed_nodes = {key for kind, key in writes if kind == "node"}
    for e in edges.values():
        if e.from_node_id in changed_nodes or e.to_node_id in changed_nodes:
            reads.add(("edge", e.id))
            reads.update(("node", k) for k in (e.from_node_id, e.to_node_id))
    for key in unit.checked_node_ids:
        reads.add(("node", key))
    return writes, reads


def group_units(units, state, coupled=()):
    """Undirected grouping only; no speculative order between candidates."""
    groups = [{u.unit_id} for u in units]
    if len(groups) != len({u.unit_id for u in units}):
        raise ValueError("Duplicate unit_id")
    footprints = {u.unit_id: _footprint(u, state) for u in units}
    links = list(coupled)
    for i, left in enumerate(units):
        w, r = footprints[left.unit_id]
        for right in units[i + 1 :]:
            w2, r2 = footprints[right.unit_id]
            if w & (w2 | r2) or w2 & r:
                links.append((left.unit_id, right.unit_id))
    for link in links:
        if not set(link) <= footprints.keys():
            raise ValueError("Unknown coupled unit")
        touched = [g for g in groups if g & set(link)]
        groups = [g for g in groups if not g & set(link)]
        groups.append(set().union(*touched))
    result = []
    for keys in groups:
        selected = [u for u in units if u.unit_id in keys]
        result.append(
            RepairUnit(
                unit_id=selected[0].unit_id,
                checked_node_ids=tuple(
                    sorted({k for u in selected for k in u.checked_node_ids})
                ),
                patch=_combine(selected),
            )
        )
    return result
