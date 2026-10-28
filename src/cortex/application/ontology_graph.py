"""One transactional graph boundary for deterministic producers and agents.

No LLM, authoring projection, or workflow acceptance lives here.  Repositories
may publish an accepted candidate using ``commit`` before its head advances.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    MergeStatus,
    SchemaDefinition,
    validate_graph_state,
)
from cortex.schema.graph import (
    ConflictAlternative,
    NodeContent,
    NodeMergeConflict,
    alternative_content,
)
from cortex.schema.references import property_reference_ids
from cortex.schema.relationships import (
    edge_identity_key,
    is_dependency_edge,
    reference_relation_type,
    synchronize_reference_edges,
)
from cortex.schema.semantic_validation import validate_semantic_properties
from cortex.schema.validation import (
    GraphSchemaError,
    _validate_physical_join,
    _validate_properties,
    validate_incoming_graph_state,
)


class GraphIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    hint: str
    operation_index: int | None = None
    path: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class GraphResult(BaseModel):
    status: Literal["applied", "rejected", "error"]
    revision: str
    errors: list[GraphIssue] = Field(default_factory=list)
    changed_node_ids: list[str] = Field(default_factory=list)
    state: GraphState | None = Field(default=None, exclude=True)


class GraphRejected(ValueError):
    """Deterministic callers may raise; agent tools return the same issues."""

    def __init__(self, issues: list[GraphIssue], revision: str = ""):
        self.issues, self.revision = issues, revision
        super().__init__("; ".join(issue.message for issue in issues))

    def feedback(self) -> dict:
        return GraphResult(
            status="rejected", revision=self.revision, errors=self.issues
        ).model_dump(mode="json")


def issue(code, message, hint, *, path="", details=None, operation_index=None):
    return GraphIssue(
        code=code,
        message=message,
        hint=hint,
        path=path,
        details=details or {},
        operation_index=operation_index,
    )


def input_issues(exc: ValidationError, *, operation_index=None, path=""):
    return [
        issue(
            "INVALID_FIELD",
            error["msg"],
            "Read get_schema/type guide and correct this field before retrying.",
            path=".".join(filter(None, (path, ".".join(map(str, error["loc"]))))),
            operation_index=operation_index,
            details={"type": error["type"]},
        )
        for error in exc.errors(include_input=False, include_context=False)
    ]


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _validated_state(state: GraphState) -> GraphState:
    """Keep graph validation failures within the public structured-error contract."""
    try:
        return GraphState.model_validate(state.model_dump(mode="json"))
    except ValidationError as exc:
        # Pydantic can flatten errors raised while restoring a candidate. Retain
        # the graph owner and branch so callers can repair the exact alternative.
        for node in state.graph.nodes:
            for index, alternative in enumerate(node.alternatives):
                try:
                    alternative_content(node, alternative)
                except ValidationError as branch_error:
                    raise GraphRejected(
                        input_issues(
                            branch_error,
                            path=f"nodes.{node.id}.alternatives.{index}.overrides",
                        )
                    ) from exc
        raise GraphRejected(input_issues(exc)) from exc


class GraphManager:
    def __init__(
        self,
        schema: SchemaDefinition,
        state: GraphState | None = None,
        *,
        revision: str | None = None,
    ):
        self.schema = schema.model_copy(deep=True)
        self._state = (state or GraphState(graph=GraphSpace())).model_copy(deep=True)
        self.revision = revision or _fingerprint(self._state.model_dump(mode="json"))
        self._receipts: dict[str, tuple[str, GraphResult]] = {}
        self._lock = RLock()
        if state is not None:
            self.validate_state(self._state)

    @staticmethod
    def resolve_alternative(
        node: GraphNode, alternative: ConflictAlternative
    ) -> NodeContent:
        """Restore one candidate. Complete graph validation runs at the state boundary."""
        return alternative_content(node, alternative)

    @staticmethod
    def compact_conflict(
        node,
        candidates,
        *,
        consolidation_status="unresolved",
        preservation_reasons=(),
        conflict_points=(),
    ):
        """Factor complete candidates into common node fields and whole-field overrides.

        candidates are (alternative_id, NodeContent, sources) tuples.
        """
        candidates = [
            (key, NodeContent.model_validate(content), sources)
            for key, content, sources in candidates
        ]
        if len(candidates) < 2:
            raise ValueError("a conflict requires at least two distinct candidates")
        contents = [content.model_dump(mode="json") for _, content, _ in candidates]
        common = dict(contents[0])
        common["properties"] = {}
        overrides = [{} for _ in candidates]
        for field in ("name", "description", "aliases", "tags"):
            values = [c[field] for c in contents]
            if any(value != values[0] for value in values[1:]):
                common[field] = {
                    "name": node.id,
                    "description": None,
                    "aliases": [],
                    "tags": [],
                }[field]
                for supplied, value in zip(overrides, values, strict=True):
                    supplied[field] = value
        keys = set().union(*(c["properties"] for c in contents))
        for key in sorted(keys):
            values = [c["properties"].get(key) for c in contents]
            if all(value == values[0] for value in values[1:]):
                if values[0] is not None:
                    common["properties"][key] = values[0]
            else:
                for supplied, value in zip(overrides, values, strict=True):
                    supplied[f"properties.{key}"] = value
        from cortex.schema.relationships import _union_sources

        compact_node = node.model_copy(
            update={
                **common,
                "sources": _union_sources(*(sources for _, _, sources in candidates)),
                "merge_status": MergeStatus.CONFLICT,
            }
        )
        conflict = NodeMergeConflict(
            node_id=node.id,
            consolidation_status=consolidation_status,
            preservation_reasons=preservation_reasons,
            conflict_points=conflict_points,
            alternatives=tuple(
                ConflictAlternative(
                    alternative_id=key, overrides=changes, sources=sources
                )
                for (key, _, sources), changes in zip(
                    candidates, overrides, strict=True
                )
            ),
        )
        return compact_node.with_conflict(conflict), conflict

    def get_snapshot(self) -> GraphState:
        with self._lock:
            return self._state.model_copy(deep=True)

    def get_schema(self, node_type=None, edge_type=None):
        if node_type is not None:
            spec = self.schema.node_type_index().get(node_type)
            if spec is None:
                raise KeyError(node_type)
            return spec.model_dump(mode="json")
        if edge_type is not None:
            spec = self.schema.edge_type_index().get(edge_type)
            if spec is None:
                raise KeyError(edge_type)
            return spec.model_dump(mode="json")
        return self.schema.model_dump(mode="json")

    def get_node(self, node_id, *, alternative_id=None):
        with self._lock:
            if alternative_id is not None:
                node = self.get_node(node_id)
                conflict = self.get_conflict(node_id)
                alternative = (
                    next(
                        (
                            a
                            for a in conflict.alternatives
                            if a.alternative_id == alternative_id
                        ),
                        None,
                    )
                    if conflict
                    else None
                )
                if node is None or alternative is None:
                    raise KeyError((node_id, alternative_id))
                content = self.resolve_alternative(node, alternative)
                return GraphNode(
                    id=node.id,
                    node_type=node.node_type,
                    **content.model_dump(mode="json"),
                    sources=alternative.sources,
                )
            return next(
                (
                    n.model_copy(deep=True)
                    for n in self._state.graph.nodes
                    if n.id == node_id
                ),
                None,
            )

    def get_conflict(self, node_id):
        """Return common-node overrides; full candidates are an explicit get_node option."""
        with self._lock:
            return next(
                (
                    c.model_copy(deep=True)
                    for c in self._state.conflict_nodes
                    if c.id == node_id
                ),
                None,
            )

    def list_nodes(self, node_type=None, *, offset=0, limit=20):
        if offset < 0 or limit < 1:
            raise ValueError("offset >= 0 and limit >= 1 required")
        with self._lock:
            nodes = [
                n
                for n in self._state.graph.nodes
                if node_type is None or n.node_type == node_type
            ]
            return [n.model_copy(deep=True) for n in nodes[offset : offset + limit]]

    def get_edges(self, node_id=None, edge_type=None, *, direction="out"):
        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be out, in or both")
        with self._lock:
            return [
                e.model_copy(deep=True)
                for e in self._state.graph.edges
                if (edge_type is None or e.edge_type == edge_type)
                and (
                    node_id is None
                    or (direction != "in" and e.from_node_id == node_id)
                    or (direction != "out" and e.to_node_id == node_id)
                )
            ]

    def get_dependencies(self, node_id):
        with self._lock:
            node = self.get_node(node_id)
            if node is None:
                raise KeyError(node_id)
            return sorted(
                {
                    edge.to_node_id
                    for edge in self._state.graph.edges
                    if edge.from_node_id == node_id and is_dependency_edge(edge)
                }
            )

    def get_dependents(self, node_id):
        with self._lock:
            if self.get_node(node_id) is None:
                raise KeyError(node_id)
            return sorted(
                {
                    edge.from_node_id
                    for edge in self._state.graph.edges
                    if edge.to_node_id == node_id
                    or any(
                        node_id in pair.values()
                        for pair in edge.properties.get("column_pairs", ())
                    )
                }
            )

    @staticmethod
    def _references(properties):
        return property_reference_ids(properties)

    @staticmethod
    def _node_references(node, nodes, properties=None):
        refs = property_reference_ids(
            node.properties if properties is None else properties
        )
        if node.node_type == "table":
            columns = {
                n.properties.get("column_name"): n.id
                for n in nodes.values()
                if n.node_type == "column" and n.properties.get("table_ref") == node.id
            }
            refs = {columns.get(ref, ref) for ref in refs}
        return refs

    def validate_state(self, state: GraphState) -> GraphState:
        """Check a stored state without repairing or changing its facts."""
        state = _validated_state(state)
        self._check_content(state)
        self._check_identities(state)
        self._check_edges(state)
        try:
            validate_graph_state(state, self.schema)
        except (ValueError, TypeError, KeyError) as exc:
            raise GraphRejected(
                [
                    issue(
                        "INVALID_GRAPH",
                        str(exc),
                        "Correct the stored graph before use.",
                    )
                ]
            ) from exc
        self._check_cycles(state)
        # Validate persistence completeness, without inferring relation names or
        # generating replacement edges during reads.
        from cortex.schema.references import content_reference_ids

        expected = {}
        for owner in state.graph.nodes:
            conflict = next((c for c in state.conflict_nodes if c.id == owner.id), None)
            branches = (
                [
                    (self.resolve_alternative(owner, a).properties, a.alternative_id)
                    for a in conflict.alternatives
                ]
                if conflict
                else [(owner.properties, None)]
            )
            for properties, alternative_id in branches:
                if owner.node_type == "concept":
                    anchors = [
                        (ref, target["id"])
                        for target in properties.get("targets", ())
                        for ref in target.get("references", ())
                    ]
                elif owner.node_type in {"column", "dimension_value"}:
                    field = "table_ref" if owner.node_type == "column" else "column_ref"
                    anchors = (
                        [(properties[field], None)] if properties.get(field) else []
                    )
                elif owner.node_type in {"metric", "generation_rule"}:
                    anchors = [(ref, None) for ref in content_reference_ids(properties)]
                else:
                    anchors = []
                for ref, target_id in anchors:
                    expected.setdefault((owner.id, ref, target_id), set()).add(
                        alternative_id
                    )
        actual = {}
        for edge in state.graph.edges:
            if edge.reference_origin is None:
                continue
            slot = (
                edge.from_node_id,
                edge.to_node_id,
                edge.properties.get("target_id"),
            )
            branches = set(edge.properties.get("alternative_ids", ())) or {None}
            if slot not in expected or not branches <= expected[slot]:
                raise GraphRejected(
                    [
                        issue(
                            "INCONSISTENT_DERIVED_EDGES",
                            "Stored relation has no matching reference anchor or branch.",
                            "Publish complete relations through GraphManager's write boundary.",
                            path=f"edges.{edge.id}",
                        )
                    ]
                )
            for branch in branches:
                actual.setdefault((slot, branch), set()).add(edge.reference_origin)
        for slot, branches in expected.items():
            if any(
                actual.get((slot, branch), set()) not in ({"default"}, {"explicit"})
                for branch in branches
            ):
                raise GraphRejected(
                    [
                        issue(
                            "INCONSISTENT_DERIVED_EDGES",
                            "Stored relations disagree with reference anchors.",
                            "Publish complete relations through GraphManager's write boundary.",
                            path=f"nodes.{slot[0]}",
                        )
                    ]
                )
        return state.model_copy(deep=True)

    def prepare_state(
        self,
        state: GraphState,
        *,
        node_ids: set[str] | None = None,
        strict: bool = False,
    ) -> tuple[GraphState, tuple[str, ...]]:
        """Shared generation/merge boundary. No publication and no caller mutation.

        CRUD writes request the same current content contract and additional
        authoring requirements for changed nodes.
        """
        state = _validated_state(state)
        if node_ids is not None:
            node_ids = set(node_ids)
            node_ids.update(
                n.id
                for n in state.graph.nodes
                if n.node_type == "column" and n.properties.get("table_ref") in node_ids
            )
        self._check_content(state, node_ids=node_ids, strict=strict)
        self._check_identities(state)
        edges, dropped = synchronize_reference_edges(
            state.graph.edges,
            state.graph.nodes,
            schema=self.schema,
            node_ids=node_ids,
        )
        candidate = state.model_copy(
            update={"graph": GraphSpace(nodes=state.graph.nodes, edges=edges)}
        )
        self._check_edges(candidate)
        try:
            validate_graph_state(candidate, self.schema)
        except (ValueError, TypeError, KeyError) as exc:
            raise GraphRejected(
                [
                    issue(
                        "INVALID_GRAPH",
                        str(exc),
                        "Correct the graph fields, endpoints or conflict overlay and retry.",
                    )
                ]
            ) from exc
        self._check_cycles(candidate)
        return candidate, dropped

    def prepare_fragment(
        self, state: GraphState, *, existing: GraphSpace | GraphState
    ) -> GraphState:
        """Generate and validate incoming edges against read-only existing endpoints."""
        state = _validated_state(state)
        existing = existing.graph if isinstance(existing, GraphState) else existing
        owners = {n.id for n in state.graph.nodes}
        if owners & {n.id for n in existing.nodes}:
            raise GraphRejected(
                [
                    issue(
                        "DUPLICATE_NODE",
                        "Incoming node IDs overlap existing nodes.",
                        "Allocate distinct incoming IDs before integration.",
                    )
                ]
            )
        overlay = GraphState(
            graph=GraphSpace(
                nodes=(*existing.nodes, *state.graph.nodes),
                edges=state.graph.edges,
            ),
        )
        self._check_content(overlay, node_ids=owners)
        edges, _ = synchronize_reference_edges(
            state.graph.edges,
            overlay.graph.nodes,
            schema=self.schema,
            node_ids=owners,
        )
        candidate = state.model_copy(
            update={"graph": GraphSpace(nodes=state.graph.nodes, edges=edges)}
        )
        checked = overlay.model_copy(
            update={"graph": GraphSpace(nodes=overlay.graph.nodes, edges=edges)}
        )
        self._check_edges(checked)
        try:
            validate_incoming_graph_state(candidate, self.schema, existing=existing)
        except (ValueError, TypeError, KeyError) as exc:
            raise GraphRejected(
                [
                    issue(
                        "INVALID_GRAPH",
                        str(exc),
                        "Correct incoming fields and references and retry.",
                    )
                ]
            ) from exc
        self._check_cycles(checked)
        return candidate

    def _check_identities(self, state):
        identities = {}
        for node in state.graph.nodes:
            policy = self.schema.node_type_index()[node.node_type].identity_policy
            if policy.mode != "exact":
                continue
            values = [
                node.properties.get(field.removeprefix("properties."))
                if field.startswith("properties.")
                else getattr(node, field, None)
                for field in policy.fields
            ]
            key = (node.node_type, json.dumps(values, sort_keys=True))
            if key in identities:
                raise GraphRejected(
                    [
                        issue(
                            "DUPLICATE_NODE_IDENTITY",
                            "Different node IDs declare the same exact identity.",
                            "Use the existing physical node or integrate the incoming candidates through Merge.",
                            path=f"nodes.{node.id}",
                            details={
                                "node_id": node.id,
                                "existing_node_id": identities[key],
                            },
                        )
                    ]
                )
            identities[key] = node.id

    def _check_edges(self, state):
        nodes, specs = (
            {n.id: n for n in state.graph.nodes},
            self.schema.edge_type_index(),
        )
        errors = []
        identities = {}
        alternatives = {
            c.id: {a.alternative_id for a in c.alternatives}
            for c in state.conflict_nodes
        }
        for edge in state.graph.edges:
            path = f"edges.{edge.id}"
            if not edge.id.strip():
                errors.append(
                    issue(
                        "INVALID_FIELD",
                        "Edge ID must be nonempty.",
                        "Supply a stable edge ID.",
                        path=path,
                    )
                )
            spec = specs.get(edge.edge_type)
            if not edge.edge_type.strip():
                errors.append(
                    issue(
                        "INVALID_FIELD",
                        "Relation name must be nonempty.",
                        "Provide an edge_type.",
                        path=f"{path}.edge_type",
                    )
                )
            try:
                json.dumps(edge.properties, allow_nan=False)
            except (TypeError, ValueError):
                errors.append(
                    issue(
                        "INVALID_FIELD",
                        "Relation properties must be finite JSON values.",
                        "Use JSON-compatible values.",
                        path=f"{path}.properties",
                    )
                )
            for reference, allowed, field in (
                (
                    edge.from_node_id,
                    spec.from_node_types
                    if spec
                    else tuple(n.node_type for n in nodes.values()),
                    "from_node_id",
                ),
                (
                    edge.to_node_id,
                    spec.to_node_types
                    if spec
                    else tuple(n.node_type for n in nodes.values()),
                    "to_node_id",
                ),
            ):
                target = nodes.get(reference)
                if target is None:
                    errors.append(
                        issue(
                            "REFERENCE_NOT_FOUND",
                            f"Edge endpoint {reference!r} does not exist.",
                            "Use an existing node ID or create it in this batch.",
                            path=f"{path}.{field}",
                            details={"reference": reference},
                        )
                    )
                elif not any(
                    self.schema.is_node_type_compatible(target.node_type, kind)
                    for kind in allowed
                ):
                    errors.append(
                        issue(
                            "INVALID_ENDPOINT_TYPE",
                            f"Endpoint {reference!r} has type {target.node_type!r}.",
                            "Use an endpoint of an allowed type.",
                            path=f"{path}.{field}",
                            details={
                                "reference": reference,
                                "actual_type": target.node_type,
                                "allowed_types": allowed,
                            },
                        )
                    )
            try:
                if spec:
                    _validate_properties(
                        {
                            k: v
                            for k, v in edge.properties.items()
                            if k not in {"target_id", "description", "alternative_ids"}
                            or k in spec.fields
                        },
                        spec.fields,
                        path,
                    )
            except GraphSchemaError as exc:
                errors.append(
                    issue(
                        "INVALID_FIELD",
                        str(exc),
                        "Read the relation schema and correct its properties.",
                        path=f"{path}.properties",
                    )
                )
                continue
            if (
                edge.reference_origin == "default"
                and edge.from_node_id in nodes
                and edge.to_node_id in nodes
            ):
                try:
                    expected_type = reference_relation_type(
                        nodes[edge.from_node_id].node_type,
                        nodes[edge.to_node_id].node_type,
                    )
                    if edge.edge_type != expected_type:
                        raise GraphSchemaError(
                            "Default relation name does not match its endpoint types"
                        )
                except GraphSchemaError as exc:
                    errors.append(
                        issue(
                            "INCONSISTENT_DERIVED_EDGES",
                            str(exc),
                            "Write explicit relations through GraphManager; do not label them as defaults.",
                            path=path,
                        )
                    )
            if "alternative_ids" in edge.properties and (
                not isinstance(edge.properties["alternative_ids"], list)
                or not edge.properties["alternative_ids"]
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in edge.properties["alternative_ids"]
                )
                or (
                    all(isinstance(v, str) for v in edge.properties["alternative_ids"])
                    and (
                        len(set(edge.properties["alternative_ids"]))
                        != len(edge.properties["alternative_ids"])
                        or not set(edge.properties["alternative_ids"])
                        <= alternatives.get(edge.from_node_id, set())
                    )
                )
            ):
                errors.append(
                    issue(
                        "INVALID_FIELD",
                        "alternative_ids must contain nonempty strings.",
                        "Omit branch metadata; code generates it.",
                        path=f"{path}.properties.alternative_ids",
                    )
                )
            if (
                edge.edge_type in {"belongs_to", "value_of"}
                and edge.from_node_id in nodes
            ):
                field = "table_ref" if edge.edge_type == "belongs_to" else "column_ref"
                if nodes[edge.from_node_id].properties.get(field) != edge.to_node_id:
                    errors.append(
                        issue(
                            "INVALID_OWNER_REFERENCE",
                            "This ownership relation disagrees with the physical owner.",
                            "Use the declared owner or a custom relation for another fact.",
                            path=path,
                        )
                    )
            if "target_id" in edge.properties:
                owner = nodes.get(edge.from_node_id)
                branches = [owner.properties] if owner else []
                branches.extend(
                    self.resolve_alternative(owner, a).properties
                    for c in state.conflict_nodes
                    if owner and c.id == owner.id
                    for a in c.alternatives
                )
                if owner and (
                    owner.node_type != "concept"
                    or not any(
                        t.get("id") == edge.properties["target_id"]
                        for props in branches
                        for t in props.get("targets", ())
                    )
                ):
                    errors.append(
                        issue(
                            "INVALID_TARGET_REFERENCE",
                            "target_id must identify a declared Concept target.",
                            "Use an existing Concept target ID or omit target_id for an independent relation.",
                            path=f"{path}.properties.target_id",
                        )
                    )
            if edge.edge_type == "grounded_on" and edge.from_node_id in nodes:
                owner = nodes[edge.from_node_id]
                target_id = edge.properties.get("target_id")
                if owner.node_type == "concept":
                    branches = [owner.properties]
                    branches.extend(
                        self.resolve_alternative(owner, a).properties
                        for c in state.conflict_nodes
                        if c.id == owner.id
                        for a in c.alternatives
                    )
                    targets = [
                        t for props in branches for t in props.get("targets", ())
                    ]
                    if not isinstance(target_id, str) or not any(
                        t.get("id") == target_id for t in targets
                    ):
                        errors.append(
                            issue(
                                "INVALID_TARGET_REFERENCE",
                                "Concept grounded_on must match a declared target and reference.",
                                "Update the Concept target through GraphManager.",
                                path=f"{path}.properties.target_id",
                            )
                        )
                    if nodes.get(edge.to_node_id) and nodes[
                        edge.to_node_id
                    ].node_type not in {"column", "dimension_value"}:
                        errors.append(
                            issue(
                                "INVALID_ENDPOINT_TYPE",
                                "Concept grounding requires a Column or governed Value.",
                                "Use a declared target physical reference.",
                                path=path,
                            )
                        )
                elif "target_id" in edge.properties:
                    errors.append(
                        issue(
                            "INVALID_FIELD",
                            "Only Concept grounding accepts target_id.",
                            "Remove target_id from Metric or GenerationRule dependencies.",
                            path=f"{path}.properties.target_id",
                        )
                    )
            try:
                key = edge_identity_key(edge, spec)
                # Distinct property variants are retained by Merge as evidence.
                # Identical facts and joins with the same complete key are duplicates.
                if edge.edge_type != "joins_with":
                    key = (key, json.dumps(edge.properties, sort_keys=True))
                if key in identities:
                    errors.append(
                        issue(
                            "DUPLICATE_EDGE",
                            "Different edge IDs assert the same relation.",
                            "Update or reuse the existing edge instead of creating another ID.",
                            path=path,
                            details={"existing_edge_id": identities[key]},
                        )
                    )
                identities[key] = edge.id
            except (ValueError, TypeError, KeyError):
                pass  # Detailed field/JOIN feedback is supplied below.
            if edge.id in nodes:
                errors.append(
                    issue(
                        "INVALID_FIELD",
                        "Node and edge IDs must be distinct.",
                        "Choose a distinct edge ID.",
                        path=path,
                    )
                )
            if edge.edge_type == "joins_with":
                try:
                    _validate_physical_join(edge, nodes)
                except (GraphSchemaError, TypeError, KeyError) as exc:
                    errors.append(
                        issue(
                            "INVALID_JOIN_COLUMNS",
                            str(exc),
                            "Supply complete Column pairs belonging to their table endpoints.",
                            path=f"{path}.properties.column_pairs",
                        )
                    )
        if errors:
            raise GraphRejected(errors[:20], self.revision)

    def _check_content(self, state, *, node_ids=None, strict=False):
        nodes = {n.id: n for n in state.graph.nodes}
        specs = self.schema.node_type_index()
        conflicts = {c.id: c for c in state.conflict_nodes}
        errors = []
        for node in state.graph.nodes:
            if node.node_type not in specs:
                errors.append(
                    issue(
                        "UNKNOWN_NODE_TYPE",
                        f"Unknown node type {node.node_type!r}.",
                        "Use a declared node type.",
                        details={"node_id": node.id},
                    )
                )
                continue
            if (
                strict
                and (node_ids is None or node.id in node_ids)
                and node.node_type
                not in {
                    "table",
                    "column",
                    "dimension_value",
                    "concept",
                    "metric",
                    "generation_rule",
                }
            ):
                errors.append(
                    issue(
                        "UNKNOWN_NODE_TYPE",
                        f"Type {node.node_type!r} is not a writable NL2SQL node type.",
                        "Use one of the six NL2SQL node types; examples belong in provenance storage.",
                        path=f"nodes.{node.id}.node_type",
                        details={"node_id": node.id},
                    )
                )
                continue
            if not node.id.strip() or not node.name.strip():
                errors.append(
                    issue(
                        "INVALID_FIELD",
                        "Node ID and name must be nonempty.",
                        "Supply a stable ID and a meaningful name.",
                        path=f"nodes.{node.id}",
                    )
                )
                continue
            try:
                _validate_properties(
                    node.properties,
                    {
                        key: field.model_copy(update={"required": False})
                        for key, field in self.schema.effective_node_fields(
                            node.node_type
                        ).items()
                    }
                    if node.id in conflicts
                    else self.schema.effective_node_fields(node.node_type),
                    f"node {node.id!r}",
                )
            except GraphSchemaError as exc:
                errors.append(
                    issue(
                        "INVALID_FIELD",
                        str(exc),
                        "Read the type guide and correct the fields.",
                        path=f"nodes.{node.id}.properties",
                    )
                )
                continue
            owner = {"column": "table_ref", "dimension_value": "column_ref"}.get(
                node.node_type
            )
            if owner and (
                not isinstance(node.properties.get(owner), str)
                or not node.properties[owner].strip()
            ):
                errors.append(
                    issue(
                        "INVALID_FIELD",
                        f"{node.node_type} requires {owner}.",
                        "Supply its actual physical owner ID.",
                        path=f"nodes.{node.id}.properties.{owner}",
                        details={"node_id": node.id},
                    )
                )
                continue
            try:
                branches = (
                    [
                        self.resolve_alternative(node, a).properties
                        for a in conflicts[node.id].alternatives
                    ]
                    if node.id in conflicts
                    else [node.properties]
                )
            except ValueError as exc:
                errors.append(
                    issue(
                        "INVALID_FIELD",
                        str(exc),
                        "Correct candidate overrides so every restored definition is complete.",
                        path=f"nodes.{node.id}.alternatives",
                    )
                )
                continue
            for branch_index, properties in enumerate(branches):
                branch_path = (
                    f"nodes.{node.id}.alternatives.{branch_index}.overrides"
                    if node.id in conflicts
                    else f"nodes.{node.id}.properties"
                )
                if node_ids is None or node.id in node_ids:
                    try:
                        _validate_properties(
                            properties,
                            self.schema.effective_node_fields(node.node_type),
                            f"candidate of {node.id!r}",
                        )
                        validate_semantic_properties(node.node_type, properties)
                    except (ValueError, TypeError, KeyError) as exc:
                        errors.append(
                            issue(
                                "INVALID_FIELD",
                                str(exc),
                                "Read this node type's schema and correct the complete content.",
                                path=branch_path,
                            )
                        )
                        continue
                allowed = {
                    "metric": {"metric", "table", "column", "dimension_value"},
                    "concept": {"column", "dimension_value"},
                    "generation_rule": {
                        "concept",
                        "metric",
                        "table",
                        "column",
                        "dimension_value",
                    },
                    "table": {"column"},
                    "column": {"table"},
                    "dimension_value": {"column"},
                }.get(node.node_type)
                for ref in sorted(self._node_references(node, nodes, properties)):
                    target = nodes.get(ref)
                    if target is None:
                        errors.append(
                            issue(
                                "REFERENCE_NOT_FOUND",
                                f"Node {node.id!r} has unresolved reference {ref!r}.",
                                "Read the graph for an existing ID or create the complete dependency in this batch.",
                                path=f"nodes.{node.id}.properties",
                                details={"node_id": node.id, "reference": ref},
                            )
                        )
                    elif (
                        node.node_type == "table"
                        and target.properties.get("table_ref") != node.id
                    ) or (
                        node.node_type == "generation_rule"
                        and ref
                        in properties.get("triggers", {})
                        .get("sql", {})
                        .get("columns", [])
                        and target.node_type != "column"
                    ):
                        errors.append(
                            issue(
                                "INVALID_ENDPOINT_TYPE",
                                f"Reference {ref!r} has an invalid owner or slot type.",
                                "Use a Column belonging to this table, or a Column for a SQL trigger.",
                                path=f"nodes.{node.id}.properties",
                                details={"reference": ref},
                            )
                        )
                    elif allowed and not any(
                        self.schema.is_node_type_compatible(target.node_type, t)
                        for t in allowed
                    ):
                        errors.append(
                            issue(
                                "INVALID_ENDPOINT_TYPE",
                                f"Reference {ref!r} has incompatible type {target.node_type!r}.",
                                "Use a dependency of an allowed type.",
                                details={
                                    "node_id": node.id,
                                    "reference": ref,
                                    "actual_type": target.node_type,
                                    "allowed_types": sorted(allowed),
                                },
                            )
                        )
        if errors:
            raise GraphRejected(errors[:20], self.revision)

    @staticmethod
    def _check_cycles(state):
        def check(adjacency, code, label, **details):
            done = set()
            for start in adjacency:
                if start in done:
                    continue
                path, active = [start], {start: 0}
                stack = [iter(adjacency.get(start, ()))]
                while stack:
                    target = next(stack[-1], None)
                    if target is None:
                        done.add(path[-1])
                        active.pop(path.pop())
                        stack.pop()
                    elif target in active:
                        cycle = path[active[target] :] + [target]
                        raise GraphRejected(
                            [
                                issue(
                                    code,
                                    f"{label} cycle: {' -> '.join(cycle)}.",
                                    "Remove or correct a reference in this cycle.",
                                    details={"cycle": cycle, **details},
                                )
                            ]
                        )
                    elif target not in done:
                        active[target] = len(path)
                        path.append(target)
                        stack.append(iter(adjacency.get(target, ())))

        conflicted = {c.id for c in state.conflict_nodes}
        nodes = {n.id: n for n in state.graph.nodes}
        adjacency = {}
        for edge in state.graph.edges:
            if (
                edge.edge_type == "depends_on"
                or edge.reference_origin is not None
                and nodes[edge.from_node_id].node_type == "metric"
                and nodes[edge.to_node_id].node_type == "metric"
            ) and edge.from_node_id not in conflicted:
                adjacency.setdefault(edge.from_node_id, []).append(edge.to_node_id)
        check(adjacency, "METRIC_DEPENDENCY_CYCLE", "Metric dependency")
        nodes = {n.id: n for n in state.graph.nodes}
        # Check each complete alternative against stable dependencies. Do not
        # combine mutually exclusive alternatives into one dependency graph.
        for conflict in state.conflict_nodes:
            if nodes[conflict.id].node_type != "metric":
                continue
            for alternative in conflict.alternatives:
                branch = {
                    **adjacency,
                    conflict.id: [
                        edge.to_node_id
                        for edge in state.graph.edges
                        if edge.from_node_id == conflict.id
                        and edge.reference_origin is not None
                        and nodes[edge.to_node_id].node_type == "metric"
                        and alternative.alternative_id
                        in edge.properties.get("alternative_ids", ())
                    ],
                }
                check(
                    branch,
                    "METRIC_DEPENDENCY_CYCLE",
                    "Metric dependency",
                    alternative_id=alternative.alternative_id,
                )
        for relation in ("broader_than", "part_of"):
            hierarchy = {}
            for edge in state.graph.edges:
                if edge.edge_type == relation:
                    hierarchy.setdefault(edge.from_node_id, []).append(edge.to_node_id)
            check(hierarchy, "CONCEPT_HIERARCHY_CYCLE", relation)

    def apply(
        self,
        operations: list[dict],
        *,
        expected_revision: str,
        request_id: str,
        commit: Callable[[GraphState, str], str | None] | None = None,
        allowed_node_types: frozenset[str] | None = None,
        reviewed_reconciliation: bool = False,
    ) -> GraphResult:
        """Apply a complete batch or return actionable issues without mutation.

        ``commit`` is trusted service code, never an agent-supplied argument. It
        publishes with CAS and may return the repository's revision. Receipts
        remain local; durable workflow receipts still belong to GraphStore.
        ``reviewed_reconciliation`` is reserved for trusted merge orchestration
        after assessment of a complete replacement for unresolved alternatives.
        """
        with self._lock:
            if not isinstance(request_id, str) or not request_id.strip():
                return self._rejected(
                    [
                        issue(
                            "INVALID_FIELD",
                            "request_id must be nonempty.",
                            "Supply a stable ID for this request.",
                            path="request_id",
                        )
                    ]
                )
            try:
                request_hash = _fingerprint(
                    {
                        "operations": operations,
                        "expected_revision": expected_revision,
                        "reviewed_reconciliation": reviewed_reconciliation,
                    }
                )
            except (TypeError, ValueError):
                return self._rejected(
                    [
                        issue(
                            "INVALID_FIELD",
                            "Operations must be JSON-compatible objects.",
                            "Serialize nodes and edges as JSON objects.",
                            path="operations",
                        )
                    ]
                )
            if request_id in self._receipts:
                previous_hash, result = self._receipts[request_id]
                if previous_hash == request_hash:
                    return result.model_copy(deep=True)
                return self._rejected(
                    [
                        issue(
                            "REQUEST_ID_REUSED",
                            "request_id was reused for different content.",
                            "Use a new request_id.",
                        )
                    ]
                )
            if expected_revision != self.revision:
                return self._rejected(
                    [
                        issue(
                            "REVISION_MISMATCH",
                            "The graph revision changed.",
                            "Read the current snapshot and resubmit against its revision.",
                            details={
                                "expected_revision": expected_revision,
                                "current_revision": self.revision,
                            },
                        )
                    ]
                )
            nodes = {n.id: n.model_copy(deep=True) for n in self._state.graph.nodes}
            edges = {e.id: e.model_copy(deep=True) for e in self._state.graph.edges}
            conflicts = {
                c.id: c.model_copy(deep=True) for c in self._state.conflict_nodes
            }
            transitions = {}
            reconciled = set()
            changed, deleted, op_by_node, op_by_edge = set(), set(), {}, {}
            errors = []
            if not isinstance(operations, list) or not operations:
                return self._rejected(
                    [
                        issue(
                            "INVALID_OPERATION",
                            "A nonempty batch is required.",
                            "Provide at least one graph operation.",
                        )
                    ]
                )
            try:
                # Nodes first: both forward batch refs and edge-before-node are legal.
                for i, op in enumerate(operations):
                    if not isinstance(op, dict):
                        raise GraphRejected(
                            [
                                issue(
                                    "INVALID_OPERATION",
                                    "Operation must be an object.",
                                    "Use a documented operation.",
                                    operation_index=i,
                                )
                            ]
                        )
                    kind = op.get("operation")
                    allowed_keys = (
                        {"operation", "node"}
                        if kind in {"create_node", "update_node"}
                        else {"operation", "node", "conflict"}
                        if kind == "set_conflict"
                        else {"operation", "node_id", "alternative_id"}
                        if kind == "resolve_conflict"
                        else {"operation", "node_id"}
                        if kind == "delete_node"
                        else {"operation", "edge_id"}
                        if kind == "delete_edge"
                        else {"operation", "edge"}
                    )
                    if set(op) - allowed_keys:
                        raise GraphRejected(
                            [
                                issue(
                                    "INVALID_FIELD",
                                    "Unexpected operation fields.",
                                    "Use only the fields documented for this operation.",
                                    operation_index=i,
                                    details={"fields": sorted(set(op) - allowed_keys)},
                                )
                            ]
                        )
                    if kind in {"create_edge", "update_edge", "delete_edge"}:
                        continue
                    if kind not in {
                        "create_node",
                        "update_node",
                        "delete_node",
                        "set_conflict",
                        "resolve_conflict",
                    }:
                        raise GraphRejected(
                            [
                                issue(
                                    "INVALID_OPERATION",
                                    f"Unknown operation {kind!r}.",
                                    "Use create/update/delete_node, create/update/delete_edge, set_conflict or resolve_conflict.",
                                    operation_index=i,
                                )
                            ]
                        )
                    if kind in {"delete_node", "resolve_conflict"}:
                        node_id = op.get("node_id")
                        node = nodes.get(node_id)
                    else:
                        try:
                            supplied_node = op.get("node")
                            if (
                                kind == "set_conflict"
                                and isinstance(supplied_node, dict)
                                and isinstance(op.get("conflict"), dict)
                            ):
                                proposal = NodeMergeConflict.model_validate(
                                    op["conflict"]
                                )
                                supplied_node = {
                                    **supplied_node,
                                    **proposal.model_dump(
                                        mode="python", exclude={"node_id"}
                                    ),
                                    "merge_status": MergeStatus.CONFLICT,
                                }
                            node = GraphNode.model_validate(supplied_node)
                        except ValidationError as exc:
                            raise GraphRejected(
                                input_issues(exc, operation_index=i, path="node")
                            ) from exc
                        node_id = node.id
                    if node_id in changed:
                        raise GraphRejected(
                            [
                                issue(
                                    "INVALID_OPERATION",
                                    "One operation per node is allowed in a batch.",
                                    "Combine changes into one full node replacement.",
                                    operation_index=i,
                                )
                            ]
                        )
                    if (
                        allowed_node_types is not None
                        and node is not None
                        and node.node_type not in allowed_node_types
                    ):
                        raise GraphRejected(
                            [
                                issue(
                                    "OPERATION_NOT_ALLOWED",
                                    f"Cannot edit physical/type {node.node_type!r}.",
                                    "Use the permitted semantic node tools.",
                                    operation_index=i,
                                )
                            ]
                        )
                    if (
                        node_id in conflicts
                        and kind
                        not in {
                            "set_conflict",
                            "resolve_conflict",
                        }
                        and not (reviewed_reconciliation and kind == "update_node")
                    ):
                        raise GraphRejected(
                            [
                                issue(
                                    "CONFLICT_REQUIRES_RECONCILIATION",
                                    "CRUD cannot replace a node with unresolved alternatives.",
                                    "Use set_conflict to replace its candidates or resolve_conflict to select one candidate.",
                                    operation_index=i,
                                    details={"node_id": node_id},
                                )
                            ]
                        )
                    exists = node_id in nodes
                    if (kind == "create_node" and exists) or (
                        kind not in {"create_node", "set_conflict"} and not exists
                    ):
                        raise GraphRejected(
                            [
                                issue(
                                    "NODE_ALREADY_EXISTS"
                                    if exists
                                    else "NODE_NOT_FOUND",
                                    f"Invalid {kind} for node {node_id!r}.",
                                    "Read the graph and use the matching create/update/delete operation.",
                                    operation_index=i,
                                )
                            ]
                        )
                    if (
                        kind in {"update_node", "set_conflict"}
                        and exists
                        and node.node_type != nodes[node_id].node_type
                    ):
                        raise GraphRejected(
                            [
                                issue(
                                    "IMMUTABLE_NODE_TYPE",
                                    "Existing node types cannot change through CRUD.",
                                    "Create a separate node or use the trusted merge correction path.",
                                    operation_index=i,
                                )
                            ]
                        )
                    if kind in {"update_node", "set_conflict"} and exists:
                        policy = self.schema.node_type_index()[
                            node.node_type
                        ].identity_policy

                        def identity_value(value, field):
                            return (
                                value.properties.get(field.removeprefix("properties."))
                                if field.startswith("properties.")
                                else getattr(value, field, None)
                            )

                        if policy.mode == "exact" and any(
                            identity_value(node, field)
                            != identity_value(nodes[node_id], field)
                            for field in policy.fields
                        ):
                            raise GraphRejected(
                                [
                                    issue(
                                        "IMMUTABLE_NODE_IDENTITY",
                                        "An update cannot change an existing node's exact identity.",
                                        "Create a distinct node and explicitly update its dependents.",
                                        operation_index=i,
                                        details={"node_id": node_id},
                                    )
                                ]
                            )
                    if (
                        reviewed_reconciliation
                        and kind == "update_node"
                        and node_id in conflicts
                    ):
                        if node.merge_status != MergeStatus.NORMAL:
                            raise GraphRejected(
                                [
                                    issue(
                                        "INVALID_FIELD",
                                        "A reviewed replacement must be a complete normal node.",
                                        "Use set_conflict when alternatives remain unresolved.",
                                        operation_index=i,
                                        path="node.merge_status",
                                    )
                                ]
                            )
                        del conflicts[node_id]
                        reconciled.add(node_id)
                    if kind == "set_conflict":
                        try:
                            conflict = (
                                node.as_conflict()
                                if op.get("conflict") is None and node.alternatives
                                else NodeMergeConflict.model_validate(
                                    op.get("conflict")
                                )
                            )
                        except ValidationError as exc:
                            raise GraphRejected(
                                input_issues(exc, operation_index=i, path="conflict")
                            ) from exc
                        if conflict.node_id != node_id:
                            raise GraphRejected(
                                [
                                    issue(
                                        "INVALID_FIELD",
                                        "Conflict node_id must match node.id.",
                                        "Use the same owner ID.",
                                        operation_index=i,
                                        path="conflict.node_id",
                                    )
                                ]
                            )
                        from cortex.schema.relationships import _union_sources

                        node = node.with_conflict(conflict).model_copy(
                            update={
                                "sources": _union_sources(
                                    *(a.sources for a in conflict.alternatives)
                                )
                            }
                        )
                        conflicts[node_id] = node
                        transitions[node_id] = None
                    elif kind == "resolve_conflict":
                        conflict = conflicts.get(node_id)
                        if conflict is None:
                            raise GraphRejected(
                                [
                                    issue(
                                        "CONFLICT_NOT_FOUND",
                                        "Node has no conflict to resolve.",
                                        "Inspect its current conflict before resolving.",
                                        operation_index=i,
                                        path="node_id",
                                    )
                                ]
                            )
                        alternative = next(
                            (
                                a
                                for a in conflict.alternatives
                                if a.alternative_id == op.get("alternative_id")
                            ),
                            None,
                        )
                        if alternative is None:
                            raise GraphRejected(
                                [
                                    issue(
                                        "ALTERNATIVE_NOT_FOUND",
                                        "Selected candidate does not exist on this node.",
                                        "Choose an alternative_id returned by get_conflict.",
                                        operation_index=i,
                                        path="alternative_id",
                                    )
                                ]
                            )
                        content = self.resolve_alternative(node, alternative)
                        node = GraphNode(
                            id=node.id,
                            node_type=node.node_type,
                            **content.model_dump(mode="json"),
                            sources=alternative.sources,
                        )
                        del conflicts[node_id]
                        transitions[node_id] = alternative.alternative_id
                    changed.add(node_id)
                    op_by_node[node_id] = i
                    if kind == "delete_node":
                        deleted.add(node_id)
                        del nodes[node_id]
                    else:
                        nodes[node_id] = node
                # Scope existing facts before processing explicit edits. Losing branches
                # disappear; selecting a candidate turns surviving facts into ordinary edges.
                for edge_id, edge in tuple(edges.items()):
                    if edge.from_node_id in reconciled:
                        if edge.reference_origin == "default":
                            del edges[edge_id]
                        else:
                            properties = dict(edge.properties)
                            properties.pop("alternative_ids", None)
                            edges[edge_id] = edge.model_copy(
                                update={"properties": properties}
                            )
                        continue
                    if edge.from_node_id not in transitions:
                        continue
                    selected = transitions[edge.from_node_id]
                    branch_ids = edge.properties.get("alternative_ids")
                    properties = dict(edge.properties)
                    if branch_ids:
                        kept = (
                            ([selected] if selected in branch_ids else [])
                            if selected is not None
                            else sorted(
                                set(branch_ids)
                                & {
                                    a.alternative_id
                                    for a in conflicts[edge.from_node_id].alternatives
                                }
                            )
                        )
                        if not kept:
                            del edges[edge_id]
                            continue
                        if selected is None:
                            properties["alternative_ids"] = kept
                        else:
                            properties.pop("alternative_ids", None)
                    if selected is not None and "target_id" in properties:
                        owner = nodes[edge.from_node_id]
                        if properties["target_id"] not in {
                            t["id"] for t in owner.properties.get("targets", ())
                        }:
                            del edges[edge_id]
                            continue
                    edges[edge_id] = edge.model_copy(
                        update={
                            "properties": properties,
                            **(
                                {"sources": nodes[edge.from_node_id].sources}
                                if selected is not None
                                and edge.reference_origin == "default"
                                else {}
                            ),
                        }
                    )
                # Branch-scoped variants can become the same fact after selection.
                # Coalesce that fact, preserving evidence, rather than publishing duplicates.
                selected_facts = {}
                for edge_id, edge in sorted(edges.items()):
                    if (
                        transitions.get(edge.from_node_id) is None
                        and edge.from_node_id not in reconciled
                    ) or edge.reference_origin == "default":
                        continue
                    fact = (
                        edge.edge_type,
                        edge.from_node_id,
                        edge.to_node_id,
                        json.dumps(edge.properties, sort_keys=True),
                    )
                    previous_id = selected_facts.get(fact)
                    if previous_id is not None:
                        from cortex.schema.relationships import _union_sources

                        previous = edges[previous_id]
                        edges[previous_id] = previous.model_copy(
                            update={
                                "sources": _union_sources(
                                    previous.sources, edge.sources
                                ),
                                "reference_origin": previous.reference_origin
                                or edge.reference_origin,
                            }
                        )
                        del edges[edge_id]
                    else:
                        selected_facts[fact] = edge_id
                for i, op in enumerate(operations):
                    kind = op.get("operation")
                    if kind not in {"create_edge", "update_edge", "delete_edge"}:
                        continue
                    try:
                        edge = (
                            edges.get(op.get("edge_id"))
                            if kind == "delete_edge"
                            else GraphEdge.model_validate(op.get("edge"))
                        )
                    except ValidationError as exc:
                        raise GraphRejected(
                            input_issues(exc, operation_index=i, path="edge")
                        ) from exc
                    if edge is None:
                        raise GraphRejected(
                            [
                                issue(
                                    "EDGE_NOT_FOUND",
                                    "Edge does not exist.",
                                    "Read edges before editing.",
                                    operation_index=i,
                                )
                            ]
                        )
                    if edge.id in op_by_edge:
                        raise GraphRejected(
                            [
                                issue(
                                    "INVALID_OPERATION",
                                    "One operation per edge is allowed in a batch.",
                                    "Combine changes into one full edge replacement.",
                                    operation_index=i,
                                )
                            ]
                        )
                    op_by_edge[edge.id] = i
                    source = nodes.get(edge.from_node_id) or next(
                        (
                            n
                            for n in self._state.graph.nodes
                            if n.id == edge.from_node_id
                        ),
                        None,
                    )
                    if (
                        allowed_node_types is not None
                        and source is not None
                        and source.node_type not in allowed_node_types
                    ):
                        raise GraphRejected(
                            [
                                issue(
                                    "OPERATION_NOT_ALLOWED",
                                    "This role cannot edit relations on physical nodes.",
                                    "Use the permitted semantic graph tools.",
                                    operation_index=i,
                                )
                            ]
                        )
                    old = edges.get(edge.id)
                    old_source = (
                        (
                            nodes.get(old.from_node_id)
                            or next(
                                (
                                    n
                                    for n in self._state.graph.nodes
                                    if n.id == old.from_node_id
                                ),
                                None,
                            )
                        )
                        if old
                        else None
                    )
                    if (
                        allowed_node_types is not None
                        and old_source is not None
                        and old_source.node_type not in allowed_node_types
                    ):
                        raise GraphRejected(
                            [
                                issue(
                                    "OPERATION_NOT_ALLOWED",
                                    "This role cannot rewrite relations owned by physical nodes.",
                                    "Use the permitted semantic graph tools.",
                                    operation_index=i,
                                )
                            ]
                        )
                    conflicted = {c.id for c in self._state.conflict_nodes}
                    if any(
                        owner
                        and owner.id in conflicted
                        and owner.id not in transitions
                        and owner.id not in reconciled
                        for owner in (source, old_source)
                    ):
                        raise GraphRejected(
                            [
                                issue(
                                    "CONFLICT_REQUIRES_RECONCILIATION",
                                    "Cannot edit relations owned by a node with unresolved alternatives.",
                                    "Include set_conflict or resolve_conflict for this owner in the same transaction.",
                                    operation_index=i,
                                )
                            ]
                        )
                    exists = edge.id in edges
                    if (kind == "create_edge" and exists) or (
                        kind != "create_edge" and not exists
                    ):
                        raise GraphRejected(
                            [
                                issue(
                                    "INVALID_OPERATION",
                                    f"Invalid {kind} for edge {edge.id!r}.",
                                    "Read edges and use the matching operation.",
                                    operation_index=i,
                                )
                            ]
                        )
                    if kind == "delete_edge":
                        del edges[edge.id]
                    else:
                        edges[edge.id] = edge.model_copy(
                            update={"reference_origin": None}
                        )
                # Remove only deleted owners' managed edges; remaining inbound
                # and explicit edges block deletion until explicitly handled.
                for edge_id, edge in tuple(edges.items()):
                    old_source = next(
                        (
                            n
                            for n in self._state.graph.nodes
                            if n.id == edge.from_node_id
                        ),
                        None,
                    )
                    if (
                        edge.from_node_id in deleted
                        and old_source
                        and edge.reference_origin is not None
                    ):
                        del edges[edge_id]
                reference_nodes = {n.id: n for n in self._state.graph.nodes}
                reference_nodes.update(nodes)
                for node_id in deleted:
                    dependents = [
                        n.id
                        for n in nodes.values()
                        if node_id in self._node_references(n, reference_nodes)
                    ]
                    dependents += [
                        e.id
                        for e in edges.values()
                        if not (
                            e.from_node_id in changed
                            and e.from_node_id in nodes
                            and e.reference_origin is not None
                        )
                        and (
                            node_id in {e.from_node_id, e.to_node_id}
                            or any(
                                node_id in pair.values()
                                for pair in e.properties.get("column_pairs", ())
                            )
                        )
                    ]
                    dependents += [
                        c.id
                        for c in conflicts.values()
                        for a in c.alternatives
                        if node_id
                        in self._references(
                            self.resolve_alternative(
                                reference_nodes[c.id], a
                            ).properties
                        )
                    ]
                    if dependents:
                        raise GraphRejected(
                            [
                                issue(
                                    "NODE_HAS_DEPENDENTS",
                                    f"Node {node_id!r} still has dependents.",
                                    "Update or remove these references in the same batch before deleting.",
                                    operation_index=op_by_node[node_id],
                                    details={
                                        "dependent_ids": sorted(set(dependents))[:20]
                                    },
                                )
                            ]
                        )
                # CRUD has one editable edge per declared identity. Merge may
                # preserve different property variants as separate evidence.
                identities = {}
                specs = self.schema.edge_type_index()
                for relation in edges.values():
                    spec = specs.get(relation.edge_type)
                    if relation.reference_origin == "default":
                        continue
                    try:
                        identity = edge_identity_key(relation, spec)
                    except (ValueError, TypeError, KeyError):
                        continue
                    previous = identities.get(identity)
                    if previous and (
                        previous.id in op_by_edge or relation.id in op_by_edge
                    ):
                        operation_index = op_by_edge.get(
                            relation.id, op_by_edge.get(previous.id)
                        )
                        raise GraphRejected(
                            [
                                issue(
                                    "DUPLICATE_EDGE",
                                    "An edge with this relation identity already exists.",
                                    "Update the existing edge ID, or remove it in the same batch before replacement.",
                                    path=f"edges.{relation.id}",
                                    operation_index=operation_index,
                                    details={"existing_edge_id": previous.id},
                                )
                            ]
                        )
                    identities[identity] = relation
                candidate = GraphState(
                    graph=GraphSpace(
                        nodes=tuple(nodes.values()),
                        edges=tuple(edges.values()),
                    ),
                )
                candidate, dropped = self.prepare_state(
                    candidate,
                    strict=True,
                    node_ids=changed
                    | {edges[key].from_node_id for key in op_by_edge if key in edges}
                    | {
                        edge.from_node_id
                        for edge in self._state.graph.edges
                        if edge.id in op_by_edge
                    },
                )
                lost_facts = [
                    edge_id
                    for edge_id in dropped
                    if edge_id in edges
                    and edges[edge_id].from_node_id in reconciled
                    and edges[edge_id].reference_origin == "explicit"
                ]
                if lost_facts:
                    raise GraphRejected(
                        [
                            issue(
                                "EXPLICIT_RELATION_REQUIRES_RECONCILIATION",
                                "Reviewed replacement removes an explicit relation anchor.",
                                "Preserve its anchor, or explicitly update or delete the relation in this batch.",
                                details={"edge_ids": sorted(lost_facts)},
                            )
                        ]
                    )
                revision = _fingerprint(candidate.model_dump(mode="json"))
                if commit is not None:
                    revision = (
                        commit(candidate.model_copy(deep=True), self.revision)
                        or revision
                    )
            except OSError:
                error_id = uuid4().hex
                logging.getLogger(__name__).exception(
                    "Graph commit failed: %s", error_id
                )
                return GraphResult(
                    status="error",
                    revision=self.revision,
                    errors=[
                        issue(
                            "STORAGE_ERROR",
                            "Graph publication failed.",
                            "Check the request receipt before retrying the same request_id.",
                            details={"error_id": error_id},
                        )
                    ],
                )
            except GraphRejected as exc:
                errors = exc.issues
            except ValidationError as exc:
                errors = input_issues(exc)
            except (GraphSchemaError, TypeError, KeyError) as exc:
                errors = [
                    issue(
                        "INVALID_GRAPH",
                        str(exc),
                        "Correct the batch's fields, references or endpoints and retry.",
                    )
                ]
            if errors:
                for error in errors:
                    if error.operation_index is None:
                        error.operation_index = op_by_node.get(
                            error.details.get("node_id")
                        )
                        if error.operation_index is None:
                            candidates = [
                                (f"nodes.{key}", index)
                                for key, index in op_by_node.items()
                            ]
                            candidates += [
                                (f"nodes.{key}", index)
                                for key, index in op_by_node.items()
                            ]
                            candidates += [
                                (f"edges.{key}", index)
                                for key, index in op_by_edge.items()
                            ]
                            error.operation_index = next(
                                (
                                    index
                                    for prefix, index in sorted(
                                        candidates, key=lambda p: -len(p[0])
                                    )
                                    if error.path == prefix
                                    or error.path.startswith(prefix + ".")
                                ),
                                None,
                            )
                return self._rejected(errors)
            self._state, self.revision = candidate.model_copy(deep=True), revision
            result = GraphResult(
                status="applied",
                revision=revision,
                state=candidate,
                changed_node_ids=sorted(changed),
            )
            self._receipts[request_id] = (request_hash, result.model_copy(deep=True))
            return result

    def _rejected(self, errors):
        return GraphResult(status="rejected", revision=self.revision, errors=errors)
