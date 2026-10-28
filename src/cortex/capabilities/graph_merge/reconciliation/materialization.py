from __future__ import annotations

import hashlib
import json

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.models import (
    DifferencePoint,
    FinalNodeGroup,
    ReconciledNode,
    ReconciledRepresentation,
    RepresentationRef,
)
from cortex.schema import (
    ConflictChoice,
    ConflictPoint,
    GraphNode,
    MergeStatus,
)


class StableCanonicalIdFactory:
    def new_node_id(self, group: FinalNodeGroup) -> str:
        refs = ",".join(sorted(member.ref.ref_id for member in group.members))
        digest = hashlib.sha256(f"{group.node_type}:{refs}".encode()).hexdigest()[:20]
        return f"{group.node_type}.canonical.{digest}"

    def new_alternative_id(
        self, node_id: str, supporting_refs: tuple[RepresentationRef, ...]
    ) -> str:
        refs = ",".join(sorted(f"{ref.origin}:{ref.ref_id}" for ref in supporting_refs))
        digest = hashlib.sha256(f"{node_id}:{refs}".encode()).hexdigest()[:20]
        return f"alternative.{digest}"


def _materialize_node(node_id, node_type, content, sources, status=MergeStatus.NORMAL):
    return GraphNode(
        id=node_id,
        node_type=node_type,
        **content.model_dump(mode="python"),
        sources=sources,
        merge_status=status,
    )


def _retained_conflict_points(conflict, alternatives):
    alternative_ids = {item.alternative_id for item in alternatives}
    return tuple(
        point
        for point in conflict.conflict_points
        if {
            alternative_id
            for choice in point.choices
            for alternative_id in choice.alternative_ids
        }
        <= alternative_ids
    )


def _materialize_conflict_points(
    difference_points: tuple[DifferencePoint, ...],
    outcomes: tuple[ReconciledRepresentation, ...],
    alternative_by_content: dict[str, str],
) -> tuple[ConflictPoint, ...]:
    """Project unresolved input differences onto complete published alternatives."""

    alternative_by_ref = {
        ref: alternative_by_content[_content_fingerprint(outcome.content)]
        for outcome in outcomes
        for ref in outcome.supporting_refs
    }
    result = []
    for point in difference_points:
        if point.resolution != "conflict":
            continue
        choices = []
        for position in point.positions:
            alternative_ids = tuple(
                sorted(
                    {
                        alternative_by_ref[member.representation_ref]
                        for member in position.members
                    }
                )
            )
            choices.append(
                ConflictChoice(
                    alternative_ids=alternative_ids,
                    statement=position.statement,
                )
            )
        if len({item for choice in choices for item in choice.alternative_ids}) < 2:
            continue
        result.append(
            ConflictPoint(
                point_id=point.point_id,
                aspect=point.aspect,
                description=point.reason,
                choices=tuple(choices),
            )
        )
    return tuple(result)


def _content_fingerprint(content):
    return json.dumps(
        content.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
    )


def _sources_for_refs(refs, representations):
    by_ref = {item.ref: item for item in representations}
    return _union_sources(*(by_ref[ref].sources for ref in refs))


def _union_sources(*groups):
    values = {}
    for group in groups:
        for source in group:
            key = (source.source_id, source.chunk_id, source.locator, source.case_id)
            values[key] = source
    keys = sorted(values, key=lambda item: tuple(value or "" for value in item))
    return tuple(values[key] for key in keys)


def materialize(
    id_factory,
    canonical_id,
    group,
    outcomes,
    difference_points,
    evidence_by_ref,
    diagnostics,
):
    by_content = {}
    historical_ids = {
        m.ref.ref_id for m in group.members if m.ref.origin == "existing_alternative"
    }
    for outcome in outcomes:
        key = _content_fingerprint(outcome.content)
        alternative_id = next(
            (
                ref.ref_id
                for ref in outcome.supporting_refs
                if ref.origin == "existing_alternative"
            ),
            id_factory.new_alternative_id(canonical_id, outcome.supporting_refs),
        )
        sources = _sources_for_refs(outcome.supporting_refs, group.members)
        if key in by_content:
            old_id, old_content, old_sources = by_content[key]
            by_content[key] = (
                old_id
                if old_id in historical_ids
                else alternative_id
                if alternative_id in historical_ids
                else min(old_id, alternative_id),
                old_content,
                _union_sources(old_sources, sources),
            )
        else:
            by_content[key] = (alternative_id, outcome.content, sources)
    candidates = [by_content[key] for key in sorted(by_content)]
    if len(candidates) == 1:
        _, content, sources = candidates[0]
        return ReconciledNode(
            node=_materialize_node(canonical_id, group.node_type, content, sources),
            representations=group.members,
            evidence_by_ref=evidence_by_ref,
            diagnostic_codes=tuple(diagnostics),
        )
    node = _materialize_node(
        canonical_id, group.node_type, candidates[0][1], candidates[0][2]
    )
    points = _materialize_conflict_points(
        difference_points,
        outcomes,
        {key: value[0] for key, value in by_content.items()},
    )
    node, conflict = GraphManager.compact_conflict(
        node,
        candidates,
        consolidation_status="semantic_conflict" if points else "unresolved",
        conflict_points=points,
    )
    return ReconciledNode(
        node=node,
        conflict=conflict,
        representations=group.members,
        evidence_by_ref=evidence_by_ref,
        diagnostic_codes=tuple(diagnostics),
    )
