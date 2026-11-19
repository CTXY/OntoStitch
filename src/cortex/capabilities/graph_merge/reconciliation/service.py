"""Group preparation and deterministic shortcuts around one LangGraph workflow."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.identity import exact_identity_key
from cortex.capabilities.graph_merge.models import (
    NodeRepresentation,
    ReconciledNode,
    ReconciledRepresentation,
    RepresentationRef,
)
from cortex.capabilities.graph_merge.reconciliation.evidence import (
    resolve_group_evidence,
)
from cortex.capabilities.graph_merge.reconciliation.materialization import (
    StableCanonicalIdFactory,
    _materialize_node,
    _retained_conflict_points,
    _union_sources,
    materialize,
)
from cortex.capabilities.graph_merge.workflow.context import GroupContext
from cortex.capabilities.graph_merge.workflow.group import build_group_workflow
from cortex.schema import GraphNode

SEMANTIC_TYPES = frozenset({"concept", "metric", "generation_rule"})


class NodeGroupReconciler:
    def __init__(
        self,
        *,
        evidence_resolver=None,
        group_consolidator=None,
        consolidation_checker=None,
        consolidation_max_rounds=3,
        consolidation_max_calls=18,
        max_evidence_items=24,
        max_evidence_characters=48_000,
        trace_sink=None,
    ):
        if (group_consolidator is None) != (consolidation_checker is None):
            raise ValueError("Consolidator and Assessment must be supplied together")
        self.evidence_resolver = evidence_resolver
        self.group_consolidator = group_consolidator
        self.consolidation_checker = consolidation_checker
        self.max_rounds, self.max_calls = (
            consolidation_max_rounds,
            consolidation_max_calls,
        )
        self.max_evidence_items = max_evidence_items
        self.max_evidence_characters = max_evidence_characters
        self.trace_sink = trace_sink
        self.id_factory = StableCanonicalIdFactory()

    def canonical_id_for_group(self, group):
        return group.existing_node_id or self.id_factory.new_node_id(group)

    def reconcile_group(
        self,
        group,
        *,
        schema,
        historical_conflict=None,
        dependency_nodes=(),
        available_nodes=(),
        outgoing_edges=(),
        evidence_items=None,
        checkpointer=None,
        run_id=None,
        unchanged_relations=True,
        force_reconcile=False,
        dependency_variants=None,
        existing_state=None,
        node_mappings=None,
        input_nodes=(),
        input_relations=(),
    ):
        # Existing-only definitions are already accepted business knowledge, including conflicts.
        if (
            historical_conflict is not None
            and not force_reconcile
            and all(m.ref.origin == "existing_node" for m in group.members)
        ):
            return ReconciledNode(
                node=historical_conflict,
                conflict=historical_conflict.as_conflict(),
                representations=group.members,
                evidence_by_ref={},
            )
        if historical_conflict is not None:
            if historical_conflict.common_sources:
                raise ValueError(
                    "Legacy group consolidation cannot reassign common conflict support; "
                    "use the incremental merge contract. The historical graph is unchanged."
                )
            group = group.model_copy(
                update={
                    "members": tuple(
                        NodeRepresentation(
                            ref=RepresentationRef(
                                origin="existing_alternative", ref_id=a.alternative_id
                            ),
                            node_type=group.node_type,
                            content=GraphManager.resolve_alternative(
                                _materialize_node(
                                    group.existing_node_id,
                                    group.node_type,
                                    group.members[0].content,
                                    group.members[0].sources,
                                ),
                                a,
                            ),
                            sources=a.sources,
                        )
                        for a in historical_conflict.alternatives
                    )
                    + tuple(m for m in group.members if m.ref.origin == "incoming_node")
                }
            )
        canonical_id = self.canonical_id_for_group(group)
        same_content = all(m.content == group.members[0].content for m in group.members)
        direct = (
            (len(group.members) == 1 and not force_reconcile)
            or (same_content and unchanged_relations and not force_reconcile)
            or group.node_type not in SEMANTIC_TYPES
        )
        if direct:
            baseline = next(
                (m for m in group.members if m.ref.origin == "existing_node"),
                group.members[0],
            )
            incoming = [m for m in group.members if m.ref.origin == "incoming_node"]
            if group.node_type not in SEMANTIC_TYPES and len(incoming) == 1:
                policy = schema.node_type_index()[group.node_type].identity_policy
                if exact_identity_key(baseline.content, policy) != exact_identity_key(
                    incoming[0].content, policy
                ):
                    baseline = incoming[0]
            if same_content or group.node_type not in SEMANTIC_TYPES:
                aliases = {
                    a
                    for m in group.members
                    for a in (*m.content.aliases, m.content.name)
                    if a and a != baseline.content.name
                }
                content = baseline.content.model_copy(
                    update={
                        "aliases": tuple(sorted(aliases)),
                        "tags": tuple(
                            sorted({t for m in group.members for t in m.content.tags})
                        ),
                    }
                )
            else:
                content = baseline.content
            return ReconciledNode(
                node=_materialize_node(
                    canonical_id,
                    group.node_type,
                    content,
                    _union_sources(*(m.sources for m in group.members)),
                ),
                representations=group.members,
                evidence_by_ref={},
            )
        if evidence_items is None:
            try:
                evidence_by_ref = resolve_group_evidence(group, self.evidence_resolver)
                evidence_items = tuple(
                    {e.id: e for v in evidence_by_ref.values() for e in v}.values()
                )
            except Exception:  # noqa: BLE001 - missing evidence must preserve the original definitions
                evidence_items = ()
        group_sources = {s for m in group.members for s in m.sources}
        context = GroupContext(
            group=group,
            schema_definition=schema,
            evidence=tuple(e for e in evidence_items if e.source in group_sources),
            nodes=tuple(available_nodes or dependency_nodes),
            relations=tuple(outgoing_edges),
            dependency_variants=dependency_variants or {},
            max_evidence_items=self.max_evidence_items,
            max_context_characters=self.max_evidence_characters,
            existing_state=existing_state,
            node_mappings=node_mappings or {},
            input_nodes=tuple(input_nodes),
            input_relations=tuple(input_relations),
        )
        if self.group_consolidator is None:
            result = materialize(
                self.id_factory,
                canonical_id,
                group,
                tuple(
                    ReconciledRepresentation(
                        content=m.content, supporting_refs=(m.ref,)
                    )
                    for m in group.members
                ),
                (),
                context.evidence_by_ref(),
                ["CONSOLIDATION_PRESERVED_UNRESOLVED"],
            )
            if result.conflict is not None:
                preserved = result.conflict.model_copy(
                    update={
                        "consolidation_status": "unresolved",
                        "preservation_reasons": ("semantics_unavailable",),
                    }
                )
                result = result.model_copy(
                    update={
                        "node": result.node.with_conflict(preserved),
                        "conflict": preserved,
                    }
                )
            return result
        graph = build_group_workflow(
            self.group_consolidator,
            self.consolidation_checker,
            max_rounds=self.max_rounds,
            max_calls=self.max_calls,
            checkpointer=checkpointer,
            id_factory=self.id_factory,
        )
        suffix = hashlib.sha256(group.group_id.encode()).hexdigest()[:24]
        thread = f"{run_id or uuid4().hex}:group:{suffix}"
        config = {
            "configurable": {"thread_id": thread, "checkpoint_ns": ""},
            "recursion_limit": self.max_rounds * 8 + self.max_calls * 3 + 12,
            "metadata": {
                "thread_id": thread,
                "group_id": group.group_id,
                "base_revision": GraphManager(schema, existing_state).revision
                if existing_state is not None
                else None,
            },
        }
        saved = graph.get_state(config) if checkpointer is not None else None
        payload = context.model_dump(mode="json")
        if saved and saved.values and saved.values.get("context") != payload:
            raise ValueError("Cannot resume group checkpoint with changed inputs")
        initial = None if saved and saved.values else {"context": payload}
        output = (
            saved.values
            if saved and saved.values and not saved.next
            else graph.invoke(
                initial,
                config=config,
                **({"durability": "sync"} if checkpointer is not None else {}),
            )
        )
        result = output["result"]
        node = GraphNode.model_validate(result["node"])
        conflict = node.as_conflict() if node.alternatives else None
        if historical_conflict is not None and conflict is not None:
            retained = _retained_conflict_points(
                historical_conflict, conflict.alternatives
            )
            ids = {p.point_id for p in conflict.conflict_points}
            conflict = conflict.model_copy(
                update={
                    "conflict_points": (
                        *conflict.conflict_points,
                        *(p for p in retained if p.point_id not in ids),
                    )
                }
            )
        if self.trace_sink is not None:
            self.trace_sink.record(
                {
                    "component": "consolidation",
                    "group_id": group.group_id,
                    "thread_id": thread,
                    **result["consolidation"],
                }
            )
        return ReconciledNode(
            node=node.with_conflict(conflict) if conflict else node.without_conflict(),
            conflict=conflict,
            representations=group.members,
            evidence_by_ref=context.evidence_by_ref(),
            diagnostic_codes=tuple(result["diagnostic_codes"]),
            consolidation=result["consolidation"],
        )
