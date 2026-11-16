"""Partial edits compiled into the existing validated local transaction."""

from pydantic import Field, model_validator

from cortex.capabilities.graph_merge.integration.batch import (
    BatchResult,
    ConflictWrite,
    EdgeMerge,
)
from cortex.capabilities.graph_merge.integration.context import PHYSICAL
from cortex.capabilities.graph_merge.integration.models import Change, Element, Model
from cortex.schema import NodeContent, ConflictPoint
from cortex.schema.relation_metadata import validate_metadata_items


class RegionIncomplete(RuntimeError):
    """A terminal writer result, never a malformed patch to retry."""


class EvidenceAlternative(Model):
    alternative_id: str
    content: NodeContent
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class EvidenceConflict(Model):
    node_id: str
    alternatives: tuple[EvidenceAlternative, ...] = Field(min_length=2)
    conflict_points: tuple[ConflictPoint, ...] = Field(min_length=1)


from cortex.capabilities.graph_merge.integration.evidence_refs import evidence_sources
from cortex.capabilities.graph_merge.integration.conflict_resolution import ConflictResolution


class NodeUpdate(Model):
    id: str
    name: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    tags: list[str] | None = None
    add_evidence: tuple[str, ...] = Field(default=(), description="Only known evidence IDs genuinely supporting this retained node, including relevant evidence from an explicitly merged/replaced node; existing exact sources are preserved.")
    properties: dict = Field(default_factory=dict, description="Only changed property keys; each supplied property's value replaces that key, all other keys are preserved.")
    explanation: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def legacy_metadata(cls, value):
        if isinstance(value, dict) and "relation_metadata" in value:
            value = dict(value)
            props = dict(value.get("properties", {}))
            if "relation_metadata" in props:
                raise ValueError("Supply properties.relation_metadata once")
            meta = value.pop("relation_metadata")
            if meta is not None:
                props["relation_metadata"] = meta
            value["properties"] = props
        return value


class EdgeUpdate(Model):
    id: str
    edge_type: str | None = None
    from_node_id: str | None = None
    to_node_id: str | None = None
    explanation: str | None = None
    add_evidence: tuple[str, ...] = Field(default=(), description="Only displayed evidence IDs supporting this edge; exact sources are resolved by code.")
    properties: dict = Field(default_factory=dict, description="Changed real properties, including description and alternative_ids. Each supplied property replaces that key; omitted keys stay unchanged.")

    @model_validator(mode="before")
    @classmethod
    def legacy_paths(cls, value):
        # Read old callers without exposing obsolete aliases in model tools.
        if not isinstance(value, dict):
            return value
        value = dict(value)
        props = dict(value.get("properties", {}))
        for old, actual in (("description", "description"), ("branches", "alternative_ids")):
            if old in value:
                val = value.pop(old)
                if val is not None:
                    if actual in props and props[actual] != val:
                        raise ValueError(f"Supply properties.{actual} once")
                    props[actual] = val
        value["properties"] = props
        return value


class EvidenceWrite(Model):
    @model_validator(mode="before")
    @classmethod
    def legacy_evidence(cls, value):
        if isinstance(value, dict) and "evidence" in value:
            value = dict(value)
            if "evidence_ids" in value:
                raise ValueError("Supply evidence_ids once")
            value["evidence_ids"] = value.pop("evidence")
        return value


class NewNode(EvidenceWrite):
    id: str
    node_type: str
    name: str
    description: str
    aliases: tuple[str, ...] = ()
    properties: dict
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class NewEdge(EdgeUpdate, EvidenceWrite):
    edge_type: str
    from_node_id: str
    to_node_id: str
    properties: dict
    evidence_ids: tuple[str, ...] = Field(min_length=1)


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
    conflicts: tuple[EvidenceConflict, ...] = ()
    resolutions: tuple[ConflictResolution, ...] = ()
    relation_contracts: tuple[BusinessContract, ...] = ()
    explanation: str = Field(min_length=1)


def submit_patch(context, patch):
    if patch.failure:
        raise RegionIncomplete(patch.failure)
    batch = context.active_batch
    current = batch.preview or batch.seed
    nodes = {n.id: n for n in current.graph.nodes}
    edges = {e.id: e for e in current.graph.edges}
    edits = []
    changes = [Change.model_validate(r.model_dump()) for r in patch.retirements]
    for update in patch.node_updates:
        node = nodes.get(update.id)
        if node is None:
            raise ValueError(f"node_updates: unknown current node {update.id}; inspect the candidate view and use its current ID")
        if node.node_type in PHYSICAL:
            raise ValueError("Physical catalogue is readonly")
        raw = node.model_dump(mode="json")
        values = update.model_dump(exclude_unset=True)
        for key in ("name", "aliases", "description", "tags"):
            if key in values:
                raw[key] = values[key]
        raw["properties"] = {**raw["properties"], **update.properties}
        if set(update.add_evidence) - {e.id for e in context.evidence}:
            raise ValueError("Unknown evidence ID")
        additions = [e.source for e in context.evidence if e.id in update.add_evidence]
        raw["sources"] = [s.model_dump(mode="json") for s in dict.fromkeys((*node.sources, *additions))]
        if raw["properties"].get("relation_metadata") is not None:
            if node.node_type != "concept":
                raise ValueError("relation_metadata is a list of groups on Concept only; use edge description for Rule/Metric/Event relation scope")
            validate_metadata_items(raw["properties"]["relation_metadata"])
        edits.append({"operation": "update_node", "node": raw})
        if not any(r.kind == "node" and r.id == node.id for c in changes for r in c.before):
            changes.append(Change(before=(Element(kind="node", id=node.id),),
                                  after=(Element(kind="node", id=node.id),),
                                  explanation=update.explanation))
    for update in patch.edge_updates:
        edge = edges.get(update.id)
        if edge is None:
            raise ValueError(f"edge_updates: unknown current edge {update.id}; inspect the candidate view and use its current ID")
        if nodes[edge.from_node_id].node_type in PHYSICAL:
            raise ValueError("Physical catalogue edges are readonly")
        if {"sources", "branches", "target_id"} & update.properties.keys():
            raise ValueError("Use properties.alternative_ids for scope and from_node_id/to_node_id for endpoints; internal sources are not editable properties")
        if "relation_metadata" in update.properties:
            raise ValueError("relation_metadata belongs on a Concept node, not an edge; use edge description for single-edge meaning")
        raw = edge.model_dump(mode="json")
        for key in ("edge_type", "from_node_id", "to_node_id"):
            value = getattr(update, key)
            if value is not None:
                raw[key] = value
        raw["properties"] = {**raw["properties"], **update.properties}
        if set(update.add_evidence) - {e.id for e in context.evidence}:
            raise ValueError("Unknown evidence ID")
        additions = [e.source for e in context.evidence if e.id in update.add_evidence]
        raw["sources"] = [s.model_dump(mode="json") for s in dict.fromkeys((*edge.sources, *additions))]
        edits.append({"operation": "update_edge", "edge": raw})
    for kind, rows in (("node", patch.create_nodes), ("edge", patch.create_edges)):
        for item in rows:
            raw = item.model_dump(mode="json", exclude={"evidence_ids", "add_evidence", "explanation"})
            refs = {e.source for e in context.evidence if e.id in item.evidence_ids}
            if set(item.evidence_ids) - {e.id for e in context.evidence}:
                raise ValueError("Unknown evidence ID")
            raw["sources"] = [s.model_dump(mode="json") for s in sorted(refs, key=lambda s: str(s))]
            if kind == "edge":
                if not raw["properties"].get("description"):
                    raise ValueError("New edge requires properties.description")
            if kind == "node" and raw.get("node_type") in PHYSICAL:
                raise ValueError("Cannot create physical catalogue nodes")
            edits.append({"operation": f"create_{kind}", kind: raw})
    # The agent chooses equivalence; code preserves the scopes of that explicit
    # union. Never infer equivalence from branch or endpoint similarity alone.
    for merge in patch.edge_merges:
        members = [edges[k] for k in dict.fromkeys((merge.survivor_id, *merge.source_ids)) if k in edges]
        if not members or any(u.id == merge.survivor_id and "alternative_ids" in u.properties for u in patch.edge_updates):
            continue
        if len({(e.from_node_id, e.edge_type, e.to_node_id) for e in members}) != 1:
            if any(e.properties.get("alternative_ids") for e in members):
                raise ValueError("A branch-scoped merge across changed endpoints needs explicit properties.alternative_ids on its survivor")
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
        if obj is None:
            raise ValueError(f"retirements.before: unknown current {ref.kind} {ref.id}; inspect the candidate view and use its current ID")
        owner = obj if ref.kind == "node" else nodes[obj.from_node_id]
        if owner.node_type in PHYSICAL:
            raise ValueError("Cannot delete physical catalogue objects")
        edits.append({"operation": f"delete_{ref.kind}", f"{ref.kind}_id": ref.id})
    result = BatchResult.model_validate({
        "edits": edits, "changes": [c.model_dump(mode="json") for c in changes],
        "edge_merges": patch.edge_merges, "resolutions": patch.resolutions,
        "conflicts": [ConflictWrite.model_validate({
            "node_id": conflict.node_id,
            "alternatives": [{"alternative_id": a.alternative_id,
                "content": a.content.model_dump(mode="json"),
                "sources": [s.model_dump(mode="json") for s in evidence_sources(context, a.evidence_ids)]}
                for a in conflict.alternatives],
            "conflict_points": [p.model_dump(mode="json") for p in conflict.conflict_points],
        }) for conflict in patch.conflicts],
        "relation_contracts": [c.model_dump(mode="json", exclude_none=True) for c in patch.relation_contracts],
    })
    return batch.submit_atomic(result)
