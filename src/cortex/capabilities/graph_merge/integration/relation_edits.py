"""Partial edits compiled into the existing validated local transaction."""
from copy import deepcopy

from pydantic import Field

from cortex.capabilities.graph_merge.integration.batch import (
    BatchResult,
    ConflictWrite,
    EdgeMerge,
)
from cortex.capabilities.graph_merge.integration.context import PHYSICAL
from cortex.capabilities.graph_merge.integration.models import Change, Element, Model
from cortex.schema.relation_metadata import RelationMetadata, validate_metadata_items


class NodeUpdate(Model):
    id: str
    name: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    tags: list[str] | None = None
    relation_metadata: tuple[RelationMetadata, ...] | None = Field(default=None, description="Concept only: complete replacement list of compatible, same-predicate adjacent edge groups. Not supported on Rule/Metric/Event or edges; never group mutually exclusive branches.")
    properties: dict = Field(default_factory=dict, description="Only changed property keys; each supplied property's value replaces that key, all other keys are preserved.")
    explanation: str = Field(min_length=1)


class EdgeUpdate(Model):
    id: str
    edge_type: str | None = None
    from_node_id: str | None = None
    to_node_id: str | None = None
    description: str | None = None
    explanation: str | None = None
    branches: tuple[str, ...] | None = Field(default=None, description="Optional complete branch scope using existing alternatives of the source node. Missing preserves scope; empty explicitly removes restriction. Do not put this in properties.")
    add_evidence: tuple[str, ...] = Field(default=(), description="Only evidence IDs genuinely supporting this successor; code preserves existing exact sources.")
    properties: dict = Field(default_factory=dict, description="Only changed additional property keys. Do not overwrite sources or controller bookkeeping.")


class NewNode(Model):
    id: str
    node_type: str
    name: str
    description: str
    aliases: tuple[str, ...] = ()
    properties: dict
    evidence: tuple[str, ...] = Field(min_length=1)


class NewEdge(Model):
    id: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    description: str
    evidence: tuple[str, ...] = Field(min_length=1)
    branches: tuple[str, ...] | None = Field(default=None, description="Existing alternative IDs of the source node, when this assertion is branch-specific.")


class Retirement(Model):
    before: tuple[Element, ...] = Field(min_length=1, description="Current assertions being replaced. Code deletes before objects absent from after.")
    after: tuple[Element, ...] = Field(default=(), description="Actual successor assertions/path. Empty only for an explained erroneous assertion discard.")
    explanation: str = Field(min_length=1)


class BusinessContract(Model):
    id: str
    name: str
    description: str
    from_node_types: tuple[str, ...]
    to_node_types: tuple[str, ...]
    direction_semantics: str
    evidence_requirement: str
    extends: str | None = None


class RelationPatch(Model):
    failure: str | None = Field(default=None, description="Only if a complete safe regional decision is impossible from available information. Do not disguise an unresolved required repair as an empty success.")
    node_updates: tuple[NodeUpdate, ...] = ()
    edge_updates: tuple[EdgeUpdate, ...] = ()
    create_nodes: tuple[NewNode, ...] = ()
    create_edges: tuple[NewEdge, ...] = ()
    retirements: tuple[Retirement, ...] = ()
    edge_merges: tuple[EdgeMerge, ...] = ()
    conflicts: tuple[ConflictWrite, ...] = ()
    relation_contracts: tuple[BusinessContract, ...] = ()
    explanation: str = Field(min_length=1)


def submit_patch(context, patch):
    if patch.failure:
        raise ValueError("Region cannot be completed: " + patch.failure)
    batch = context.active_batch
    current = batch.preview or batch.seed
    nodes = {n.id: n for n in current.graph.nodes}
    edges = {e.id: e for e in current.graph.edges}
    edits = []
    changes = [Change.model_validate(r.model_dump()) for r in patch.retirements]
    for update in patch.node_updates:
        node = nodes[update.id]
        if node.node_type in PHYSICAL:
            raise ValueError("Physical catalogue is readonly")
        raw = node.model_dump(mode="json")
        values = update.model_dump(exclude_unset=True)
        for key in ("name", "aliases", "description", "tags"):
            if key in values:
                raw[key] = values[key]
        raw["properties"] = {**raw["properties"], **update.properties}
        if update.relation_metadata is not None:
            if "relation_metadata" in update.properties:
                raise ValueError("Supply relation_metadata once, using the typed node-update field")
            raw["properties"]["relation_metadata"] = [g.model_dump(mode="json") for g in update.relation_metadata]
        if "relation_metadata" in raw["properties"]:
            if node.node_type != "concept":
                raise ValueError("relation_metadata is a list of groups on Concept only; use edge description for Rule/Metric/Event relation scope")
            validate_metadata_items(raw["properties"]["relation_metadata"])
        edits.append({"operation": "update_node", "node": raw})
        if not any(r.kind == "node" and r.id == node.id for c in changes for r in c.before):
            changes.append(Change(before=(Element(kind="node", id=node.id),),
                                  after=(Element(kind="node", id=node.id),),
                                  explanation=update.explanation))
    for update in patch.edge_updates:
        edge = edges[update.id]
        if nodes[edge.from_node_id].node_type in PHYSICAL:
            raise ValueError("Physical catalogue edges are readonly")
        if {"sources", "alternative_ids", "branches", "target_id"} & update.properties.keys():
            raise ValueError("Use the typed branches field for scope; do not author source or branch/target bookkeeping in properties")
        if "relation_metadata" in update.properties:
            raise ValueError("relation_metadata belongs on a Concept node, not an edge; use edge description for single-edge meaning")
        raw = edge.model_dump(mode="json")
        for key in ("edge_type", "from_node_id", "to_node_id"):
            value = getattr(update, key)
            if value is not None:
                raw[key] = value
        raw["properties"] = {**raw["properties"], **update.properties}
        if update.branches is not None:
            if update.branches:
                raw["properties"]["alternative_ids"] = list(update.branches)
            else:
                raw["properties"].pop("alternative_ids", None)
        if set(update.add_evidence) - {e.id for e in context.evidence}:
            raise ValueError("Unknown evidence ID")
        additions = [e.source for e in context.evidence if e.id in update.add_evidence]
        raw["sources"] = [s.model_dump(mode="json") for s in dict.fromkeys((*edge.sources, *additions))]
        if update.description is not None:
            raw["properties"]["description"] = update.description
        edits.append({"operation": "update_edge", "edge": raw})
    for kind, rows in (("node", patch.create_nodes), ("edge", patch.create_edges)):
        for item in rows:
            raw = item.model_dump(mode="json", exclude={"evidence"})
            refs = {e.source for e in context.evidence if e.id in item.evidence}
            if set(item.evidence) - {e.id for e in context.evidence}:
                raise ValueError("Unknown evidence ID")
            raw["sources"] = [s.model_dump(mode="json") for s in sorted(refs, key=lambda s: str(s))]
            if kind == "edge":
                raw["properties"] = {"description": raw.pop("description")}
                branches = raw.pop("branches", None)
                if branches:
                    raw["properties"]["alternative_ids"] = branches
            if kind == "node" and raw.get("node_type") in PHYSICAL:
                raise ValueError("Cannot create physical catalogue nodes")
            edits.append({"operation": f"create_{kind}", kind: raw})
    # The agent chooses equivalence; code preserves the scopes of that explicit
    # union. Never infer equivalence from branch or endpoint similarity alone.
    for merge in patch.edge_merges:
        members = [edges[k] for k in dict.fromkeys((merge.survivor_id, *merge.source_ids)) if k in edges]
        if not members or any(u.id == merge.survivor_id and u.branches is not None for u in patch.edge_updates):
            continue
        if len({(e.from_node_id, e.edge_type, e.to_node_id) for e in members}) != 1:
            if any(e.properties.get("alternative_ids") for e in members):
                raise ValueError("A branch-scoped merge across changed endpoints needs explicit typed branches on its survivor")
            continue
        scopes = [e.properties.get("alternative_ids", []) for e in members]
        if not any(scopes):
            continue
        update = next((e for e in edits if e.get("edge", {}).get("id") == merge.survivor_id), None)
        if update is None:
            update = {"operation": "update_edge", "edge": edges[merge.survivor_id].model_dump(mode="json")}
            edits.append(update)
        props = update["edge"]["properties"]
        if all(scopes):
            props["alternative_ids"] = sorted({s for scope in scopes for s in scope})
        else:
            props.pop("alternative_ids", None)
    # A named one-edge successor is an explicit attribution choice by the
    # writer, not inference from a rewrite group's proximity. Copy exact refs
    # to that one destination only; many-to-many rewrites still need attribution.
    for retirement in patch.retirements:
        if (len(retirement.before) == len(retirement.after) == 1
                and retirement.before[0].kind == retirement.after[0].kind == "edge"):
            source, target = retirement.before[0].id, retirement.after[0].id
            if source not in edges or target not in edges:
                continue
            update = next((e for e in edits if e.get("edge", {}).get("id") == target), None)
            if update is None:
                update = {"operation": "update_edge", "edge": edges[target].model_dump(mode="json")}
                edits.append(update)
            present = update["edge"]["sources"]
            for ref in edges[source].sources:
                value = ref.model_dump(mode="json")
                if value not in present:
                    present.append(value)
    removed = {ref for r in patch.retirements for ref in r.before if ref not in r.after}
    for ref in removed:
        if ref.alternative_id:
            raise ValueError("Use conflicts to change complete alternatives")
        obj = nodes.get(ref.id) if ref.kind == "node" else edges.get(ref.id)
        owner = obj if ref.kind == "node" else nodes[obj.from_node_id]
        if owner.node_type in PHYSICAL:
            raise ValueError("Cannot delete physical catalogue objects")
        edits.append({"operation": f"delete_{ref.kind}", f"{ref.kind}_id": ref.id})
    result = BatchResult.model_validate({
        "edits": edits, "changes": [c.model_dump(mode="json") for c in changes],
        "edge_merges": patch.edge_merges, "conflicts": patch.conflicts,
        "relation_contracts": [c.model_dump(mode="json", exclude_none=True) for c in patch.relation_contracts],
    })
    # A malformed partial proposal cannot poison saved pending state.
    saved = {k: deepcopy(getattr(batch, k)) for k in
             ("pending", "changes", "contracts", "edge_merges")}
    saved.update({k: getattr(batch, k) for k in ("preview", "result", "revision")})
    candidate = context.candidate
    try:
        return batch.submit(result)
    except (ValueError, TypeError, KeyError):
        for key, value in saved.items():
            setattr(batch, key, value)
        context.candidate = candidate
        raise
