"""Small, serial graph mutation boundary shared by Learning tools and workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationError, model_validator

from cortex.application.ontology_graph import GraphRejected, input_issues, issue
from cortex.capabilities.sql_training.core.enums import NodeType, SourceType
from cortex.capabilities.sql_training.graph.nodes import build_node
from cortex.capabilities.sql_training.graph.snapshots import (
    EDITABLE_TYPES,
    SEMANTIC_TYPES,
    GraphSnapshot,
    GraphStore,
    graph_diff,
    relation_contract_change_groups,
    table_schema,
)
from cortex.capabilities.sql_training.graph.values import (
    merge_verified_values,
    verify_value,
)
from cortex.capabilities.sql_training.models import NodeAsset, Source
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.capabilities.sql_training.models.semantic_mapping import (
    ConceptAuthoringProperties,
)
from cortex.capabilities.sql_training.pipeline.grounding.semantic_values import (
    materialize_semantic_mapping_dimension_values,
)
from cortex.infrastructure.database import DatabaseConnector
from cortex.schema import GraphEdge, GraphNode, NodeMergeConflict, SourceReference
from cortex.schema.graph import alternative_content
from cortex.schema.models import EdgeType
from cortex.schema.relation_vocabulary import RETIRED_RELATIONS, require_concrete_relation
from cortex.schema.relationships import is_dependency_edge


class ChangeGroup(NodeModel):
    group_id: str = Field(min_length=1)
    member_ids: list[str] = Field(min_length=1)
    target_ids: list[str] = Field(default_factory=list)


class GraphOperation(NodeModel):
    operation: Literal["create", "update", "revert"]
    node_id: str
    asset: NodeAsset | None = None

    @model_validator(mode="after")
    def check_payload(self):
        if self.operation == "revert":
            if self.asset is not None:
                raise ValueError("revert takes node_id only")
        elif self.asset is None or self.asset.id != self.node_id:
            raise ValueError(
                "create/update requires the full asset with matching node_id"
            )
        return self


class GraphEdit(NodeModel):
    edit_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: str
    operations: list[GraphOperation] = Field(default_factory=list, max_length=50)
    edge_operations: list[dict] = Field(default_factory=list, max_length=50)
    relation_contracts: list[EdgeType] = Field(default_factory=list, max_length=20)
    conflict_operations: list[dict] = Field(default_factory=list, max_length=50)
    revert_edge_ids: list[str] = Field(default_factory=list, max_length=50)
    proposed_groups: list[ChangeGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_changes(self):
        if not (
            self.operations
            or self.edge_operations
            or self.conflict_operations
            or self.revert_edge_ids
        ):
            raise ValueError("at least one node or edge operation is required")
        used = {
            op.get("edge", {}).get("edge_type") for op in self.edge_operations
            if op.get("operation") in {"create_edge", "update_edge"}
        }
        if any(contract.id not in used for contract in self.relation_contracts):
            raise ValueError("submit each relation contract with an authored edge using it")
        return self


def apply_edit(
    store: GraphStore, edit: GraphEdit, *, database: DatabaseConnector, source_id: str
) -> GraphSnapshot:
    if edit.expected_revision != store.working_revision:
        raise GraphRejected(
            [
                issue(
                    "REVISION_MISMATCH",
                    f"stale expected_revision; current={store.working_revision}",
                    "Read the current Working revision and resubmit.",
                    details={"current_revision": store.working_revision},
                )
            ],
            store.working_revision,
        )
    current, accepted = store.working, store.accepted
    by_id = {a.id: a for a in current.assets}
    retained = {a.id: a for a in accepted.assets}
    seen: set[str] = set()
    for op in edit.operations:
        if op.node_id in seen:
            raise ValueError("one operation per node in an edit")
        seen.add(op.node_id)
        existing = by_id.get(op.node_id)
        if op.operation == "revert":
            if existing is None or existing.node_type not in EDITABLE_TYPES:
                raise ValueError("only editable content can be reverted")
            if op.node_id in retained:
                by_id[op.node_id] = retained[op.node_id]
            else:
                del by_id[op.node_id]
            continue
        asset = op.asset
        if asset.node_type not in EDITABLE_TYPES:
            raise GraphRejected(
                [
                    issue(
                        "OPERATION_NOT_ALLOWED",
                        "Learning Agent cannot create or edit physical Tables or Columns",
                        "Use a semantic node or a verified dimension_value.",
                        details={"node_id": asset.id},
                    )
                ]
            )
        if op.operation == "create" and existing is not None:
            raise ValueError("create requires a new ID")
        if op.operation == "update" and existing is None:
            raise ValueError("update requires an existing ID")
        if existing is not None and existing.node_type != asset.node_type:
            raise ValueError("node type and identity are immutable")
        if asset.node_type == NodeType.DIMENSION_VALUE:
            verify_value(asset, list(by_id.values()), database)
        # Attach trustworthy case provenance instead of accepting model-written sources.
        asset = asset.model_copy(
            update={
                "sources": [
                    *(existing.sources if existing else []),
                    Source(source_type=SourceType.QA, source_id=source_id),
                ]
            }
        )
        unique = {(s.source_type, s.source_id): s for s in asset.sources}
        by_id[asset.id] = asset.model_copy(update={"sources": list(unique.values())})
    semantics = [
        by_id[op.node_id] for op in edit.operations if op.operation != "revert"
    ]
    semantics, values = materialize_semantic_mapping_dimension_values(
        semantics,
        table_schema=table_schema(current.assets),
        database=database,
        source_id=source_id,
        existing_values=[a for a in by_id.values() if a.node_type == NodeType.DIMENSION_VALUE],
    )
    values = merge_verified_values(
        [a for a in by_id.values() if a.node_type == NodeType.DIMENSION_VALUE], values
    )
    by_id.update({a.id: a for a in [*semantics, *values]})
    manager = store.manager(current.revision)
    current_nodes = {n.id: n for n in manager.get_snapshot().graph.nodes}
    next_nodes = {asset.id: build_node(asset) for asset in by_id.values()}
    for conflict in current.conflict_nodes:
        if conflict.id in seen:
            raise ValueError(
                "Use conflict_operations to replace or resolve a conflict; retain discards the complete tentative revision"
            )
        next_nodes[conflict.id] = current_nodes[conflict.id]
    operations = [
        {
            "operation": "update_node" if key in current_nodes else "create_node",
            "node": node.model_dump(mode="json"),
        }
        for key, node in next_nodes.items()
        if current_nodes.get(key) != node
    ]
    operations.extend(
        {"operation": "delete_node", "node_id": key}
        for key in current_nodes
        if key not in next_nodes
    )
    conflict_offset = len(operations)
    for index, raw in enumerate(edit.conflict_operations):
        if not isinstance(raw, dict):
            raise GraphRejected(
                [
                    issue(
                        "INVALID_FIELD",
                        "Conflict operation must be an object.",
                        "Use a documented set_conflict or resolve_conflict operation.",
                        path=f"conflict_operations.{index}",
                    )
                ]
            )
        operation = dict(raw)
        kind = operation.get("operation")
        if kind not in {"set_conflict", "resolve_conflict"}:
            raise ValueError(
                "conflict_operations accepts set_conflict or resolve_conflict only"
            )
        if kind == "set_conflict":
            raw_node, raw_conflict = (
                operation.get("node", {}),
                operation.get("conflict", {}),
            )
            node = GraphNode.model_validate(raw_node)
            conflict = NodeMergeConflict.model_validate(raw_conflict)
            if (
                "sources" in raw_node
                or "merge_status" in raw_node
                or any(key in raw_node for key in ("alternatives", "conflict_points", "consolidation_status", "preservation_reasons"))
                or any(
                    "sources" in alternative
                    for alternative in raw_conflict.get("alternatives", [])
                )
            ):
                raise ValueError(
                    "Conflict sources and merge_status are code-managed; omit them"
                )
            previous = next(
                (c for c in current.conflict_nodes if c.id == node.id), None
            )
            prior = (
                {a.alternative_id: a.sources for a in previous.alternatives}
                if previous
                else {}
            )
            source = SourceReference(
                source_id=source_id, locator="QA", case_id=source_id
            )
            from cortex.schema.relationships import _union_sources

            conflict = conflict.model_copy(
                update={
                    "alternatives": tuple(
                        a.model_copy(
                            update={
                                "sources": _union_sources(
                                    prior.get(a.alternative_id, ()), (source,)
                                )
                            }
                        )
                        for a in conflict.alternatives
                    )
                }
            )
            operation["conflict"] = conflict.model_dump(mode="json")
            owner_id = node.id
            # Explicit relations may refer to the newly proposed conflict owner
            # in the same atomic batch, before GraphManager compacts its content.
            next_nodes[owner_id] = node.with_conflict(conflict)
        else:
            owner_id = operation.get("node_id")
            node = current_nodes.get(owner_id)
        if node is not None and node.node_type not in {
            kind.value for kind in SEMANTIC_TYPES
        }:
            raise GraphRejected(
                [
                    issue(
                        "OPERATION_NOT_ALLOWED",
                        "Agent conflict operations require a semantic node.",
                        "Use Concept, Event, Metric or GenerationRule.",
                        path=f"conflict_operations.{index}",
                    )
                ]
            )
        if owner_id in seen:
            raise ValueError(
                "One node operation, including conflict operations, is allowed per edit"
            )
        seen.add(owner_id)
        operations.append(operation)
    current_edges = {e.id: e for e in current.explicit_edges}
    accepted_edges = {e.id: e for e in accepted.explicit_edges}
    edge_operations = [dict(op) for op in edit.edge_operations]
    for edge_id in edit.revert_edge_ids:
        if edge_id in accepted_edges:
            edge_operations.append(
                {
                    "operation": "update_edge"
                    if edge_id in current_edges
                    else "create_edge",
                    "edge": accepted_edges[edge_id].model_dump(mode="json"),
                }
            )
        elif edge_id in current_edges:
            edge_operations.append({"operation": "delete_edge", "edge_id": edge_id})
        else:
            raise ValueError(f"no tentative relation to revert: {edge_id}")
    catalog_ids = {e.id for e in current.catalog_edges}
    for index, op in enumerate(edge_operations):
        kind = op.get("operation")
        if kind not in {"create_edge", "update_edge", "delete_edge"}:
            raise ValueError(
                "edge_operations only accepts create_edge, update_edge or delete_edge"
            )
        expected = {"operation", "edge_id" if kind == "delete_edge" else "edge"}
        if set(op) != expected:
            shape = ('{"operation":"delete_edge","edge_id":"<existing edge ID>"}'
                     if kind == "delete_edge" else
                     '{"operation":"' + kind + '","edge":{...complete edge...}}')
            raise GraphRejected([issue(
                "INVALID_EDGE_OPERATION",
                f"{kind} requires exactly {sorted(expected)}; received {sorted(op)}.",
                f"Use {shape}. Remove all other wrapper fields; rejected edits changed nothing.",
                path=f"edge_operations.{index}",
            )])
        if kind != "delete_edge":
            authored_edge = op.get("edge")
            if (
                index < len(edit.edge_operations)
                and isinstance(authored_edge, dict)
                and (
                    set(authored_edge) & {"reference_origin", "sources"}
                    or isinstance(authored_edge.get("properties"), dict)
                    and "alternative_ids" in authored_edge["properties"]
                )
            ):
                raise ValueError(
                    "Relation reference_origin, sources and alternative_ids are code-managed; omit them from Agent operations"
                )
            try:
                edge = GraphEdge.model_validate(authored_edge)
            except ValidationError as exc:
                raise GraphRejected(input_issues(
                    exc, operation_index=index, path=f"edge_operations.{index}.edge"
                )) from exc
            if index < len(edit.edge_operations) and edge.edge_type in RETIRED_RELATIONS:
                raise GraphRejected([issue(
                    "RETIRED_RELATION", f"{edge.edge_type!r} is read-only legacy vocabulary.",
                    RETIRED_RELATIONS[edge.edge_type], path=f"edge_operations.{index}.edge.edge_type",
                )])
            previous = current_edges.get(edge.id)
            if index < len(edit.edge_operations):
                require_concrete_relation(edge.edge_type)
            sources = list(previous.sources) if previous else []
            source = SourceReference(
                source_id=source_id, locator="QA", case_id=source_id
            )
            if source not in sources:
                sources.append(source)
            if index < len(edit.edge_operations):
                op["edge"] = edge.model_copy(
                    update={"sources": tuple(sources)}
                ).model_dump(mode="json")
            edge_id = edge.id
        else:
            edge_id = op.get("edge_id")
        authored = op.get("edge")
        target_edge = (
            GraphEdge.model_validate(authored)
            if authored
            else current_edges.get(edge_id)
        )
        old_edge = current_edges.get(edge_id)
        for relation in (target_edge, old_edge):
            if relation is None:
                continue
            owner = next_nodes.get(relation.from_node_id) or current_nodes.get(
                relation.from_node_id
            )
            if owner is None:
                raise ValueError("edge source must exist in the candidate graph")
        if edge_id in catalog_ids:
            raise GraphRejected(
                [
                    issue(
                        "OPERATION_NOT_ALLOWED",
                        "Catalog relations are read-only for Learning.",
                        "Edit semantic relations only.",
                        path=f"edge_operations.{index}",
                    )
                ]
            )
    edge_offset = len(operations)
    operations.extend(edge_operations)
    if operations:
        result = manager.apply(
            operations,
            expected_revision=current.revision,
            request_id=edit.edit_id,
            allowed_node_types=None,
            relation_contracts=tuple(edit.relation_contracts),
        )
        if result.status != "applied":
            for error in result.errors:
                if (
                    error.operation_index is not None
                    and conflict_offset <= error.operation_index < edge_offset
                ):
                    index = error.operation_index - conflict_offset
                    operation = edit.conflict_operations[index]
                    owner_id = operation.get("node_id") or operation.get(
                        "node", {}
                    ).get("id")
                    path = error.path
                    for prefix, replacement in (
                        (f"nodes.{owner_id}", "node"),
                        (f"nodes.{owner_id}", "node"),
                    ):
                        if path == prefix or path.startswith(prefix + "."):
                            path = replacement + path[len(prefix) :]
                            break
                    error.path = f"conflict_operations.{index}" + (
                        f".{path}" if path else ""
                    )
                elif (
                    error.operation_index is not None
                    and error.operation_index >= edge_offset
                ):
                    index = error.operation_index - edge_offset
                    operation = edge_operations[index]
                    if index < len(edit.edge_operations):
                        edge_id = operation.get("edge_id") or operation.get(
                            "edge", {}
                        ).get("id")
                        prefix = f"edges.{edge_id}"
                        suffix = (
                            error.path[len(prefix) :]
                            if error.path.startswith(prefix)
                            else ""
                        )
                        error.path = (
                            f"edge_operations.{index}"
                            + (".edge" if "edge" in operation else ".edge_id")
                            + suffix
                        )
                    else:
                        error.path = f"revert_ids.{index - len(edit.edge_operations)}"
            raise GraphRejected(result.errors, current.revision)
    final_state = manager.get_snapshot()
    final = final_state.graph
    # Validate new authoring content before persisting; old snapshots remain readable.
    touched = {op.node_id for op in edit.operations if op.operation != "revert"}
    touched.update(op.get("node_id") or op.get("node", {}).get("id") for op in edit.conflict_operations)
    for node in final.nodes:
        if node.id not in touched or node.node_type != "concept":
            continue
        versions = [alternative_content(node, a) for a in node.alternatives] if node.alternatives else [node]
        for version in versions:
            try:
                ConceptAuthoringProperties(**{k: v for k, v in version.properties.items()
                                               if k in ConceptAuthoringProperties.model_fields})
            except ValidationError as exc:
                raise GraphRejected([issue(
                    "CONCEPT_TARGET_KIND", "Concept targets must match Columns or governed Values; Table bindings are not allowed in new extraction writes.",
                    "Keep table grain/coverage in metadata. Separate independently complete matches; combine fields only when jointly necessary for the same meaning.",
                    path=f"nodes.{node.id}.properties.targets",
                )], current.revision) from exc
    authored_edges = {op["edge"]["id"] for op in edit.edge_operations if op.get("operation") in {"create_edge", "update_edge"}}
    types = {node.id: node.node_type for node in final.nodes}
    for edge in final.edges:
        if (edge.id in authored_edges and edge.edge_type == "grounded_on"
                and types.get(edge.from_node_id) == "concept" and types.get(edge.to_node_id) == "table"):
            raise GraphRejected([issue(
                "CONCEPT_TABLE_BINDING", "Concept grounded_on must not point to Table.",
                "Bind the business meaning to a Column or governed Value; inspect Table metadata for source grain and coverage.",
                path=f"edges.{edge.id}",
            )], current.revision)
    explicit_ids = set(current_edges)
    for operation in edge_operations:
        if operation["operation"] == "delete_edge":
            explicit_ids.discard(operation["edge_id"])
        else:
            explicit_ids.add(operation["edge"]["id"])
    explicit = [edge for edge in final.edges if edge.id in explicit_ids]
    snap = store.save(
        list(by_id.values()),
        parent_revision=current.revision,
        explicit_edges=explicit,
        state=final_state,
    )
    store.restore(snap.revision, store.accepted_revision)
    return snap


def normalize_groups(
    groups: list[ChangeGroup], before: GraphSnapshot, after: GraphSnapshot
) -> list[ChangeGroup]:
    """Merge overlapping targets/nodes and explicit tentative references; no subset solver."""
    changed = set(graph_diff(before, after))
    pending: list[ChangeGroup] = []
    for group in groups:
        members = sorted(set(group.member_ids) & changed)
        if members:
            pending.append(group.model_copy(update={"member_ids": members}))
    uncovered = changed - {key for group in pending for key in group.member_ids}
    if uncovered:
        pending.append(ChangeGroup(group_id="unassigned", member_ids=sorted(uncovered)))
    relations = [*before.graph.edges, *after.graph.edges]
    contract_groups = relation_contract_change_groups(before, after)

    def dependencies(ids):
        related = {
            ref
            for edge in relations
            for ref in (
                (edge.to_node_id,)
                if edge.from_node_id in ids and is_dependency_edge(edge)
                else (edge.from_node_id, edge.to_node_id)
                if edge.id in ids
                else ()
            )
        }
        for affected in contract_groups:
            if affected & ids:
                related.update(affected)
        return related

    merged = True
    while merged:
        merged = False
        for i, left in enumerate(pending):
            left_ids = set(left.member_ids)
            left_refs = dependencies(left_ids)
            for j in range(i + 1, len(pending)):
                right = pending[j]
                right_ids = set(right.member_ids)
                right_refs = dependencies(right_ids)
                if (
                    left_ids & right_ids
                    or set(left.target_ids) & set(right.target_ids)
                    or left_refs & right_ids
                    or right_refs & left_ids
                ):
                    pending[i] = ChangeGroup(
                        group_id=left.group_id,
                        member_ids=sorted(left_ids | right_ids),
                        target_ids=sorted(set(left.target_ids) | set(right.target_ids)),
                    )
                    del pending[j]
                    merged = True
                    break
            if merged:
                break
    # Stable IDs are local to this assessed tentative diff, never long-lived identities.
    return [
        g.model_copy(update={"group_id": f"g{n}"}) for n, g in enumerate(pending, 1)
    ]
