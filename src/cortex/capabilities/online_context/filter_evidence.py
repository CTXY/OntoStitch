"""Candidate-scoped evidence views; never mutate or re-retrieve the stored graph."""

from copy import deepcopy

from cortex.application.online_context import ChoicePoint
from cortex.capabilities.online_context.graph.context import (
    without_untrusted_dimension_values,
)
from cortex.schema import GraphState
from cortex.schema.bindings import select_target_groups, target_views
from cortex.schema.graph import alternative_content
from cortex.schema.references import content_reference_ids


def _dependencies(properties):
    refs = content_reference_ids(properties)
    for key in ("table_ref", "column_ref", "value_ref", "default_time_column"):
        if isinstance(properties.get(key), str):
            refs.add(properties[key])
    for group in properties.get("relation_metadata", ()):
        refs.update(group["edges"])
    for pair in properties.get("column_pairs", ()):
        refs.update(pair[key] for key in ("from_column_ref", "to_column_ref") if key in pair)
    return refs


def build_filter_evidence(
    state: GraphState, points: tuple[ChoicePoint, ...]
) -> tuple[dict, dict[str, list[str]]]:
    """Follow cited definitions and declared dependencies, without semantic neighbors.

    Alternatives are restored from common fields plus overrides. A generic owner
    reference retains all its branches rather than guessing which branch applies.
    Incomplete closures are returned per slot so the host can conservatively keep it.
    """
    state = without_untrusted_dimension_values(state)
    nodes = {n.id: n for n in state.graph.nodes}
    edges = {e.id: e for e in state.graph.edges}
    alternatives = {
        a.alternative_id: (n, a) for n in nodes.values() for a in n.alternatives
    }
    targets = {
        f"target:{n.id}:{index + 1}": (n, index, target)
        for n in nodes.values()
        for index, target in enumerate(target_views(n.properties))
    }
    evidence, incomplete = {}, {}

    def resolve(ref):
        if ref in nodes:
            node = nodes[ref]
            view = node.model_dump(mode="json", exclude={"sources"})
            deps = _dependencies(node.properties)
            for alternative in node.alternatives:
                deps.update(_dependencies(alternative_content(node, alternative).properties))
            return view, deps
        if ref in alternatives:
            node, alternative = alternatives[ref]
            content = alternative_content(node, alternative).model_dump(mode="json")
            return {
                "owner_node_id": node.id,
                "node_type": node.node_type,
                **content,
            }, _dependencies(content["properties"])
        if ref in targets:
            node, index, target = targets[ref]
            # A common-node target cannot silently stand for a branch-specific binding.
            if node.alternatives:
                return None, set()
            properties = select_target_groups(deepcopy(node.properties), [index])
            legacy = properties.get("legacy_target_definitions")
            if isinstance(legacy, list):
                properties["legacy_target_definitions"] = [legacy[index]]
            return {
                "owner_node_id": node.id,
                "node_type": node.node_type,
                "name": node.name,
                "description": node.description,
                "target": target,
                "properties": properties,
            }, _dependencies(properties)
        if ref in edges:
            edge = edges[ref]
            return edge.model_dump(mode="json", exclude={"sources"}), {
                edge.from_node_id, edge.to_node_id,
                *_dependencies(edge.properties),
                *edge.properties.get("alternative_ids", ()),
            }
        return None, set()

    for point in points:
        pending = [r for option in point.options for r in option.graph_refs]
        seen, missing = set(), set()
        while True:
            while pending:
                ref = pending.pop()
                if ref in seen:
                    continue
                seen.add(ref)
                view, deps = resolve(ref)
                if view is None:
                    missing.add(ref)
                    continue
                evidence[ref] = {"graph_ref": ref, **view}
                pending.extend(sorted(deps - seen))
            # Preserve known JOIN contracts only between already-required tables.
            # This does not add routing tables or expand the semantic neighborhood.
            pending = [
                e.id for e in edges.values()
                if e.edge_type == "joins_with" and e.id not in seen
                and e.from_node_id in seen and e.to_node_id in seen
            ]
            if not pending:
                break
        if missing:
            incomplete[point.choice_point_id] = sorted(missing)

    return {
        "evidence": [evidence[ref] for ref in sorted(evidence)],
        "incomplete_slots": incomplete,
    }, incomplete
