from __future__ import annotations

from cortex.application.merge_graph import (
    CanonicalIdMapping,
    DiagnosticCode,
    GraphMergeResult,
    MergeDiagnostic,
)
from cortex.capabilities.graph_merge.edges import (
    normalize_edges,
    prune_stale_reference_edges,
    remap_property_node_ids,
)
from cortex.capabilities.graph_merge.identity import IdentityGrouper
from cortex.capabilities.graph_merge.mining import BaseGraphMiner
from cortex.capabilities.graph_merge.models import (
    FinalNodeGroup,
    IdentityGroupingResult,
)
from cortex.capabilities.graph_merge.normalization import (
    EdgePropertyMappingKey,
    NodeGroupNormalizerRegistry,
)
from cortex.capabilities.graph_merge.reconciliation import NodeGroupReconciler
from cortex.capabilities.graph_merge.rewrite import GraphRewriteApplier
from cortex.schema import (
    GraphNode,
    GraphSpace,
    GraphState,
    InvalidGraphState,
    SchemaDefinition,
    validate_graph_state,
    validate_incoming_graph_state,
)


class FinalGraphStateInvalid(InvalidGraphState):
    """Raised when staged merge output fails final atomic validation."""


class GraphMergeOperator:
    def __init__(
        self,
        *,
        identity_grouper: IdentityGrouper,
        node_reconciler: NodeGroupReconciler,
        graph_miner: BaseGraphMiner | None = None,
        rewrite_applier: GraphRewriteApplier | None = None,
        normalizer_registry: NodeGroupNormalizerRegistry | None = None,
    ) -> None:
        self.identity_grouper = identity_grouper
        self.node_reconciler = node_reconciler
        self.graph_miner = graph_miner
        self.rewrite_applier = rewrite_applier or GraphRewriteApplier()
        self.normalizer_registry = normalizer_registry or NodeGroupNormalizerRegistry()

    def merge(
        self,
        existing: GraphState,
        incoming: GraphState,
        schema: SchemaDefinition,
    ) -> GraphMergeResult:
        validate_graph_state(existing, schema)
        _validate_disjoint_ids(existing, incoming)
        validate_incoming_graph_state(incoming, schema, existing=existing.graph)
        if not incoming.graph.nodes and not incoming.graph.edges:
            return GraphMergeResult(
                state=existing,
                node_mappings=(),
                edge_mappings=(),
                diagnostics=(),
            )

        grouping = self.identity_grouper.group(
            existing.graph.nodes,
            incoming.graph.nodes,
            schema.node_type_index(),
            {
                node_type.id: frozenset(schema.effective_node_fields(node_type.id))
                for node_type in schema.node_types
            },
        )
        grouping = _coalesce_canonical_id_collisions(grouping, self.node_reconciler)
        historical = {item.node_id: item for item in existing.node_conflicts}
        nodes = []
        conflicts = []
        node_mapping_values = {}
        canonical_by_ref = {}
        edge_property_mappings: dict[EdgePropertyMappingKey, object] = {}
        diagnostics = [_diagnostic(code, "node") for code in grouping.diagnostic_codes]
        available_nodes = {node.id: node for node in (*existing.graph.nodes, *incoming.graph.nodes)}
        for group in grouping.groups:
            normalized = self.normalizer_registry.normalize(group, schema)
            group = normalized.group
            edge_property_mappings.update(normalized.edge_property_mappings)
            dependencies = _dependency_nodes_for_group(
                group,
                available_nodes,
                historical.get(group.existing_node_id),
                known_conflicts=existing.node_conflicts,
            )
            dependency_ids = {node.id for node in dependencies}
            dependency_conflicts = tuple(
                item for item in existing.node_conflicts if item.node_id in dependency_ids
            )
            extra_context = (
                {"dependency_conflicts": dependency_conflicts} if dependency_conflicts else {}
            )
            reconciled = self.node_reconciler.reconcile_group(
                group,
                schema=schema,
                dependency_nodes=dependencies,
                **extra_context,
                historical_conflict=(
                    historical.get(group.existing_node_id)
                    if group.existing_node_id is not None
                    else None
                ),
            )
            nodes.append(reconciled.node)
            if reconciled.conflict is not None:
                conflicts.append(reconciled.conflict)
            diagnostics.extend(
                _diagnostic(code, "node", reconciled.node.id)
                for code in reconciled.diagnostic_codes
            )
            for member in group.members:
                canonical_by_ref[member.ref] = reconciled.node.id
                if member.ref.origin == "incoming_node":
                    node_mapping_values[member.ref.ref_id] = reconciled.node.id
        existing_replacements = {}
        for loser, winner in grouping.cross_type_replacements:
            canonical_id = canonical_by_ref.get(winner)
            if canonical_id is None:
                raise FinalGraphStateInvalid("cross-type replacement target was not reconciled")
            canonical_by_ref[loser] = canonical_id
            if loser.origin == "incoming_node":
                node_mapping_values[loser.ref_id] = canonical_id
            else:
                existing_replacements[loser.ref_id] = canonical_id
        if set(node_mapping_values) != {node.id for node in incoming.graph.nodes}:
            raise FinalGraphStateInvalid("node mappings do not cover every incoming node")
        all_node_mappings = {**existing_replacements, **node_mapping_values}
        nodes = [
            node.model_copy(
                update={
                    "properties": remap_property_node_ids(
                        node.properties,
                        all_node_mappings,
                    )
                }
            )
            for node in nodes
        ]

        edge_result = normalize_edges(
            existing=existing.graph.edges,
            incoming=incoming.graph.edges,
            nodes=tuple(nodes),
            node_mappings=all_node_mappings,
            property_value_mappings=edge_property_mappings,
            schema=schema,
        )
        diagnostics.extend(
            MergeDiagnostic(
                code=DiagnosticCode.EDGE_CONTENT_VARIANT,
                element_kind="edge",
                element_id=edge_id,
                message="same edge identity has a distinct content variant",
            )
            for edge_id in edge_result.variant_edge_ids
        )
        diagnostics.extend(
            MergeDiagnostic(
                code=DiagnosticCode.INVALIDATED_RELATIONSHIP_DROPPED,
                element_kind="edge",
                element_id=edge_id,
                message="relationship is incompatible with the corrected asset type",
            )
            for edge_id in edge_result.invalidated_edge_ids
        )
        conflicts = [
            conflict.model_copy(
                update={
                    "alternatives": tuple(
                        alternative.model_copy(
                            update={
                                "content": remap_property_node_ids(
                                    alternative.content, all_node_mappings
                                )
                            }
                        )
                        for alternative in conflict.alternatives
                    )
                }
            )
            for conflict in conflicts
        ]
        synchronized_edges, dropped_reference_edge_ids = prune_stale_reference_edges(
            edge_result.edges,
            tuple(nodes),
            tuple(conflicts),
        )
        diagnostics.extend(
            MergeDiagnostic(
                code=DiagnosticCode.STALE_REFERENCE_EDGE_DROPPED,
                element_kind="edge",
                element_id=edge_id,
                message="compiled dependency edge is absent from the canonical reference closure",
            )
            for edge_id in dropped_reference_edge_ids
        )
        state = GraphState(
            graph=GraphSpace(
                nodes=tuple(sorted(nodes, key=lambda item: (item.node_type, item.id))),
                edges=synchronized_edges,
            ),
            node_conflicts=tuple(sorted(conflicts, key=lambda item: item.node_id)),
        )
        if self.graph_miner is not None:
            mining = self.graph_miner.propose(state, schema)
            diagnostics.extend(
                MergeDiagnostic(
                    code=DiagnosticCode(item.code),
                    element_kind="merge",
                    message=(
                        item.message
                        if item.component_id is None
                        else f"{item.component_id}: {item.message}"
                    ),
                )
                for item in mining.diagnostics
            )
            rewritten = self.rewrite_applier.apply(state, mining.proposals, schema)
            state = rewritten.state
            diagnostics.extend(_diagnostic(code, "merge") for code in rewritten.diagnostic_codes)
        try:
            validate_graph_state(state, schema)
        except InvalidGraphState as exc:
            raise FinalGraphStateInvalid(str(exc)) from exc
        node_mappings = tuple(
            CanonicalIdMapping(input_id=input_id, canonical_id=canonical_id)
            for input_id, canonical_id in sorted(node_mapping_values.items())
        )
        dropped_reference_edges = {
            *dropped_reference_edge_ids,
            *edge_result.invalidated_edge_ids,
        }
        edge_mappings = tuple(
            CanonicalIdMapping(input_id=input_id, canonical_id=canonical_id)
            for input_id, canonical_id in sorted(edge_result.mappings)
            if canonical_id not in dropped_reference_edges
        )
        return GraphMergeResult(
            state=state,
            node_mappings=node_mappings,
            edge_mappings=edge_mappings,
            diagnostics=tuple(diagnostics),
        )


def _validate_disjoint_ids(existing: GraphState, incoming: GraphState) -> None:
    node_collisions = {item.id for item in existing.graph.nodes} & {
        item.id for item in incoming.graph.nodes
    }
    edge_collisions = {item.id for item in existing.graph.edges} & {
        item.id for item in incoming.graph.edges
    }
    if node_collisions or edge_collisions:
        raise InvalidGraphState("existing and incoming element ids must be disjoint")


def _dependency_nodes_for_group(
    group: FinalNodeGroup,
    available_nodes: dict[str, GraphNode],
    historical_conflict=None,
    *,
    known_conflicts=(),
) -> tuple[GraphNode, ...]:
    """Return the bounded transitive node closure referenced by this identity group."""

    member_ids = {member.ref.ref_id for member in group.members}
    pending = [
        ref
        for member in group.members
        for ref in member.content.properties.get("references", ())
        if isinstance(ref, str)
    ]
    if historical_conflict is not None:
        pending.extend(
            ref
            for alternative in historical_conflict.alternatives
            for ref in alternative.content.get("properties", {}).get("references", ())
            if isinstance(ref, str)
        )
    dependencies: dict[str, GraphNode] = {}
    conflicts_by_id = {item.node_id: item for item in known_conflicts}
    while pending:
        reference = pending.pop()
        if reference in member_ids or reference in dependencies:
            continue
        node = available_nodes.get(reference)
        if node is None:
            continue
        dependencies[reference] = node
        if reference in conflicts_by_id:
            pending.extend(
                ref
                for alternative in conflicts_by_id[reference].alternatives
                for ref in alternative.content.get("properties", {}).get("references", ())
                if isinstance(ref, str)
            )
        pending.extend(ref for ref in node.properties.get("references", ()) if isinstance(ref, str))
    return tuple(dependencies[node_id] for node_id in sorted(dependencies))


def _coalesce_canonical_id_collisions(
    grouping: IdentityGroupingResult,
    reconciler: NodeGroupReconciler,
) -> IdentityGroupingResult:
    """Reunite revised authoring content with its existing stable canonical identity."""

    by_canonical_id: dict[str, list[FinalNodeGroup]] = {}
    for group in grouping.groups:
        by_canonical_id.setdefault(reconciler.canonical_id_for_group(group), []).append(group)
    groups: list[FinalNodeGroup] = []
    for canonical_id, colliding in by_canonical_id.items():
        if len(colliding) == 1:
            groups.append(colliding[0])
            continue
        existing = [group for group in colliding if group.existing_node_id == canonical_id]
        if len(existing) != 1:
            raise FinalGraphStateInvalid(
                f"canonical node id collision cannot be anchored: {canonical_id}"
            )
        members = {member.ref: member for group in colliding for member in group.members}
        anchor = existing[0]
        groups.append(
            anchor.model_copy(
                update={
                    "members": tuple(
                        members[ref]
                        for ref in sorted(members, key=lambda item: (item.origin, item.ref_id))
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
