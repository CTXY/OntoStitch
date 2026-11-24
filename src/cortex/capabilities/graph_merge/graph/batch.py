"""One small patch against a stable current graph; no source-relative replay."""

from copy import deepcopy
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.operations import deduplicate_edges
from cortex.capabilities.graph_merge.graph.operations import PHYSICAL, remap_edge, remap_node
from cortex.capabilities.graph_merge.graph.edit_contract import Change, CompleteAlternative, EdgeDelete, EdgeWrite, Element, InspectionCheck, NodeDelete, RationaleCheck
from cortex.capabilities.graph_merge.models import Model
from cortex.capabilities.graph_merge.graph.edit_validation import (
    content_errors,
    edit_key,
)
from cortex.schema import ConflictPoint, GraphEdge, GraphNode, GraphSpace, GraphState, SourceReference
from cortex.schema.models import EdgeType
from cortex.schema.relation_contracts import merge_relation_contracts
from cortex.schema.validation import validate_graph_state
from cortex.capabilities.graph_merge.graph.conflicts import ConflictResolution, resolution_edits
from cortex.capabilities.graph_merge.evidence import evidence_sources


class ConflictWrite(Model):
    node_id: str
    alternatives: tuple[CompleteAlternative, ...] = Field(min_length=2)
    conflict_points: tuple[ConflictPoint, ...] = Field(min_length=1)
    common_evidence_ids: tuple[str, ...] = ()


class EdgeMerge(Model):
    """The Agent declares the same fact; code preserves exact references."""

    source_ids: tuple[str, ...] = Field(min_length=1, description="Graph EDGE IDs to absorb into the survivor, not evidence IDs or SourceReference source_ids. Code preserves their exact provenance.")
    survivor_id: str = Field(description="ID of the existing edge to retain. Change its description/properties separately in edge_updates using id; this merge object has no properties field.")
    explanation: str = Field(min_length=1, description="Why these edges express the same fact in compatible scope, with material restrictions preserved.")


class NodeWrite(Model):
    operation: Literal["create_node", "update_node"]
    node: dict = Field(description="Create: complete node. Update: id plus changed fields; unmentioned fields stay unchanged. properties merges by key; each supplied property value replaces its old value completely. Never put an edge here.")
    evidence_ids: tuple[str, ...] = Field(default=(), description="Evidence supporting a new object or additional support; sibling of node, never inside node. Updates retain existing exact sources.")


class LocalEdgeWrite(EdgeWrite):
    evidence_ids: tuple[str, ...] = Field(default=(), description="Evidence supporting a new edge or additional support; sibling of edge. Updates retain existing exact sources.")


class BatchResult(Model):
    """Submit only this task's patch, not the accumulated graph or history."""

    edits: tuple[NodeWrite | LocalEdgeWrite | NodeDelete | EdgeDelete, ...] = ()
    changes: tuple[Change, ...] = Field(
        default=(),
        description="Provenance correspondences only; does NOT execute graph changes. Retire current copies with explicit delete_node/delete_edge edits as well as their before/after correspondence.",
    )
    conflicts: tuple[ConflictWrite, ...] = ()
    resolutions: tuple[ConflictResolution, ...] = ()
    edge_merges: tuple[EdgeMerge, ...] = ()
    relation_contracts: tuple[EdgeType, ...] = Field(
        default=(),
        description="NEW predicate IDs only. Reuse installed predicates by edge_type; do not resend or restyle installed definitions.",
    )
    inspection_checks: tuple[InspectionCheck, ...] = ()
    rationale_checks: SkipJsonSchema[tuple[RationaleCheck, ...]] = ()
    remove_edits: tuple[Element, ...] = ()
    remove_edge_merges: tuple[str, ...] = ()
    remove_changes: tuple[Element, ...] = ()
    remove_contracts: tuple[str, ...] = ()
    failure: str | None = Field(default=None, description="Omit or use JSON null on success (including no changes). On failure, give a concrete reason; never the string 'null'.")

    @field_validator("failure")
    @classmethod
    def concrete_failure(cls, value):
        if value is not None and value.strip().lower() in {"", "null", "none", "nil", "undefined"}:
            raise ValueError("failure must be omitted or JSON null for success, including an empty patch. A real failure requires a concrete explanation. Correct this field; no edits have been applied.")
        return value

    @model_validator(mode="after")
    def object_kinds(self):
        for index, edit in enumerate(self.edits):
            body = edit.node if isinstance(edit, NodeWrite) else edit.edge if isinstance(edit, EdgeWrite) else {}
            if "evidence_ids" in body:
                raise ValueError(f"Move edits[{index}] evidence_ids outside node/edge to its sibling edits[{index}].evidence_ids. It is attribution, not GraphNode/GraphEdge content.")
            if isinstance(edit, NodeWrite) and {"edge_type", "from_node_id", "to_node_id"} <= edit.node.keys():
                operation = "create_edge" if edit.operation == "create_node" else "update_edge"
                raise ValueError(f"edits[{index}].node contains an EDGE, not a node. Use operation='{operation}' and edge={{...the same edge content...}}, not node. Do not add node_type/name to this edge.")
            if isinstance(edit, EdgeWrite) and "node_type" in edit.edge:
                raise ValueError(f"edits[{index}].edge contains a NODE. Use create_node/update_node with node={{...}}, not edge.")
        return self


def delta(before, after):
    result = {}
    for kind in ("nodes", "edges"):
        a = {x.id: x for x in getattr(before.graph, kind)}
        b = {x.id: x for x in getattr(after.graph, kind)}
        result[kind] = {
            "created": sorted(b.keys() - a.keys()),
            "deleted": sorted(a.keys() - b.keys()),
            "updated": sorted(k for k in a.keys() & b.keys() if a[k] != b[k]),
        }
    return result


def alias_view(state, aliases):
    """Only declared identity aliases; never infer business equivalence."""
    nodes = {n.id: n for n in state.graph.nodes}
    merged = {}
    for n in nodes.values():
        key = aliases.get(n.id, n.id)
        if key != n.id and (n.alternatives or nodes[key].alternatives):
            raise ValueError('Conflict identity projection requires a complete integrate_groups result; source attribution cannot be inferred')
        if key not in merged:
            merged[key] = remap_node(nodes[key], aliases)
        first = merged[key]
        merged[key] = first.model_copy(
            update={
                "sources": first.sources
                if first.alternatives
                else tuple(dict.fromkeys((*first.sources, *n.sources))),
                "aliases": first.aliases
                if any("aliases" in a.overrides for a in first.alternatives)
                else list(
                    dict.fromkeys(
                        [
                            *first.aliases,
                            *n.aliases,
                            *([n.name] if n.name != first.name else []),
                        ]
                    )
                ),
            }
        )
    edges, edge_map = deduplicate_edges(
        remap_edge(e, aliases) for e in state.graph.edges
    )
    merged = {k: remap_node(n, {}, edge_map) for k, n in merged.items()}
    return GraphState(
        graph=GraphSpace(
            nodes=tuple(merged.values()),
            edges=tuple(edges.values()),
            relation_contracts=state.graph.relation_contracts,
        )
    ), edge_map


class ActiveBatch:
    """Pending malformed objects never replace the last valid draft or accepted graph."""

    def __init__(self, context, base, *, targets, aliases=None):
        self.context, self.base = context, base
        self.aliases = aliases or {}
        self.seed, self.edge_aliases = alias_view(base, self.aliases)
        self.targets = set(targets)
        self.allowed = self.targets | {
            e.id
            for e in self.seed.graph.edges
            if e.from_node_id in self.targets or e.to_node_id in self.targets
        }
        self.pending = {}
        self.changes = []
        self.contracts = {}
        self.edge_merges = {}
        self.preview = None
        self.result = None
        self.revision = 0
        self.extra_check = None
        self.prepare_submission = None
        self.scope_events = []
        # Only compiler-proved alias-created self-relations may be retained in
        # an unpublished node candidate for independent Assessment feedback.
        self.node_structural_diagnostics = []
        self.node_original_group = None

    def submit_atomic(self, result):
        """Keep the last valid draft when a new structured submission is invalid."""
        fields = ("pending", "changes", "contracts", "edge_merges", "allowed", "targets",
                  "node_structural_diagnostics")
        saved = {k: deepcopy(getattr(self, k)) for k in fields}
        saved.update({k: getattr(self, k) for k in ("preview", "result", "revision")})
        candidate = self.context.candidate
        try:
            added = self.prepare_submission(result) if self.prepare_submission else set()
            self.allowed.update(added)
            self.targets.update(added)
            value = self.submit(result)
            if added:
                self.scope_events.append({"ids": sorted(added), "reason": "Explicit coupled patch objects"})
            return value
        except (ValueError, TypeError, KeyError):
            for key, value in saved.items():
                setattr(self, key, value)
            self.context.candidate = candidate
            raise

    def submit(self, result):
        if result.failure:
            raise ValueError(result.failure)
        data = result.model_dump(mode="json")
        if {r.node_id for r in result.resolutions} & {c.node_id for c in result.conflicts}:
            raise ValueError("Use either resolutions or conflicts for the same owner, not both")
        if result.resolutions:
            extra_edits, extra_changes = resolution_edits(self.preview or self.seed, result.resolutions)
            if {e["node"]["id"] for e in extra_edits if e["operation"] == "update_node"} & {e.get("node", {}).get("id") for e in data["edits"]}:
                raise ValueError("Resolve a conflict once; do not separately update its owner in the same proposal")
            data["edits"].extend(extra_edits)
            data["changes"].extend(extra_changes)
        stale_deletes = {}
        for edit in data["edits"]:
            if edit["operation"].startswith("delete_"):
                kind, key = edit_key(edit)
                mapping = self.aliases if kind == "node" else self.edge_aliases
                if mapping.get(key, key) != key:
                    stale_deletes[key] = mapping[key]
        if stale_deletes:
            raise ValueError(
                f"These source IDs are already collapsed in the candidate: {stale_deletes}. "
                "Do not delete them: that would delete their live canonical objects. "
                "Identity projection and its lineage are already handled by code. "
                "Withdraw any pending edits for these source IDs with remove_edits, "
                "and withdraw their redundant source-only changes with remove_changes. "
                "Update only the current canonical content. No edits from this submission were saved."
            )
        for ref in result.remove_edits:
            self.pending.pop((ref.kind, ref.id), None)
        for key in result.remove_edge_merges:
            self.edge_merges.pop(key, None)
        removed_refs = {(r.kind, r.id, r.alternative_id) for r in result.remove_changes}
        kept_changes = []
        for c in self.changes:
            old_refs = {
                (e["kind"], e["id"], e.get("alternative_id")) for e in c["before"]
            }
            if old_refs & removed_refs and not old_refs <= removed_refs:
                raise ValueError(
                    "Withdraw the complete before-group, not a partial disposition"
                )
            if not old_refs & removed_refs:
                kept_changes.append(c)
        self.changes = kept_changes
        for edit in data["edits"]:
            try:
                key = edit_key(edit)
            except KeyError as exc:
                raise ValueError(
                    f"{edit['operation']} requires the object id inside node/edge"
                ) from exc
            old = self.pending.get(key)
            if (
                old
                and old["operation"].startswith("create_")
                and edit["operation"].startswith("update_")
            ):
                edit["operation"] = old["operation"]
            if (
                old
                and old["operation"].startswith("create_")
                and edit["operation"].startswith("delete_")
            ):
                self.pending.pop(key)
            else:
                self.pending[key] = edit
        # Replace an affected source/current correspondence group as a whole.
        replaced = {
            (e["kind"], e["id"], e.get("alternative_id"))
            for c in data["changes"]
            for e in c["before"]
        }
        kept = []
        for c in self.changes:
            old = {(e["kind"], e["id"], e.get("alternative_id")) for e in c["before"]}
            if old & replaced and not old <= replaced:
                raise ValueError(
                    "Cover the whole prior before-group when replacing its disposition"
                )
            if not old & replaced:
                kept.append(c)
        self.changes = kept + data["changes"]
        for key in result.remove_contracts:
            self.contracts.pop(key, None)
        installed = {c.id: c for c in self.seed.graph.relation_contracts}
        for contract in result.relation_contracts:
            if contract.id in installed and installed[contract.id] != contract:
                self.contracts.pop(contract.id, None)
                raise ValueError(
                    f"Installed predicate {contract.id} cannot be redefined. The conflicting definition was NOT saved; reuse its ID without resending a contract, or use a new predicate ID for a different meaning. Installed contract: {installed[contract.id].model_dump_json(exclude_defaults=True)}"
                )
            missing = [
                field
                for field in ("direction_semantics", "evidence_requirement")
                if not getattr(contract, field)
            ]
            if contract.id not in installed and missing:
                raise ValueError(
                    f"New contract {contract.id} requires all of {missing}; incomplete definition was not saved"
                )
            self.contracts[contract.id] = contract
        nodes = {n.id: n for n in self.seed.graph.nodes}
        edges = {e.id: e for e in self.seed.graph.edges}
        source_nodes = {n.id: n for n in self.base.graph.nodes}
        source_edges = {e.id: e for e in self.base.graph.edges}
        prior = self.preview or self.seed
        prior_objects = {"node": {n.id: n for n in prior.graph.nodes}, "edge": {e.id: e for e in prior.graph.edges}}
        physical_ids = {n.id for n in self.seed.graph.nodes if n.node_type in PHYSICAL}
        edits = list(self.pending.values())
        created_nodes = {
            e["node"]["id"] for e in edits if e["operation"] == "create_node"
        }
        created_edges = {
            e["edge"]["id"] for e in edits if e["operation"] == "create_edge"
        }
        for edit in edits:
            kind, raw_key = edit_key(edit)
            key = (
                self.aliases.get(raw_key, raw_key)
                if kind == "node"
                else self.edge_aliases.get(raw_key, raw_key)
            )
            objects = nodes if kind == "node" else edges
            exists = key in objects
            if exists and key not in self.allowed:
                raise ValueError(
                    f"Outside task write scope: {kind} {key}. Return a locally coupled proposal; the controller manages its scope."
                )
            previous = objects.get(key)
            if (kind == "node" and previous and previous.node_type in PHYSICAL) or (
                kind == "edge"
                and previous
                and all(
                    k in physical_ids
                    for k in (previous.from_node_id, previous.to_node_id)
                )
            ):
                raise ValueError(
                    f"Physical catalogue {kind} {key} is immutable. "
                    "For a new semantic object use create with a fresh ID under new_id_prefix; "
                    "withdraw any pending operation that reused this ID."
                )
            op = edit["operation"]
            if op.startswith("delete_"):
                if not exists:
                    raise ValueError(f"Cannot delete missing {kind} {key}")
                del objects[key]
                continue
            if op.startswith("create_") == exists:
                raise ValueError(
                    f"Use update for existing or create for new {kind} {key}"
                )
            raw = deepcopy(edit[kind])
            raw["id"] = key
            current_object = prior_objects[kind].get(key) or previous
            if current_object is not None:
                stored = current_object.model_dump(mode="json")
                properties = {} if op == "set_conflict" else dict(stored.get("properties", {}))
                for field, value in raw.get("properties", {}).items():
                    if value is None:
                        properties.pop(field, None)
                    else:
                        properties[field] = value
                raw = {**stored, **raw, "properties": properties}
            if kind == "node" and raw.get("merge_status") == "normal":
                # Normal GraphNode serialization omits empty conflict fields.
                # A full resolution must not inherit the old conflict envelope.
                raw.update(consolidation_status=None, preservation_reasons=[], alternatives=[], conflict_points=[], common_sources=[])
            if edit.get("evidence_ids"):
                raw["sources"] = [s.model_dump(mode="json") for s in evidence_sources(self.context, edit["evidence_ids"])]
            saved_object = prior_objects[kind].get(key)
            resolving = kind == 'node' and previous and previous.alternatives and raw.get('merge_status') == 'normal'
            if (previous or saved_object) and not resolving:
                supplied = raw.get("sources", [])
                refs = tuple(dict.fromkeys((*(previous.sources if previous else ()), *(saved_object.sources if saved_object else ()))))
                raw["sources"] = [s.model_dump(mode="json") for s in refs]
                raw["sources"].extend(s for s in supplied if s not in raw["sources"])
            if kind == "node" and raw.get("merge_status") == "conflict":
                from cortex.schema.validation import _stable_sources
                raw["sources"] = [s.model_dump(mode="json") for s in _stable_sources(
                    SourceReference.model_validate(ref)
                    for ref in [*raw.get('common_sources', []),
                                *(r for alternative in raw['alternatives'] for r in alternative['sources'])])]
            try:
                obj = (GraphNode if kind == "node" else GraphEdge).model_validate(raw)
            except (ValueError, TypeError) as error:
                raise ValueError(f"Invalid {kind} {key} in {op}: {error}") from error
            obj = (
                remap_node(obj, self.aliases, self.edge_aliases)
                if kind == "node"
                else remap_edge(obj, self.aliases)
            )
            if kind == "node" and obj.node_type in PHYSICAL:
                raise ValueError("Cannot create physical catalogue nodes")
            if kind == "node" and previous and previous.node_type != obj.node_type:
                raise ValueError(
                    "Type correction needs a new node and explicit rewrite"
                )
            if (
                kind == "edge"
                and previous is None
                and not (
                    {obj.from_node_id, obj.to_node_id} & (self.targets | created_nodes)
                )
            ):
                raise ValueError(
                    "A new business edge must involve a local task target or a node created by this proposal"
                )
            if kind == "edge" and all(
                k in physical_ids for k in (obj.from_node_id, obj.to_node_id)
            ):
                raise ValueError("Cannot write physical catalogue edges")
            objects[key] = obj
        changes = []
        for raw_change in self.changes:
            change = Change.model_validate(raw_change)

            def projected(ref):
                mapping = self.aliases if ref.kind == "node" else self.edge_aliases
                return ref.model_copy(update={"id": mapping.get(ref.id, ref.id)})

            changes.append(
                change.model_copy(
                    update={
                        "before": tuple(
                            dict.fromkeys(projected(r) for r in change.before)
                        ),
                        "after": tuple(
                            dict.fromkeys(projected(r) for r in change.after)
                        ),
                    }
                )
            )
        for conflict in result.conflicts:
            if conflict.node_id not in nodes or conflict.node_id not in (
                self.allowed | created_nodes
            ):
                raise ValueError("Conflict owner must be a current writable node")
            previous_alternatives = {a.alternative_id: a for a in nodes[conflict.node_id].alternatives}
            resolved = []
            for alternative in conflict.alternatives:
                old = previous_alternatives.get(alternative.alternative_id)
                refs = tuple(dict.fromkeys((*(old.sources if old else ()),
                    *(evidence_sources(self.context, alternative.evidence_ids) if alternative.evidence_ids else alternative.sources))))
                if not refs:
                    raise ValueError(f"New alternative {alternative.alternative_id} needs evidence_ids; existing IDs may retain their exact sources")
                resolved.append(alternative.model_copy(update={"sources": refs}))
            compact, _ = GraphManager.compact_conflict(
                nodes[conflict.node_id],
                [
                    (a.alternative_id, a.content, a.sources)
                    for a in resolved
                ],
                consolidation_status="semantic_conflict",
                conflict_points=conflict.conflict_points,
                common_sources=(evidence_sources(self.context, conflict.common_evidence_ids)
                    if 'common_evidence_ids' in conflict.model_fields_set else None),
            )
            nodes[compact.id] = compact
            # Persist successful complete alternative input for local retry.
            self.pending[("node", compact.id)] = {
                "operation": "create_node"
                if compact.id in created_nodes
                else "set_conflict",
                "node": compact.model_dump(mode="json"),
            }
        for merge in result.edge_merges:
            self.edge_merges[merge.survivor_id] = merge
        for merge in self.edge_merges.values():
            if merge.survivor_id not in edges or merge.survivor_id not in (
                self.allowed | created_edges
            ):
                raise ValueError("Merge survivor must be a current writable edge")
            survivor = edges[merge.survivor_id]
            refs = list(survivor.sources)
            before = (
                [{"kind": "edge", "id": survivor.id}]
                if survivor.id in source_edges
                else []
            )
            archived = []
            for source_id in dict.fromkeys(merge.source_ids):
                if source_id in source_edges and source_id not in self.allowed:
                    raise ValueError(f"Outside task write scope: edge {source_id}")
                original = (
                    source_edges.get(source_id)
                    or self.context.raw_edges.get(source_id)
                    or edges.get(source_id)
                )
                if original is None:
                    raise ValueError(f"Unknown source edge {source_id}")
                refs.extend(original.sources)
                if source_id in source_edges:
                    before.append({"kind": "edge", "id": source_id})
                elif source_id in self.context.raw_edges:
                    archived.append({"kind": "edge", "id": source_id})
                if source_id != survivor.id:
                    edges.pop(source_id, None)
            edges[survivor.id] = survivor.model_copy(
                update={"sources": tuple(dict.fromkeys(refs))}
            )
            after_ref = Element(kind="edge", id=survivor.id)
            for group in (before, archived):
                pending_refs = []
                for ref in dict.fromkeys(Element.model_validate(r) for r in group):
                    previous = [c for c in changes if ref in c.before]
                    if previous:
                        if any(after_ref not in c.after for c in previous):
                            raise ValueError(
                                f"Edge merge disagrees with the saved disposition for {ref.id}; replace the complete before-group with changes or withdraw it with remove_changes"
                            )
                    else:
                        pending_refs.append(ref)
                if pending_refs:
                    changes.append(
                        Change(
                            before=tuple(pending_refs),
                            after=(after_ref,),
                            explanation=merge.explanation,
                        )
                    )
        # Correspondences describe effects; they do not execute them. A current
        # input excluded from its replacement cannot survive wholly unchanged.
        stale = set()
        for change in changes:
            after_ids = {(r.kind, r.id) for r in change.after}
            for ref in change.before:
                old, current = (
                    (source_nodes, nodes)
                    if ref.kind == "node"
                    else (source_edges, edges)
                )
                if (
                    ref.alternative_id is None
                    and (ref.kind, ref.id) not in after_ids
                    and ref.id in old
                    and ref.id in current
                    and old[ref.id] == current[ref.id]
                ):
                    stale.add((ref.kind, ref.id))
        if stale:
            raise ValueError(
                f"Correspondence-only replacement leaves current objects unchanged: {sorted(stale)}. "
                "changes does not execute edits. Submit explicit delete_node/delete_edge edits to retire copies, "
                "or include each genuinely retained output in after; do not merely repeat the correspondence."
            )
        # Same-ID edits carry their own mechanical disposition; deletes do not.
        covered = {(e.kind, e.id) for c in changes for e in c.before}
        for kind, old, current in [
            ("node", source_nodes, nodes),
            ("edge", source_edges, edges),
        ]:
            for key, obj in old.items():
                target = (
                    self.aliases.get(key, key)
                    if kind == "node"
                    else self.edge_aliases.get(key, key)
                )
                if (kind, key) in covered or target != key:
                    continue
                if key not in current:
                    raise ValueError(
                        f"Delete requires explicit merge/rewrite/discard disposition: {kind} {key}"
                    )
                if current[key] != obj:
                    changes.append(
                        Change.model_validate(
                            {
                                "before": [{"kind": kind, "id": key}],
                                "after": [{"kind": kind, "id": key}],
                                "explanation": "Update the current object; original versions remain archived.",
                            }
                        )
                    )
        contracts = merge_relation_contracts(
            self.seed.graph.relation_contracts, tuple(self.contracts.values())
        )
        candidate = GraphState(
            graph=GraphSpace(
                nodes=tuple(nodes.values()),
                edges=tuple(edges.values()),
                relation_contracts=contracts,
            )
        )
        # Check every actual changed/new object and every resolved alternative.
        content = []
        for n in nodes.values():
            if source_nodes.get(n.id) != n:
                content.append(
                    {"operation": "update_node", "node": n.model_dump(mode="json")}
                )
        for e in edges.values():
            if source_edges.get(e.id) != e:
                content.append(
                    {"operation": "update_edge", "edge": e.model_dump(mode="json")}
                )
        errors = content_errors(content, self.context.schema)
        if errors:
            raise ValueError(str(errors))
        known = {
            s
            for n in self.context.raw_nodes.values()
            for s in (*n.sources, *(s for a in n.alternatives for s in a.sources))
        } | {s for e in self.context.raw_edges.values() for s in e.sources}
        for n in nodes.values():
            if not set(n.sources) <= known or any(
                not set(a.sources) <= known for a in n.alternatives
            ):
                raise ValueError(f"Invented source on {n.id}")
        if any(not set(e.sources) <= known for e in edges.values()):
            raise ValueError("Invented edge source")
        diagnostics = []
        indexed_edges = {edge.id: edge for edge in candidate.graph.edges}
        before = getattr(self.context, 'task_before', None)
        original_edges = {edge.id: edge for edge in before.graph.edges} if before is not None else {}
        groups = ([self.node_original_group] if self.node_original_group is not None else
                  list(getattr(self.context, 'task_identity_groups', ())))
        for row in self.node_structural_diagnostics:
            edge = indexed_edges.get(row['edge_id'])
            # An explicit deletion or a corrected mapping resolves the issue;
            # historical diagnostics must not follow a repaired candidate.
            if edge is None or edge.from_node_id != edge.to_node_id:
                continue
            original = original_edges.get(edge.id)
            participants = {row.get('original_from_node_id'), row.get('original_to_node_id'),
                            row.get('owner_id')}
            same_group = (any(participants <= set(group['member_ids']) for group in groups)
                          if groups else bool(self.aliases) and all(
                              self.aliases.get(key, key) == row.get('owner_id')
                              for key in (row.get('original_from_node_id'), row.get('original_to_node_id'))))
            if (not getattr(self.context, 'node_local_compile', False)
                    or row.get('code') != 'alias_created_self_relation'
                    or edge.edge_type != 'is_a' or row.get('edge_type') != edge.edge_type
                    or edge.from_node_id != row.get('owner_id')
                    or edge.from_node_id != row.get('from_node_id')
                    or edge.to_node_id != row.get('to_node_id')
                    or original is None or original.edge_type != edge.edge_type
                    or original.from_node_id == original.to_node_id
                    or original.from_node_id != row.get('original_from_node_id')
                    or original.to_node_id != row.get('original_to_node_id') or not same_group):
                raise ValueError('Invalid controller diagnostic for a pending node self-relation')
            diagnostics.append(row)
        deferred = {row['edge_id'] for row in diagnostics}
        validation_view = candidate if not deferred else candidate.model_copy(update={
            'graph': candidate.graph.model_copy(update={
                'edges': tuple(edge for edge in candidate.graph.edges if edge.id not in deferred)})})
        # This copy is for validation only. Assessment receives the actual
        # self-edge candidate; all unrelated structural defects still reject.
        validate_graph_state(validation_view, self.context.schema)
        self.node_structural_diagnostics = diagnostics
        if self.extra_check:
            self.extra_check(candidate, tuple(changes), result)
        self.preview = candidate
        self.result = result.model_copy(update={"changes": tuple(changes)})
        self.revision += 1
        self.context.candidate = candidate
        return self.result
