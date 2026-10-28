from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from cortex.application.retrieve_db import RetrieveDbError, TypedRetrievalPath
from cortex.capabilities.retrieve_db.legacy.graph_index import GraphIndex
from cortex.schema import GraphEdge, GraphSpace

_POLICIES: dict[str, dict[str, tuple[frozenset[str], bool]]] = {
    "concept": {
        "grounded_on": (frozenset({"column", "dimension_value"}), True),
        "filters_on": (frozenset({"column"}), True),
    },
    "dimension_value": {"value_of": (frozenset({"column"}), True)},
    "column": {"belongs_to": (frozenset({"table"}), True)},
    "metric": {
        "depends_on": (frozenset({"metric"}), True),
        "grounded_on": (frozenset({"table", "column", "dimension_value"}), True),
        # Explicit immutable physical requirements may coexist with grounded_on.
        "computed_from": (frozenset({"column"}), True),
        "filters_on": (frozenset({"column"}), True),
        "analyzable_by": (frozenset({"column"}), False),
    },
    "generation_rule": {
        "applies_to": (frozenset({"metric", "concept"}), True),
        "grounded_on": (frozenset({"table", "column", "dimension_value"}), True),
    },
}


@dataclass(frozen=True)
class ClosureResult:
    graph: GraphSpace
    dependency_node_ids: tuple[str, ...]
    paths: tuple[TypedRetrievalPath, ...]


class TypedGraphClosure:
    def __init__(
        self, *, max_nodes: int = 50, max_edges: int = 100, metric_depth: int = 4
    ) -> None:
        self.max_nodes = max_nodes
        self.max_edges = max_edges
        self.metric_depth = metric_depth

    def expand(
        self,
        index: GraphIndex,
        selected_node_ids: tuple[str, ...],
        join_candidates: list[dict] | None = None,
    ) -> ClosureResult:
        conflicted_roots = [
            node_id
            for node_id in selected_node_ids
            if index.node_by_id[node_id].merge_status == "conflict"
            and node_id not in index.reference_conflicts
        ]
        if conflicted_roots:
            raise RetrieveDbError(
                "unresolved conflict nodes are not executable: "
                + ", ".join(sorted(conflicted_roots))
            )
        nodes = set(selected_node_ids)
        edges: dict[str, GraphEdge] = {}
        paths: list[TypedRetrievalPath] = []
        for root in selected_node_ids:
            self._expand_root(index, root, nodes, edges, paths)
        if join_candidates is None:
            self._connect_required_tables(index, nodes, edges, paths)
        else:
            self._include_join_paths(index, join_candidates, nodes, edges, paths)
        # Preserve facts between retained endpoints without turning every relation
        # into a dependency traversal policy.
        for edge in index.edge_by_id.values():
            if edge.from_node_id in nodes and edge.to_node_id in nodes:
                self._add(nodes, edges, edge)
        return ClosureResult(
            graph=GraphSpace(
                nodes=tuple(index.node_by_id[node_id] for node_id in sorted(nodes)),
                edges=tuple(edges[edge_id] for edge_id in sorted(edges)),
            ),
            dependency_node_ids=tuple(sorted(nodes - set(selected_node_ids))),
            paths=tuple(paths),
        )

    def _expand_root(
        self,
        index: GraphIndex,
        root: str,
        nodes: set[str],
        edges: dict[str, GraphEdge],
        paths: list[TypedRetrievalPath],
    ) -> None:
        queue = deque([(root, 0)])
        visited: set[tuple[str, str]] = set()
        while queue:
            source_id, metric_depth = queue.popleft()
            source = index.node_by_id[source_id]
            for edge in index.outgoing_edges.get(source_id, ()):
                policy = (
                    (frozenset(n.node_type for n in index.node_by_id.values()), True)
                    if edge.reference_origin is not None
                    else _POLICIES.get(source.node_type, {}).get(edge.edge_type)
                )
                if policy is None:
                    continue
                allowed, required = policy
                target = index.node_by_id.get(edge.to_node_id)
                if target is None or target.node_type not in allowed:
                    raise RetrieveDbError(
                        f"typed closure relation is invalid: {edge.id}"
                    )
                if (
                    target.merge_status == "conflict"
                    and target.id not in index.reference_conflicts
                ):
                    raise RetrieveDbError(
                        f"unresolved conflict dependency is not executable: {target.id}"
                    )
                if not required and not bool(edge.properties.get("is_default_time")):
                    continue
                marker = (root, edge.id)
                if marker in visited:
                    continue
                visited.add(marker)
                next_metric_depth = metric_depth
                if source.node_type == "metric" and target.node_type == "metric":
                    next_metric_depth += 1
                    if next_metric_depth > self.metric_depth:
                        raise RetrieveDbError("metric dependency depth exceeded")
                self._add(nodes, edges, edge)
                paths.append(
                    TypedRetrievalPath(
                        kind=_path_kind(source.node_type),
                        root_node_id=root,
                        node_ids=(source.id, target.id),
                        edge_ids=(edge.id,),
                        required=required,
                        reason=f"typed {source.node_type}.{edge.edge_type} closure",
                    )
                )
                if (
                    target.node_type
                    in {"metric", "concept", "dimension_value", "column"}
                    or target.id in index.reference_conflicts
                ):
                    queue.append((target.id, next_metric_depth))

    def _connect_required_tables(
        self,
        index: GraphIndex,
        nodes: set[str],
        edges: dict[str, GraphEdge],
        paths: list[TypedRetrievalPath],
        anchor_table_ids: set[str] | None = None,
    ) -> None:
        tables = {
            node_id
            for node_id in nodes
            if index.node_by_id[node_id].node_type == "table"
        }
        for edge in index.edge_by_id.values():
            if (
                edge.edge_type != "joins_with"
                or {edge.from_node_id, edge.to_node_id} - tables
            ):
                continue
            if anchor_table_ids is not None and not (
                {edge.from_node_id, edge.to_node_id} & anchor_table_ids
            ):
                continue
            self._include_join_columns(index, edge, nodes, edges)
            self._add(nodes, edges, edge)
            paths.append(
                TypedRetrievalPath(
                    kind="join",
                    root_node_id=edge.from_node_id,
                    node_ids=(edge.from_node_id, edge.to_node_id),
                    edge_ids=(edge.id,),
                    required=True,
                    reason="connect required tables",
                )
            )

    def _include_join_columns(self, index, edge, nodes, edges):
        for pair in edge.properties["column_pairs"]:
            for column_id in pair.values():
                if column_id in index.node_by_id:
                    self._add_node(nodes, column_id)
                    for owner_edge in index.outgoing_edges.get(column_id, ()):
                        if (
                            owner_edge.edge_type == "belongs_to"
                            or owner_edge.reference_origin is not None
                        ) and index.node_by_id[
                            owner_edge.to_node_id
                        ].node_type == "table":
                            self._add(nodes, edges, owner_edge)

    def _include_join_paths(
        self,
        index: GraphIndex,
        candidates: list[dict],
        nodes: set[str],
        edges: dict[str, GraphEdge],
        paths: list[TypedRetrievalPath],
    ) -> None:
        selected_asset_table_ids = {
            node_id
            for node_id in nodes
            if index.node_by_id[node_id].node_type == "table"
        }
        for candidate in candidates:
            edge_ids = tuple(candidate["edge_ids"])
            table_ids = set(candidate["semantic_tables"])
            for edge_id in edge_ids:
                edge = index.edge_by_id[edge_id]
                table_ids.update((edge.from_node_id, edge.to_node_id))
                self._add(nodes, edges, edge)
                self._include_join_columns(index, edge, nodes, edges)
            rule_id = candidate["joinability_rule_id"]
            if rule_id:
                self._add_node(nodes, rule_id)
                for rule_edge in index.outgoing_edges.get(rule_id, ()):
                    if rule_edge.to_node_id in nodes and (
                        rule_edge.reference_origin is not None
                        or rule_edge.edge_type
                        in {
                            "grounded_on",
                            "applies_to",
                        }
                    ):
                        self._add(nodes, edges, rule_edge)
            paths.append(
                TypedRetrievalPath(
                    kind="join",
                    root_node_id=candidate["semantic_tables"][0],
                    node_ids=tuple(sorted(table_ids)),
                    edge_ids=edge_ids,
                    semantic_tables=tuple(candidate["semantic_tables"]),
                    joins=tuple(candidate["joins"]),
                    joinability_rule_id=rule_id,
                    required=False,
                    reason=candidate["kind"] + " join path candidate",
                )
            )
        self._connect_required_tables(
            index, nodes, edges, paths, selected_asset_table_ids
        )

    def _add(
        self, nodes: set[str], edges: dict[str, GraphEdge], edge: GraphEdge
    ) -> None:
        new_nodes = {edge.from_node_id, edge.to_node_id} - nodes
        if len(nodes) + len(new_nodes) > self.max_nodes:
            raise RetrieveDbError("typed closure node budget exceeded")
        if edge.id not in edges and len(edges) >= self.max_edges:
            raise RetrieveDbError("typed closure edge budget exceeded")
        nodes.update(new_nodes)
        edges[edge.id] = edge

    def _add_node(self, nodes: set[str], node_id: str) -> None:
        if node_id not in nodes and len(nodes) >= self.max_nodes:
            raise RetrieveDbError("typed closure node budget exceeded")
        nodes.add(node_id)


def _path_kind(node_type: str) -> str:
    if node_type in {"concept", "dimension_value"}:
        return "concept"
    if node_type == "metric":
        return "metric"
    if node_type == "generation_rule":
        return "rule"
    return "schema"


__all__ = ["ClosureResult", "TypedGraphClosure"]
