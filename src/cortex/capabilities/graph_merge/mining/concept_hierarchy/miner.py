from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Awaitable
from typing import Protocol

from cortex.capabilities.graph_merge.mining.base import (
    BaseGraphMiner,
    GraphMiningResult,
    MiningDiagnostic,
)
from cortex.capabilities.graph_merge.mining.concept_hierarchy.models import (
    ConceptLocalSubgraph,
    ConceptMinerOutcome,
    ConceptNodeAlternative,
    ConceptNodeView,
    ConceptTrigger,
)
from cortex.capabilities.graph_merge.models import NodeContent
from cortex.capabilities.graph_merge.rewrite.contracts import (
    GraphRewriteOperations,
    GraphRewriteProposal,
)
from cortex.schema import GraphNode, GraphState, SchemaDefinition


class ConceptResolver(Protocol):
    def resolve(
        self,
        subgraph: ConceptLocalSubgraph,
        schema: SchemaDefinition,
        *,
        allow_grounding_removal: bool,
    ) -> Awaitable[ConceptMinerOutcome]: ...


class ConceptConnectionMiner(BaseGraphMiner):
    miner_id = "concept_connection"

    def __init__(
        self,
        resolver: ConceptResolver,
        *,
        allow_grounding_removal: bool = False,
        max_components: int = 16,
        max_nodes_per_component: int = 32,
        max_edges_per_component: int = 64,
        max_proposals: int = 16,
    ) -> None:
        self.resolver = resolver
        self.allow_grounding_removal = allow_grounding_removal
        self.max_components = max_components
        self.max_nodes_per_component = max_nodes_per_component
        self.max_edges_per_component = max_edges_per_component
        self.max_proposals = max_proposals

    async def propose(
        self, state: GraphState, schema: SchemaDefinition
    ) -> GraphMiningResult:
        subgraphs, diagnostics = self._locate(state)
        proposals: list[GraphRewriteProposal] = []
        for subgraph in subgraphs:
            if len(proposals) >= self.max_proposals:
                diagnostics.append(
                    MiningDiagnostic(
                        code="MINING_PROPOSAL_LIMIT_REACHED",
                        message="concept mining proposal limit reached",
                    )
                )
                break
            try:
                outcome = await self.resolver.resolve(
                    subgraph,
                    schema,
                    allow_grounding_removal=self.allow_grounding_removal,
                )
            except Exception:
                diagnostics.append(
                    MiningDiagnostic(
                        code="MINER_MODEL_FAILURE",
                        component_id=subgraph.component_id,
                        message="concept resolver failed",
                    )
                )
                continue
            if outcome.action == "no_change":
                continue
            operations = outcome.operations
            if operations is None or not self._within_scope(subgraph, operations):
                diagnostics.append(
                    MiningDiagnostic(
                        code="INVALID_MINER_PROPOSAL",
                        component_id=subgraph.component_id,
                        message="concept proposal referenced elements outside its local subgraph",
                    )
                )
                continue
            if operations.remove_edge_ids and not self.allow_grounding_removal:
                diagnostics.append(
                    MiningDiagnostic(
                        code="INVALID_MINER_PROPOSAL",
                        component_id=subgraph.component_id,
                        message="grounding removal is disabled",
                    )
                )
                continue
            proposals.append(
                GraphRewriteProposal(
                    miner_id=self.miner_id,
                    component_id=subgraph.component_id,
                    proposal_key=_proposal_key(subgraph.component_id, operations),
                    reason=outcome.reason,
                    operations=operations,
                )
            )
        return GraphMiningResult(
            proposals=tuple(proposals), diagnostics=tuple(diagnostics)
        )

    def _locate(
        self, state: GraphState
    ) -> tuple[tuple[ConceptLocalSubgraph, ...], list[MiningDiagnostic]]:
        nodes = {node.id: node for node in state.graph.nodes}
        concept_values: dict[str, set[str]] = defaultdict(set)
        value_concepts: dict[str, set[str]] = defaultdict(set)
        for edge in state.graph.edges:
            if edge.edge_type != "maps_to_value":
                continue
            source = nodes.get(edge.from_node_id)
            target = nodes.get(edge.to_node_id)
            if (
                source
                and target
                and source.node_type == "concept"
                and target.node_type == "dimension_value"
            ):
                concept_values[source.id].add(target.id)
                value_concepts[target.id].add(source.id)

        trigger_members: list[tuple[ConceptTrigger, set[str]]] = []
        for concept_id, value_ids in sorted(concept_values.items()):
            if len(value_ids) >= 2:
                related = tuple(sorted(value_ids))
                trigger_members.append(
                    (
                        ConceptTrigger(
                            kind="concept_maps_to_multiple_values",
                            subject_id=concept_id,
                            related_ids=related,
                        ),
                        {concept_id, *related},
                    )
                )
        for value_id, concept_ids in sorted(value_concepts.items()):
            if len(concept_ids) >= 2:
                related = tuple(sorted(concept_ids))
                trigger_members.append(
                    (
                        ConceptTrigger(
                            kind="value_mapped_by_multiple_concepts",
                            subject_id=value_id,
                            related_ids=related,
                        ),
                        {value_id, *related},
                    )
                )

        components: list[tuple[list[ConceptTrigger], set[str]]] = []
        for trigger, members in trigger_members:
            overlapping = [index for index, (_, ids) in enumerate(components) if ids & members]
            if not overlapping:
                components.append(([trigger], set(members)))
                continue
            first = overlapping[0]
            components[first][0].append(trigger)
            components[first][1].update(members)
            for index in reversed(overlapping[1:]):
                other_triggers, other_members = components.pop(index)
                components[first][0].extend(other_triggers)
                components[first][1].update(other_members)

        historical = {item.node_id: item for item in state.node_conflicts}
        results: list[ConceptLocalSubgraph] = []
        diagnostics: list[MiningDiagnostic] = []
        for triggers, member_ids in components:
            related_columns = {
                edge.to_node_id
                for edge in state.graph.edges
                if edge.edge_type == "value_of" and edge.from_node_id in member_ids
            }
            context_ids = member_ids | related_columns
            concept_ids = {
                node_id
                for node_id in member_ids
                if node_id in nodes and nodes[node_id].node_type == "concept"
            }
            context_ids.update(
                edge.to_node_id
                for edge in state.graph.edges
                if edge.from_node_id in concept_ids
                and edge.edge_type == "maps_to_column"
            )
            edges = tuple(
                sorted(
                    (
                        edge
                        for edge in state.graph.edges
                        if edge.from_node_id in context_ids
                        and edge.to_node_id in context_ids
                        and edge.edge_type
                        in {"maps_to_value", "maps_to_column", "value_of", "broader_than"}
                    ),
                    key=lambda item: (item.edge_type, item.id),
                )
            )
            component_id = _component_id(triggers, context_ids, edges)
            if (
                len(context_ids) > self.max_nodes_per_component
                or len(edges) > self.max_edges_per_component
            ):
                diagnostics.append(
                    MiningDiagnostic(
                        code="MINING_COMPONENT_LIMIT_REACHED",
                        component_id=component_id,
                        message="concept mining component exceeded its size limit",
                    )
                )
                continue
            views = tuple(
                _node_view(nodes[node_id], historical.get(node_id))
                for node_id in sorted(context_ids)
                if node_id in nodes
            )
            results.append(
                ConceptLocalSubgraph(
                    component_id=component_id,
                    triggers=tuple(
                        sorted(triggers, key=lambda item: (item.kind, item.subject_id))
                    ),
                    nodes=views,
                    edges=edges,
                )
            )
        results.sort(key=lambda item: item.component_id)
        if len(results) > self.max_components:
            diagnostics.append(
                MiningDiagnostic(
                    code="MINING_COMPONENT_LIMIT_REACHED",
                    message="concept mining run component limit reached",
                )
            )
            results = results[: self.max_components]
        return tuple(results), diagnostics

    @staticmethod
    def _within_scope(
        subgraph: ConceptLocalSubgraph, operations: GraphRewriteOperations
    ) -> bool:
        node_ids = {node.id for node in subgraph.nodes}
        edge_ids = {edge.id for edge in subgraph.edges}
        temporary_nodes = {node.temporary_id for node in operations.add_nodes}
        temporary_edges = {edge.temporary_id for edge in operations.add_edges}
        if len(temporary_nodes) != len(operations.add_nodes) or len(temporary_edges) != len(
            operations.add_edges
        ):
            return False
        if temporary_nodes & temporary_edges:
            return False
        allowed_refs = node_ids | temporary_nodes
        return all(
            edge.from_ref in allowed_refs and edge.to_ref in allowed_refs
            for edge in operations.add_edges
        ) and set(operations.remove_edge_ids) <= edge_ids


def _node_view(node: GraphNode, conflict: object | None) -> ConceptNodeView:
    alternatives = ()
    if conflict is not None:
        alternatives = tuple(
            ConceptNodeAlternative(
                content=NodeContent.model_validate(alternative.content),
                sources=alternative.sources,
            )
            for alternative in conflict.alternatives
        )
    return ConceptNodeView(
        id=node.id,
        node_type=node.node_type,
        content=NodeContent(
            name=node.name,
            description=node.description,
            aliases=tuple(node.aliases),
            tags=tuple(node.tags),
            properties=node.properties,
        ),
        alternatives=alternatives,
        sources=node.sources,
    )


def _component_id(
    triggers: list[ConceptTrigger], member_ids: set[str], edges: tuple[object, ...]
) -> str:
    payload = {
        "triggers": [item.model_dump(mode="json") for item in triggers],
        "nodes": sorted(member_ids),
        "edges": sorted(edge.id for edge in edges),
    }
    return f"concept-component-{_digest(payload)}"


def _proposal_key(component_id: str, operations: GraphRewriteOperations) -> str:
    payload = operations.model_dump(mode="json")
    return f"concept-proposal-{_digest({'component': component_id, 'operations': payload})}"


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]
