from __future__ import annotations

import hashlib
import json
from typing import Any

from cortex.capabilities.graph_merge.models import _GraphMergeModel
from cortex.capabilities.graph_merge.rewrite.contracts import GraphRewriteProposal
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSchemaError,
    GraphSpace,
    GraphState,
    SchemaDefinition,
    SourceReference,
    effective_concept_grounding,
    validate_graph_state,
)


class GraphRewriteResult(_GraphMergeModel):
    state: GraphState
    diagnostic_codes: tuple[str, ...] = ()


class GraphRewriteApplier:
    def __init__(self, *, allow_grounding_removal: bool = False) -> None:
        self.allow_grounding_removal = allow_grounding_removal

    def apply(
        self,
        state: GraphState,
        proposals: tuple[GraphRewriteProposal, ...],
        schema: SchemaDefinition,
    ) -> GraphRewriteResult:
        if not proposals:
            return GraphRewriteResult(state=state)
        accepted: list[GraphRewriteProposal] = []
        diagnostics: list[str] = []
        removed: set[str] = set()
        child_parents: dict[str, str] = {}
        for proposal in sorted(proposals, key=lambda item: item.proposal_key):
            if not self._valid_proposal(state, proposal, schema):
                diagnostics.append("INVALID_GRAPH_REWRITE_PROPOSAL")
                continue
            proposal_removed = set(proposal.operations.remove_edge_ids)
            hierarchy = {
                edge.to_ref: edge.from_ref
                for edge in proposal.operations.add_edges
                if edge.edge_type == "broader_than"
            }
            if proposal_removed & removed or any(
                child in child_parents and child_parents[child] != parent
                for child, parent in hierarchy.items()
            ):
                diagnostics.append("GRAPH_REWRITE_CONFLICT")
                continue
            removed.update(proposal_removed)
            child_parents.update(hierarchy)
            accepted.append(proposal)
        if not accepted:
            return GraphRewriteResult(state=state, diagnostic_codes=tuple(diagnostics))
        try:
            rewritten = self._materialize(state, tuple(accepted), schema)
            validate_graph_state(rewritten, schema)
        except (GraphSchemaError, ValueError):
            return GraphRewriteResult(
                state=state,
                diagnostic_codes=tuple((*diagnostics, "MINING_STAGE_ROLLED_BACK")),
            )
        return GraphRewriteResult(state=rewritten, diagnostic_codes=tuple(diagnostics))

    def _valid_proposal(
        self,
        state: GraphState,
        proposal: GraphRewriteProposal,
        schema: SchemaDefinition,
    ) -> bool:
        operations = proposal.operations
        if operations.is_empty():
            return False
        node_ids = {node.id for node in state.graph.nodes}
        edge_by_id = {edge.id: edge for edge in state.graph.edges}
        temporary_nodes = {node.temporary_id for node in operations.add_nodes}
        temporary_edges = {edge.temporary_id for edge in operations.add_edges}
        if len(temporary_nodes) != len(operations.add_nodes):
            return False
        if len(temporary_edges) != len(operations.add_edges):
            return False
        if temporary_nodes & temporary_edges:
            return False
        if any(node.node_type != "concept" for node in operations.add_nodes):
            return False
        allowed_edge_types = {"broader_than", "maps_to_value", "maps_to_column"}
        allowed_refs = node_ids | temporary_nodes
        if any(
            edge.edge_type not in allowed_edge_types
            or edge.from_ref not in allowed_refs
            or edge.to_ref not in allowed_refs
            for edge in operations.add_edges
        ):
            return False
        if operations.remove_edge_ids and not self.allow_grounding_removal:
            return False
        if any(
            edge_id not in edge_by_id
            or edge_by_id[edge_id].edge_type not in {"maps_to_value", "maps_to_column"}
            for edge_id in operations.remove_edge_ids
        ):
            return False
        if operations.remove_edge_ids and not _valid_grounding_removals(
            state, proposal
        ):
            return False
        try:
            candidate = self._materialize(state, (proposal,), schema)
            validate_graph_state(candidate, schema)
        except (GraphSchemaError, ValueError):
            return False
        if _has_broader_than_cycle(candidate.graph) or _has_multiple_direct_parents(
            candidate.graph
        ):
            return False
        if operations.remove_edge_ids:
            affected_children = {
                edge_by_id[edge_id].from_node_id
                for edge_id in operations.remove_edge_ids
            }
            if any(
                _effective_grounding_fingerprint(state.graph, child_id)
                != _effective_grounding_fingerprint(candidate.graph, child_id)
                for child_id in affected_children
            ):
                return False
        return True

    def _materialize(
        self,
        state: GraphState,
        proposals: tuple[GraphRewriteProposal, ...],
        schema: SchemaDefinition,
    ) -> GraphState:
        nodes = {node.id: node for node in state.graph.nodes}
        edges = {edge.id: edge for edge in state.graph.edges}
        for proposal in proposals:
            source = SourceReference(
                source_id=f"derived:{proposal.miner_id}:{proposal.proposal_key}",
                locator=proposal.component_id,
            )
            temporary_nodes: dict[str, str] = {}
            for addition in proposal.operations.add_nodes:
                node_id = _derived_node_id(addition.content.name, addition.temporary_id, proposal)
                temporary_nodes[addition.temporary_id] = node_id
                if node_id not in nodes:
                    nodes[node_id] = GraphNode(
                        id=node_id,
                        node_type=addition.node_type,
                        name=addition.content.name,
                        description=addition.content.description,
                        aliases=list(addition.content.aliases),
                        tags=list(addition.content.tags),
                        properties=addition.content.properties,
                        sources=(source,),
                    )
            for edge_id in proposal.operations.remove_edge_ids:
                removed_edge = edges.pop(edge_id, None)
                if removed_edge is not None:
                    _remove_semantic_grounding(nodes, removed_edge)
            for addition in proposal.operations.add_edges:
                from_id = temporary_nodes.get(addition.from_ref, addition.from_ref)
                to_id = temporary_nodes.get(addition.to_ref, addition.to_ref)
                edge_id = _derived_edge_id(
                    addition.edge_type, from_id, to_id, addition.properties
                )
                existing = edges.get(edge_id)
                if existing is None:
                    edges[edge_id] = GraphEdge(
                        id=edge_id,
                        edge_type=addition.edge_type,
                        from_node_id=from_id,
                        to_node_id=to_id,
                        properties=addition.properties,
                        sources=(source,),
                    )
                else:
                    edges[edge_id] = existing.model_copy(
                        update={"sources": _union_sources(existing.sources, (source,))}
                    )
        return GraphState(
            graph=GraphSpace(
                nodes=tuple(sorted(nodes.values(), key=lambda item: (item.node_type, item.id))),
                edges=tuple(sorted(edges.values(), key=lambda item: (item.edge_type, item.id))),
            ),
            node_conflicts=state.node_conflicts,
        )


def _derived_node_id(name: str, temporary_id: str, proposal: GraphRewriteProposal) -> str:
    grounding = sorted(
        (
            edge.edge_type,
            edge.to_ref,
            _canonical(edge.properties),
        )
        for edge in proposal.operations.add_edges
        if edge.from_ref == temporary_id and edge.edge_type in {"maps_to_value", "maps_to_column"}
    )
    digest = _digest({"name": name.strip().casefold(), "grounding": grounding})
    return f"concept.derived.{digest}"


def _remove_semantic_grounding(nodes: dict[str, GraphNode], edge: GraphEdge) -> None:
    node = nodes.get(edge.from_node_id)
    if node is None or node.node_type != "concept":
        return
    targets = node.properties.get("targets")
    if not isinstance(targets, list):
        return
    rewritten = []
    for target in targets:
        if not isinstance(target, dict) or target.get("id") != edge.properties.get("target_id"):
            rewritten.append(target)
            continue
        references = target.get("references")
        if not isinstance(references, list):
            semantic_targets = target.get("semantic_targets")
            if not isinstance(semantic_targets, list):
                rewritten.append(target)
                continue
            remaining_targets = [
                item
                for item in semantic_targets
                if not (
                    isinstance(item, dict)
                    and item.get("node_id") == edge.to_node_id
                    and item.get("relation") == edge.edge_type
                )
            ]
            if remaining_targets:
                rewritten.append({**target, "semantic_targets": remaining_targets})
            continue
        remaining = [item for item in references if item != edge.to_node_id]
        had_value_endpoints = any(
            isinstance(item, str) and item.startswith("dimension_value.")
            for item in references
        )
        value_endpoints = [
            item
            for item in remaining
            if isinstance(item, str) and item.startswith("dimension_value.")
        ]
        column_endpoints = [
            item
            for item in remaining
            if isinstance(item, str) and item.startswith("column.")
        ]
        if value_endpoints or (not had_value_endpoints and column_endpoints):
            rewritten.append({**target, "references": remaining})
    nodes[node.id] = node.model_copy(
        update={"properties": {**node.properties, "targets": rewritten}}
    )


def _derived_edge_id(
    edge_type: str, from_node_id: str, to_node_id: str, properties: dict[str, Any]
) -> str:
    digest = _digest(
        {
            "edge_type": edge_type,
            "from": from_node_id,
            "to": to_node_id,
            "properties": properties,
        }
    )
    return f"edge.derived.{digest}"


def _has_broader_than_cycle(graph: GraphSpace) -> bool:
    children: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.edge_type == "broader_than":
            children.setdefault(edge.from_node_id, []).append(edge.to_node_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        if any(visit(child) for child in children.get(node_id, ())):
            return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in children)


def _has_multiple_direct_parents(graph: GraphSpace) -> bool:
    parents: dict[str, set[str]] = {}
    for edge in graph.edges:
        if edge.edge_type == "broader_than":
            parents.setdefault(edge.to_node_id, set()).add(edge.from_node_id)
    return any(len(values) > 1 for values in parents.values())


def _valid_grounding_removals(
    state: GraphState, proposal: GraphRewriteProposal
) -> bool:
    operations = proposal.operations
    edge_by_id = {edge.id: edge for edge in state.graph.edges}
    removed_ids = set(operations.remove_edge_ids)
    removed_edges = [edge_by_id[edge_id] for edge_id in removed_ids]

    for edge in removed_edges:
        parent_refs = {
            candidate.from_ref
            for candidate in operations.add_edges
            if candidate.edge_type == "broader_than"
            and candidate.to_ref == edge.from_node_id
        }
        parent_refs.update(
            candidate.from_node_id
            for candidate in state.graph.edges
            if candidate.edge_type == "broader_than"
            and candidate.to_node_id == edge.from_node_id
        )
        if len(parent_refs) != 1:
            return False
        parent_ref = next(iter(parent_refs))
        existing_replacement = any(
            candidate.edge_type == edge.edge_type
            and candidate.from_node_id == parent_ref
            and _same_grounding_target(state, candidate.to_node_id, edge.to_node_id)
            for candidate in state.graph.edges
        )
        proposed_replacement = any(
            candidate.edge_type == edge.edge_type
            and candidate.from_ref == parent_ref
            and candidate.to_ref == edge.to_node_id
            for candidate in operations.add_edges
        )
        if not (existing_replacement or proposed_replacement):
            return False
    return True


def _same_grounding_target(state: GraphState, left_id: str, right_id: str) -> bool:
    if left_id == right_id:
        return True
    nodes = {node.id: node for node in state.graph.nodes}
    left = nodes.get(left_id)
    right = nodes.get(right_id)
    if (
        left is None
        or right is None
        or left.node_type != "dimension_value"
        or right.node_type != "dimension_value"
    ):
        return False
    left_value = left.properties.get("value", left.name)
    right_value = right.properties.get("value", right.name)
    return str(left_value).strip().casefold() == str(right_value).strip().casefold()


def _effective_grounding_fingerprint(
    graph: GraphSpace, concept_id: str
) -> frozenset[tuple[str, str]]:
    nodes = {node.id: node for node in graph.nodes}

    def semantic_target(edge: GraphEdge) -> str:
        target = nodes.get(edge.to_node_id)
        if target is not None and target.node_type == "dimension_value":
            value = target.properties.get("value", target.name)
            return f"dimension_value:{str(value).strip().casefold()}"
        return edge.to_node_id

    return frozenset(
        (
            edge.edge_type,
            semantic_target(edge),
        )
        for edge in effective_concept_grounding(graph, concept_id)
    )


def _union_sources(
    left: tuple[SourceReference, ...], right: tuple[SourceReference, ...]
) -> tuple[SourceReference, ...]:
    values = {
        (item.source_id, item.chunk_id, item.locator, item.case_id): item
        for item in (*left, *right)
    }
    keys = sorted(values, key=lambda item: tuple(x or "" for x in item))
    return tuple(values[key] for key in keys)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()[:20]
