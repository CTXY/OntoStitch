"""Serializable, fixed inputs; prompt views expose only the current group's material."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.models import (
    EvidenceItem,
    FinalNodeGroup,
    _GraphMergeModel,
)
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphState,
    SchemaDefinition,
)
from cortex.schema.references import content_reference_ids

GROUP_WORKFLOW_VERSION = "merge-group-inline-node-v2"


class GroupContext(_GraphMergeModel):
    workflow_version: Literal["merge-group-inline-node-v2"] = GROUP_WORKFLOW_VERSION
    group: FinalNodeGroup
    schema_definition: SchemaDefinition
    evidence: tuple[EvidenceItem, ...] = ()
    nodes: tuple[GraphNode, ...] = ()
    relations: tuple[GraphEdge, ...] = ()
    existing_state: GraphState | None = None
    candidate_state: GraphState | None = None
    input_nodes: tuple[GraphNode, ...] = ()
    input_relations: tuple[GraphEdge, ...] = ()
    node_mappings: dict[str, str] = Field(default_factory=dict)
    dependency_variants: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    max_evidence_items: int = 24
    max_context_characters: int = 48_000

    def relation_scope_changes(self):
        if self.existing_state is None or self.candidate_state is None:
            return ()
        before = {edge.id: edge for edge in self.existing_state.graph.edges}
        return tuple(
            edge.id
            for edge in self.candidate_state.graph.edges
            if edge.id in before
            and before[edge.id].reference_origin != "default"
            and before[edge.id].properties.get("alternative_ids")
            and not edge.properties.get("alternative_ids")
        )

    def evidence_by_ref(self):
        by_source = {item.source: item for item in self.evidence}
        return {
            m.ref: tuple(by_source[s] for s in m.sources if s in by_source)
            for m in self.group.members
        }

    def payload(self):
        group = self.group
        node_type = self.schema_definition.node_type_index()[group.node_type]
        member_ids = {member.ref.ref_id for member in group.members}
        if group.existing_node_id is not None:
            member_ids.add(group.existing_node_id)
        relations = tuple(e for e in self.relations if e.from_node_id in member_ids)
        dependency_ids = {e.to_node_id for e in relations}
        dependency_ids.update(
            ref
            for m in group.members
            for ref in content_reference_ids(m.content.properties)
        )
        for node_id in tuple(dependency_ids):
            dependency_ids.update(self.dependency_variants.get(node_id, ()))
        evidence = [e.model_dump(mode="json") for e in self.evidence]
        preload = (
            len(evidence) <= self.max_evidence_items
            and len(json.dumps(evidence, ensure_ascii=False))
            <= self.max_context_characters // 2
        )
        group_payload = group.model_dump(mode="json")
        if (
            len(
                {
                    json.dumps(m.content.model_dump(mode="json"), sort_keys=True)
                    for m in group.members
                }
            )
            > 1
        ):
            base = GraphNode(
                id=group.existing_node_id or group.group_id,
                node_type=group.node_type,
                **group.members[0].content.model_dump(mode="json"),
            )
            fingerprints = [
                json.dumps(member.content.model_dump(mode="json"), sort_keys=True)
                for member in group.members
            ]
            first_indices = {value: fingerprints.index(value) for value in fingerprints}
            common, changes = GraphManager.compact_conflict(
                base,
                [
                    (
                        str(index),
                        group.members[index].content,
                        group.members[index].sources,
                    )
                    for index in first_indices.values()
                ],
            )
            group_payload["common"] = {
                k: getattr(common, k)
                for k in ("name", "description", "aliases", "tags", "properties")
            }
            by_first = {
                int(a.alternative_id): a.overrides for a in changes.alternatives
            }
            by_index = {
                index: by_first[first_indices[value]]
                for index, value in enumerate(fingerprints)
            }
        else:
            group_payload["common"] = group.members[0].content.model_dump(mode="json")
            by_index = {index: {} for index in range(len(group.members))}
        for index, member in enumerate(group_payload["members"]):
            member.pop("content")
            member["overrides"] = by_index[index]
        payload = {
            "group": group_payload,
            "node_type": node_type.model_dump(mode="json", exclude={"fields"}),
            "effective_fields": {
                k: f.model_dump(mode="json")
                for k, f in self.schema_definition.effective_node_fields(
                    group.node_type
                ).items()
            },
            "evidence": evidence if preload else [],
            "evidence_directory": [
                {"id": e.id, "source": e.source.model_dump(mode="json")}
                for e in self.evidence
            ],
            "relations": [e.model_dump(mode="json") for e in relations],
            "dependencies": [
                {"id": n.id, "node_type": n.node_type, "name": n.name}
                for n in self.nodes
                if n.id in dependency_ids
            ],
            "instruction": "Read missing source/definition details using the read-only tools.",
        }
        if self.existing_state is not None:
            manager = GraphManager(self.schema_definition, self.existing_state)
            payload["existing_graph"] = {
                "revision": manager.revision,
                "node_count": len(self.existing_state.graph.nodes),
                "instruction": "Current accepted working graph; original inputs are separate read-only evidence.",
            }
            if self.candidate_state is not None:
                original_nodes = {n.id: n for n in self.existing_state.graph.nodes}
                payload["candidate_graph"] = {
                    "nodes": [
                        n.model_dump(mode="json")
                        for n in self.candidate_state.graph.nodes
                        if n != original_nodes.get(n.id)
                    ],
                    "instruction": "Isolated transaction preview; not yet committed. Read candidate relations with read_nodes(view='candidate').",
                }
                previous_edges = {
                    edge.id: edge for edge in self.existing_state.graph.edges
                }
                payload["candidate_graph"]["changed_relations"] = [
                    edge.model_dump(mode="json")
                    for edge in self.candidate_state.graph.edges
                    if edge != previous_edges.get(edge.id)
                ]
                payload["candidate_graph"]["scope_change_edge_ids"] = list(
                    self.relation_scope_changes()
                )
        if len(json.dumps(payload, ensure_ascii=False)) > self.max_context_characters:
            raise ValueError(
                "Group definitions exceed the configured context limit; preserve originals"
            )
        return payload
