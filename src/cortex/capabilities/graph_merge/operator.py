from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from functools import lru_cache
from importlib.resources import files

from cortex.application.merge_graph import (
    CanonicalIdMapping,
    DiagnosticCode,
    GraphMergeResult,
    MergeDiagnostic,
)
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.edges import (
    normalize_edges,
    remap_property_node_ids,
)
from cortex.capabilities.graph_merge.identity.legacy import IdentityGrouper
from cortex.capabilities.graph_merge.models import (
    FinalNodeGroup,
    IdentityGroupingResult,
    NodeRepresentation,
    RepresentationRef,
)
from cortex.capabilities.graph_merge.normalization import (
    NodeGroupNormalizerRegistry,
)
from cortex.capabilities.graph_merge.reconciliation import NodeGroupReconciler
from cortex.schema import (
    GraphSpace,
    GraphState,
    InvalidGraphState,
    SchemaDefinition,
)
from cortex.schema.relation_contracts import effective_schema, merge_relation_contracts
from cortex.schema.relationships import is_dependency_edge

PHYSICAL_TYPES = frozenset({"table", "column", "dimension_value"})


@lru_cache(maxsize=1)
def _implementation_fingerprint():
    root = files("cortex")
    paths = (
        "application/ontology_graph.py",
        "schema/relationships.py",
        "schema/references.py",
        "schema/semantic_validation.py",
        *(
            f"capabilities/graph_merge/{name}"
            for name in (
                "operator.py",
                "transactions.py",
                "identity/legacy.py",
                "normalization.py",
                "edges.py",
                "models.py",
                "workflow/run.py",
                "workflow/group.py",
                "workflow/context.py",
                "reconciliation/service.py",
                "reconciliation/models.py",
                "reconciliation/materialization.py",
                "reconciliation/validation.py",
                "llm/agents.py",
                "llm/tools.py",
            )
        ),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode())
        digest.update(root.joinpath(path).read_bytes())
    return digest.hexdigest()


class FinalGraphStateInvalid(InvalidGraphState):
    """Raised when staged merge output fails final atomic validation."""


class GraphMergeOperator:
    def __init__(
        self,
        *,
        identity_grouper: IdentityGrouper,
        node_reconciler: NodeGroupReconciler,
        checkpoint_factory=None,
        workflow_version: str = "merge-v2-transactions",
        normalizer_registry: NodeGroupNormalizerRegistry | None = None,
    ) -> None:
        self.identity_grouper = identity_grouper
        self.node_reconciler = node_reconciler
        self.checkpoint_factory = checkpoint_factory
        self.workflow_version = f"{workflow_version}:{_implementation_fingerprint()}"
        self.normalizer_registry = normalizer_registry or NodeGroupNormalizerRegistry()

    def merge(
        self,
        existing: GraphState,
        incoming: GraphState,
        schema: SchemaDefinition,
        *,
        run_id: str | None = None,
    ) -> GraphMergeResult:
        from cortex.capabilities.graph_merge.workflow.run import run_merge
        from cortex.infrastructure.workflow.tracing import merge_tracing

        schema = effective_schema(schema, GraphSpace(relation_contracts=merge_relation_contracts(
            existing.graph.relation_contracts, incoming.graph.relation_contracts)))
        manager = GraphManager(schema)
        manager.validate_state(existing)
        _validate_disjoint_ids(existing, incoming)
        incoming = manager.prepare_fragment(incoming, existing=existing)
        if not incoming.graph.nodes and not incoming.graph.edges:
            return GraphMergeResult(
                state=existing, node_mappings=(), edge_mappings=(), diagnostics=()
            )
        factory = self.checkpoint_factory or (lambda: nullcontext(None))
        with factory() as saver, merge_tracing():
            result = run_merge(
                self, existing, incoming, schema, saver=saver, run_id=run_id
            )
        # Completed checkpoint results bypass _merge; validate on every return.
        manager.validate_state(result.state)
        return result

    def group_nodes(self, existing, incoming, schema):
        from cortex.schema.references import remap_references

        fields = {
            t.id: frozenset(schema.effective_node_fields(t.id))
            for t in schema.node_types
        }
        physical_groups, mappings = [], {}
        for node_type in ("table", "column", "dimension_value"):
            old_nodes = tuple(
                n for n in existing.graph.nodes if n.node_type == node_type
            )
            new_nodes = tuple(
                n.model_copy(
                    update={"properties": remap_references(n.properties, mappings)}
                )
                for n in incoming.graph.nodes
                if n.node_type == node_type
            )
            grouped = IdentityGrouper(None).group(
                old_nodes, new_nodes, schema.node_type_index(), fields
            )
            physical_groups.extend(grouped.groups)
            for group in grouped.groups:
                canonical = self.node_reconciler.canonical_id_for_group(group)
                mappings.update({m.ref.ref_id: canonical for m in group.members})
        old_nodes = tuple(
            n for n in existing.graph.nodes if n.node_type not in PHYSICAL_TYPES
        )
        new_nodes = tuple(
            n.model_copy(
                update={"properties": remap_references(n.properties, mappings)}
            )
            for n in incoming.graph.nodes
            if n.node_type not in PHYSICAL_TYPES
        )
        old_by_id = {n.id: n for n in old_nodes}
        alternatives = {
            c.id: tuple(
                NodeRepresentation(
                    ref=RepresentationRef(
                        origin="existing_alternative", ref_id=a.alternative_id
                    ),
                    node_type=old_by_id[c.id].node_type,
                    content=GraphManager.resolve_alternative(old_by_id[c.id], a),
                    sources=a.sources,
                )
                for a in c.alternatives
            )
            for c in existing.conflict_nodes
        }
        semantic = self.identity_grouper.group(
            old_nodes,
            new_nodes,
            schema.node_type_index(),
            fields,
            existing_alternatives=alternatives,
        )
        if semantic.cross_type_replacements:
            raise FinalGraphStateInvalid(
                "Cross-type replacement requires separate reviewed type correction"
            )
        return _coalesce_canonical_id_collisions(
            semantic.model_copy(
                update={"groups": (*physical_groups, *semantic.groups)}
            ),
            self.node_reconciler,
        )

    def begin_merge(self, existing, incoming, schema, grouping):
        from cortex.capabilities.graph_merge.transactions import apply_merge_state

        mappings = {
            member.ref.ref_id: self.node_reconciler.canonical_id_for_group(group)
            for group in grouping.groups
            for member in group.members
        }
        nodes = {n.id: n for n in existing.graph.nodes}
        for group in grouping.groups:
            if group.node_type not in PHYSICAL_TYPES:
                continue
            if all(m.ref.origin == "existing_node" for m in group.members):
                continue
            result = self.node_reconciler.reconcile_group(group, schema=schema)
            node = result.node.model_copy(
                update={
                    "properties": remap_property_node_ids(
                        result.node.properties, mappings
                    )
                }
            )
            nodes[node.id] = node
        physical_edges = tuple(
            edge
            for edge in incoming.graph.edges
            if mappings.get(edge.from_node_id, edge.from_node_id) in nodes
            and mappings.get(edge.to_node_id, edge.to_node_id) in nodes
            and nodes[mappings.get(edge.from_node_id, edge.from_node_id)].node_type
            in PHYSICAL_TYPES
            and nodes[mappings.get(edge.to_node_id, edge.to_node_id)].node_type
            in PHYSICAL_TYPES
        )
        schema = effective_schema(schema, GraphSpace(relation_contracts=merge_relation_contracts(existing.graph.relation_contracts, incoming.graph.relation_contracts)))
        normalized = normalize_edges(
            existing=existing.graph.edges,
            incoming=physical_edges,
            nodes=tuple(nodes.values()),
            node_mappings=mappings,
            schema=schema,
        )
        candidate = GraphState(
            graph=GraphSpace(nodes=tuple(nodes.values()), edges=normalized.edges,
                relation_contracts=merge_relation_contracts(existing.graph.relation_contracts, incoming.graph.relation_contracts)),
        )
        manager = GraphManager(schema, existing)
        working = apply_merge_state(manager, candidate, request_id="merge:physical")
        return {
            "working": working.model_dump(mode="json"),
            "remaining": [
                g.model_dump(mode="json")
                for g in grouping.groups
                if g.node_type not in PHYSICAL_TYPES
            ],
            "mappings": mappings,
            "changed_dependencies": [],
            "diagnostics": [
                _diagnostic(c, "node").model_dump(mode="json")
                for c in grouping.diagnostic_codes
            ],
            "edge_mappings": [list(pair) for pair in normalized.mappings],
            "completed": [],
            "target_mappings": [],
        }

    def advance_merge(
        self,
        assembly,
        existing,
        incoming,
        schema,
        *,
        evidence_items=(),
        checkpointer=None,
        run_id=None,
    ):
        from cortex.capabilities.graph_merge.transactions import (
            apply_merge_state,
            preview_group,
        )
        from cortex.capabilities.graph_merge.workflow.context import GroupContext
        from cortex.schema.references import remap_references

        working = GraphState.model_validate(assembly["working"])
        schema = effective_schema(schema, working.graph)
        remaining = [FinalNodeGroup.model_validate(g) for g in assembly["remaining"]]
        mappings = assembly["mappings"]
        pending_ids = {
            self.node_reconciler.canonical_id_for_group(g) for g in remaining
        }
        group = next(
            (
                g
                for g in remaining
                if not _group_dependency_ids(g, working, incoming, mappings)
                & pending_ids
            ),
            None,
        )
        if group is None:
            raise FinalGraphStateInvalid(
                "Unresolved or cyclic incoming dependency groups; no partial graph published"
            )
        original_group = group
        normalized = self.normalizer_registry.normalize(group, schema)
        group = normalized.group.model_copy(
            update={
                "members": tuple(
                    m.model_copy(
                        update={
                            "content": m.content.model_copy(
                                update={
                                    "properties": remap_references(
                                        m.content.properties, mappings
                                    )
                                }
                            )
                        }
                    )
                    for m in normalized.group.members
                )
            }
        )
        canonical = self.node_reconciler.canonical_id_for_group(group)
        by_id = {n.id: n for n in working.graph.nodes}
        dependencies = _group_dependency_ids(group, working, incoming, mappings)
        changed = set(assembly["changed_dependencies"])
        conflict_ids = {c.id for c in working.conflict_nodes}
        force = bool(dependencies & (changed | conflict_ids))
        untouched = all(m.ref.origin == "existing_node" for m in group.members)
        input_edges = tuple(
            edge.model_copy(
                update={
                    "properties": {
                        key: normalized.edge_property_mappings.get(
                            (
                                edge.from_node_id,
                                key,
                                json.dumps(
                                    value,
                                    sort_keys=True,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            ),
                            value,
                        )
                        for key, value in edge.properties.items()
                    }
                }
            )
            for edge in incoming.graph.edges
        )
        manager = GraphManager(schema, working)
        revision = manager.revision
        historical = next(
            (c for c in working.conflict_nodes if c.id == canonical), None
        )
        reconciled = self.node_reconciler.reconcile_group(
            group,
            schema=schema,
            historical_conflict=historical,
            dependency_nodes=tuple(
                n for n in working.graph.nodes if n.id in dependencies
            ),
            available_nodes=working.graph.nodes,
            outgoing_edges=working.graph.edges,
            evidence_items=evidence_items,
            checkpointer=checkpointer,
            run_id=run_id,
            unchanged_relations=_same_outgoing_relations(
                original_group, existing, incoming
            ),
            force_reconcile=force,
            existing_state=working,
            node_mappings=mappings,
            input_nodes=(*existing.graph.nodes, *incoming.graph.nodes),
            input_relations=input_edges,
        )
        if reconciled.consolidation.get("identity_mismatch"):
            split = []
            mappings = dict(mappings)
            for member in original_group.members:
                singleton = FinalNodeGroup(
                    group_id=f"{original_group.group_id}:separate:{member.ref.ref_id}",
                    node_type=original_group.node_type,
                    existing_node_id=original_group.existing_node_id
                    if member.ref.origin == "existing_node"
                    else None,
                    members=(member,),
                )
                mappings[member.ref.ref_id] = (
                    self.node_reconciler.canonical_id_for_group(singleton)
                )
                split.append(singleton)
            return {
                **assembly,
                "mappings": mappings,
                "remaining": [
                    g.model_dump(mode="json")
                    for g in (
                        *split,
                        *(g for g in remaining if g.group_id != group.group_id),
                    )
                ],
                "diagnostics": [
                    *assembly["diagnostics"],
                    MergeDiagnostic(
                        code=DiagnosticCode.INVALID_SELECTION,
                        element_kind="node",
                        element_id=canonical,
                        message=f"Identity group separated after Assessment: {reconciled.consolidation['identity_mismatch']}",
                    ).model_dump(mode="json"),
                ],
            }
        context = GroupContext(
            group=group,
            schema_definition=schema,
            existing_state=working,
            nodes=working.graph.nodes,
            relations=working.graph.edges,
            input_relations=input_edges,
            node_mappings=mappings,
        )
        if untouched and not force:
            updated = working
        else:
            candidate = preview_group(context, reconciled)
            updated = apply_merge_state(
                manager, candidate, request_id=f"merge:{group.group_id}:{revision}"
            )
        previous = by_id.get(canonical)
        current = next(n for n in updated.graph.nodes if n.id == canonical)
        current_conflict = next(
            (c for c in updated.conflict_nodes if c.id == canonical), None
        )
        if previous != current or historical != current_conflict:
            changed.add(canonical)
        diagnostics = list(assembly["diagnostics"])
        diagnostics.extend(
            _diagnostic(code, "node", canonical).model_dump(mode="json")
            for code in reconciled.diagnostic_codes
        )
        return {
            **assembly,
            "working": updated.model_dump(mode="json"),
            "remaining": [
                g.model_dump(mode="json")
                for g in remaining
                if g.group_id != group.group_id
            ],
            "changed_dependencies": sorted(changed),
            "diagnostics": diagnostics,
            "target_mappings": [
                *assembly["target_mappings"],
                *[
                    [list(key), value]
                    for key, value in normalized.edge_property_mappings.items()
                ],
            ],
            "completed": [
                *assembly["completed"],
                {
                    "group_id": group.group_id,
                    "node_id": canonical,
                    "base_revision": revision,
                    "revision": GraphManager(schema, updated).revision,
                    "conflict": current_conflict is not None,
                },
            ],
        }

    def finish_merge(self, assembly, incoming, schema):
        from cortex.capabilities.graph_merge.transactions import apply_merge_state

        working = GraphState.model_validate(assembly["working"])
        mappings = assembly["mappings"]
        if not {n.id for n in incoming.graph.nodes} <= mappings.keys():
            raise FinalGraphStateInvalid(
                "node mappings do not cover every incoming node"
            )
        schema = effective_schema(schema, working.graph)
        normalized = normalize_edges(
            existing=working.graph.edges,
            incoming=tuple(
                e for e in incoming.graph.edges if e.reference_origin is None
            ),
            nodes=working.graph.nodes,
            node_mappings=mappings,
            property_value_mappings={
                tuple(key): value for key, value in assembly["target_mappings"]
            },
            schema=schema,
        )
        candidate = working.model_copy(
            update={
                "graph": working.graph.model_copy(update={"edges": normalized.edges})
            }
        )
        manager = GraphManager(schema, working)
        state = apply_merge_state(
            manager, candidate, request_id="merge:final-relations"
        )
        state = state.model_copy(
            update={
                "graph": GraphSpace(
                    nodes=tuple(
                        sorted(state.graph.nodes, key=lambda n: (n.node_type, n.id))
                    ),
                    edges=state.graph.edges,
                    relation_contracts=state.graph.relation_contracts,
                ),
            }
        )
        manager.validate_state(state)
        diagnostics = [
            MergeDiagnostic.model_validate(d) for d in assembly["diagnostics"]
        ]
        diagnostics.extend(
            _diagnostic("EDGE_CONTENT_VARIANT", "edge", edge_id)
            for edge_id in normalized.variant_edge_ids
        )
        edge_by_anchor = {}
        for edge in state.graph.edges:
            anchor = (
                edge.edge_type,
                edge.from_node_id,
                edge.to_node_id,
                edge.properties.get("target_id"),
            )
            edge_by_anchor.setdefault(anchor, []).append(edge.id)
        edge_mappings = dict(assembly["edge_mappings"])
        edge_mappings.update(normalized.mappings)
        target_mappings = {
            tuple(key): value for key, value in assembly["target_mappings"]
        }
        for edge in incoming.graph.edges:
            if edge_mappings.get(edge.id) in {item.id for item in state.graph.edges}:
                continue
            target_id = target_mappings.get(
                (
                    edge.from_node_id,
                    "target_id",
                    json.dumps(
                        edge.properties.get("target_id"),
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
                edge.properties.get("target_id"),
            )
            key = (
                edge.edge_type,
                mappings.get(edge.from_node_id, edge.from_node_id),
                mappings.get(edge.to_node_id, edge.to_node_id),
                target_id,
            )
            if len(edge_by_anchor.get(key, ())) == 1:
                edge_mappings[edge.id] = edge_by_anchor[key][0]
            elif len(edge_by_anchor.get(key, ())) > 1:
                diagnostics.append(
                    MergeDiagnostic(
                        code=DiagnosticCode.EDGE_CONTENT_VARIANT,
                        element_kind="edge",
                        element_id=edge.id,
                        message="Multiple final edge variants; no ambiguous canonical mapping reported",
                    )
                )
        final_ids = {e.id for e in state.graph.edges}
        return GraphMergeResult(
            state=state,
            node_mappings=tuple(
                CanonicalIdMapping(input_id=n.id, canonical_id=mappings[n.id])
                for n in sorted(incoming.graph.nodes, key=lambda n: n.id)
            ),
            edge_mappings=tuple(
                CanonicalIdMapping(input_id=key, canonical_id=value)
                for key, value in sorted(edge_mappings.items())
                if value in final_ids
            ),
            diagnostics=tuple(diagnostics),
        )

    def _merge(
        self,
        existing,
        incoming,
        schema,
        *,
        grouping,
        evidence_items=(),
        checkpointer=None,
        run_id=None,
    ):
        assembly = self.begin_merge(existing, incoming, schema, grouping)
        while assembly["remaining"]:
            assembly = self.advance_merge(
                assembly,
                existing,
                incoming,
                schema,
                evidence_items=evidence_items,
                checkpointer=checkpointer,
                run_id=run_id,
            )
        return self.finish_merge(assembly, incoming, schema)


def _validate_disjoint_ids(existing: GraphState, incoming: GraphState) -> None:
    node_collisions = {item.id for item in existing.graph.nodes} & {
        item.id for item in incoming.graph.nodes
    }
    edge_collisions = {item.id for item in existing.graph.edges} & {
        item.id for item in incoming.graph.edges
    }
    if node_collisions or edge_collisions:
        raise InvalidGraphState("existing and incoming element ids must be disjoint")


def _group_dependency_ids(group, working, incoming, mappings):
    from cortex.schema.references import content_reference_ids

    outgoing = {}
    for edge in (*working.graph.edges, *incoming.graph.edges):
        if is_dependency_edge(edge):
            owner = mappings.get(edge.from_node_id, edge.from_node_id)
            outgoing.setdefault(owner, set()).add(
                mappings.get(edge.to_node_id, edge.to_node_id)
            )
    for node in (*working.graph.nodes, *incoming.graph.nodes):
        owner = mappings.get(node.id, node.id)
        outgoing.setdefault(owner, set()).update(
            mappings.get(ref, ref) for ref in content_reference_ids(node.properties)
        )
    owners = {mappings.get(m.ref.ref_id, m.ref.ref_id) for m in group.members}
    if group.existing_node_id:
        owners.add(group.existing_node_id)
    pending = [ref for owner in owners for ref in outgoing.get(owner, ())]
    pending.extend(
        mappings.get(ref, ref)
        for m in group.members
        for ref in content_reference_ids(m.content.properties)
    )
    dependencies = set()
    while pending:
        ref = pending.pop()
        if ref in owners or ref in dependencies:
            continue
        dependencies.add(ref)
        pending.extend(outgoing.get(ref, ()))
    return dependencies


def _coalesce_canonical_id_collisions(
    grouping: IdentityGroupingResult,
    reconciler: NodeGroupReconciler,
) -> IdentityGroupingResult:
    """Reunite revised authoring content with its existing stable canonical identity."""

    by_canonical_id: dict[str, list[FinalNodeGroup]] = {}
    for group in grouping.groups:
        by_canonical_id.setdefault(reconciler.canonical_id_for_group(group), []).append(
            group
        )
    groups: list[FinalNodeGroup] = []
    for canonical_id, colliding in by_canonical_id.items():
        if len(colliding) == 1:
            groups.append(colliding[0])
            continue
        existing = [
            group for group in colliding if group.existing_node_id == canonical_id
        ]
        if len(existing) != 1:
            raise FinalGraphStateInvalid(
                f"canonical node id collision cannot be anchored: {canonical_id}"
            )
        members = {
            member.ref: member for group in colliding for member in group.members
        }
        anchor = existing[0]
        groups.append(
            anchor.model_copy(
                update={
                    "members": tuple(
                        members[ref]
                        for ref in sorted(
                            members, key=lambda item: (item.origin, item.ref_id)
                        )
                    )
                }
            )
        )
    return grouping.model_copy(
        update={"groups": tuple(sorted(groups, key=lambda item: item.group_id))}
    )


def _diagnostic(code: str, kind: str, element_id: str | None = None) -> MergeDiagnostic:
    return MergeDiagnostic(
        code=DiagnosticCode(code),
        element_kind=kind,
        element_id=element_id,
        message=code.replace("_", " ").lower(),
    )


def _same_outgoing_relations(group, existing, incoming):
    member_ids = {m.ref.ref_id for m in group.members}
    signatures = []
    for member in group.members:
        signatures.append(
            frozenset(
                (
                    e.edge_type,
                    "self" if e.to_node_id in member_ids else e.to_node_id,
                    json.dumps(e.properties, sort_keys=True, ensure_ascii=False),
                )
                for e in (*existing.graph.edges, *incoming.graph.edges)
                if e.from_node_id == member.ref.ref_id
            )
        )
    return all(value == signatures[0] for value in signatures)
