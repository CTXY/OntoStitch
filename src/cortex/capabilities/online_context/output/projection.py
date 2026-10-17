from __future__ import annotations

import hashlib
import json
from typing import Any

from cortex.application.online_context import (
    ChoicePoint,
    DownstreamGuidance,
    InteractionGuidance,
    ProjectionDecision,
)
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState, MergeStatus


def build_downstream_guidance(
    *,
    question: str,
    initial_sql: str | None,
    graph: GraphSpace,
    relevant_semantic_node_ids: set[str] | None = None,
    relevant_grounding_node_ids: set[str] | None = None,
    excluded_alternative_ids: set[str] | None = None,
    interaction_guidance: tuple[InteractionGuidance, ...],
    resolved_guidance_content: str,
    resolved_join_paths: tuple[str, ...] = (),
    query_examples=(),
    conflicts=(),
    include_interaction_preamble: bool = True,
) -> DownstreamGuidance:
    """Render the projected ontology and resolved choices as one agent handoff."""
    chinese = any("\u4e00" <= char <= "\u9fff" for char in question)
    conflict_by_node = {item.node_id: item for item in conflicts}
    nodes_by_id = {node.id: node for node in graph.nodes}
    asset_ids = (
        set(nodes_by_id) if relevant_semantic_node_ids is None else set(relevant_semantic_node_ids)
    )
    # Required references are distinct from expansion-only neighbors.
    pending = list(asset_ids)
    while pending:
        node = nodes_by_id.get(pending.pop())
        if node is None:
            continue
        for reference in _projection_references(
            node, conflict_by_node.get(node.id), excluded_alternative_ids or set()
        ):
            if reference in nodes_by_id and reference not in asset_ids:
                asset_ids.add(reference)
                pending.append(reference)
    assets = tuple(
        semantic_asset_payload(
            node,
            conflict_by_node.get(node.id) if node.merge_status == MergeStatus.CONFLICT else None,
            nodes_by_id=nodes_by_id,
            excluded_alternative_ids=excluded_alternative_ids,
        )
        for node in graph.nodes
        if node.node_type in _SEMANTIC_TYPES and node.id in asset_ids
    )
    content = []
    if include_interaction_preamble:
        content.append(
            "生成 SQL 时必须先遵守以下用户交互 guidance；其优先级高于 ontology assets。"
            if chinese
            else (
                "When generating SQL, you MUST follow the resolved user guidance first; "
                "it has highest priority since it is user-confirmed."
            )
        )
    if resolved_guidance_content.strip():
        content.append(
            ("用户交互 guidance：" if chinese else "Resolved user guidance:")
            + "\n"
            + resolved_guidance_content.strip()
        )
    if resolved_join_paths:
        content.append(
            ("相关 JOIN 路径（仅作参考）：" if chinese else "Relevant join paths (reference only):")
            + "\n"
            + "\n".join(f"- {path}" for path in resolved_join_paths)
        )
    content.append(
        ("Ontology assets（仅作参考）：" if chinese else "Ontology assets (reference only):")
        + "\n"
        + "\n".join(asset_summary(asset, chinese=chinese) for asset in assets)
    )
    grounding_ids = asset_ids | (relevant_grounding_node_ids or set())
    bindings = []
    for node in graph.nodes:
        if node.node_type != "dimension_value" or node.id not in grounding_ids:
            continue
        column = _reference_label(str(node.properties.get("column_ref") or ""), nodes_by_id)
        bindings.append(f"- {node.name}: {column} = {node.properties.get('value')!r}")
    if bindings:
        content.append("Selected schema value bindings (reference only):\n" + "\n".join(bindings))
    from cortex.capabilities.online_context.output.query_examples import render_query_examples

    guidance_content = "\n\n".join(content)
    guidance_content = render_query_examples(
        guidance_content, tuple(query_examples), chinese=chinese
    )
    return DownstreamGuidance(
        usage=(
            (
                "必须先遵守用户交互 guidance；ontology assets 仅作为补充参考。"
                if chinese
                else (
                    "Follow resolved user guidance first; ontology assets are supplementary "
                    "reference only."
                )
            )
            if include_interaction_preamble
            else (
                "仅将相关 ontology assets 作为参考。"
                if chinese
                else "Use only the relevant ontology assets as reference."
            )
        ),
        guidance_content=guidance_content,
        ontology_assets=assets,
        interaction_guidance=interaction_guidance,
        resolved_join_paths=resolved_join_paths,
        query_examples=tuple(query_examples),
    )


_SEMANTIC_TYPES = {"concept", "metric", "generation_rule"}


def semantic_asset_payload(
    node: GraphNode,
    conflict=None,
    *,
    nodes_by_id: dict[str, GraphNode] | None = None,
    excluded_alternative_ids: set[str] | None = None,
) -> dict[str, Any]:
    properties = node.properties
    if node.node_type == "concept":
        asset = {
            "type": "concept",
            "name": node.name,
            "description": node.description,
            "targets": [
                {
                    **item,
                    "matching": item.get("sql_expression", ""),
                    "binding_operator": item.get("binding_operator", "exact"),
                    "references": item.get("references", []),
                    "semantic_targets": item.get("semantic_targets", []),
                }
                for item in _dict_list(properties.get("targets"))
            ],
        }
    elif node.node_type == "metric":
        expression = properties.get("expression") or {}
        asset = {
            "type": "metric",
            "metric_term": node.name,
            "description": node.description,
            "expression": (
                expression.get("expr", expression) if isinstance(expression, dict) else expression
            ),
            "generation_plan": [
                dict(item) for item in _dict_list(properties.get("sql_generation_plans"))
            ],
            "expression_refs": expression.get("refs", {}) if isinstance(expression, dict) else {},
        }
        asset["operand_bindings"] = {
            key: _reference_label(str(ref), nodes_by_id or {})
            for key, ref in asset["expression_refs"].items()
        }
        for key in ("metric_kind", "pattern", "aggregation_policy", "calculation_policy"):
            if properties.get(key) not in (None, "", [], {}):
                asset[key] = properties[key]
    else:
        asset = {
            "type": "generation_rule",
            "condition": properties.get("condition", ""),
            "action": properties.get("action", ""),
            "reason": properties.get("reason", ""),
            "triggers": properties.get("triggers", {}),
        }
    asset["references"] = properties.get("references", [])
    asset["reference_bindings"] = {
        reference: _reference_label(str(reference), nodes_by_id or {})
        for reference in sorted(_references(node))
    }
    if conflict is not None:
        asset["consolidation_status"] = conflict.consolidation_status or "legacy_unclassified"
        asset["preservation_reasons"] = list(conflict.preservation_reasons)
        asset["conflict_points"] = [
            point.model_dump(mode="json") for point in conflict.conflict_points
        ]
        asset["alternatives"] = [
            semantic_asset_payload(
                GraphNode(
                    id="",
                    node_type=node.node_type,
                    name=str(item.content.get("name") or ""),
                    description=str(item.content.get("description") or ""),
                    properties=item.content.get("properties") or {},
                ),
                nodes_by_id=nodes_by_id,
            )
            | {
                "alternative_id": item.alternative_id,
                "name": item.content.get("name"),
                "description": item.content.get("description"),
            }
            for item in conflict.alternatives
            if item.alternative_id not in (excluded_alternative_ids or set())
        ]
    return {"node_id": node.id, **asset}


def asset_summary(asset: dict[str, Any], *, chinese: bool) -> str:
    if asset.get("alternatives"):
        return (
            "- Unresolved ontology alternatives (reference only; no confirmed default):\n"
            + "\n".join(
                f"  Alternative {item.get('alternative_id', index)}: "
                + asset_summary(item, chinese=chinese).removeprefix("- ")
                for index, item in enumerate(asset["alternatives"], 1)
            )
            + "\n  Consolidation status: "
            + str(asset.get("consolidation_status", "legacy_unclassified"))
            + f"; preservation reasons: {asset.get('preservation_reasons', [])}"
            + f"\n  Conflict points: {asset.get('conflict_points', [])}"
        )
    references = (
        f"; reference bindings: {asset.get('reference_bindings') or asset.get('references', [])}"
    )
    if asset["type"] == "concept":
        return (
            f"- Concept name: {asset['name']}; description: {asset['description']}; "
            f"targets: {asset['targets']}{references}"
        )
    if asset["type"] == "metric":
        return (
            f"- Metric term: {asset['metric_term']}; expression: {asset['expression']}; "
            f"description: {asset.get('description') or ''}; "
            f"generation plan: {asset['generation_plan']}; "
            f"operand bindings: {asset.get('operand_bindings') or asset.get('expression_refs', {})}"
            + "; calculation contract: "
            + str(
                {
                    key: asset[key]
                    for key in (
                        "metric_kind",
                        "pattern",
                        "aggregation_policy",
                        "calculation_policy",
                    )
                    if key in asset
                }
            )
            + references
        )
    return (
        "- "
        + (
            f"Rule name: {asset['name']}; description: {asset.get('description') or ''}; "
            if asset.get("name")
            else ""
        )
        + f"Generation rule condition: {asset['condition']}; action: {asset['action']}; "
        f"reason: {asset.get('reason') or ''}{references}"
    )


def _reference_label(reference: str, nodes: dict[str, GraphNode]) -> str:
    node = nodes.get(reference)
    if node is None:
        return reference
    if node.node_type == "column":
        table = nodes.get(str(node.properties.get("table_ref") or ""))
        table_name = node.properties.get("table_name") or (
            table.properties.get("table_name") if table else None
        )
        column_name = node.properties.get("column_name")
        if table_name and column_name:
            return f"{table_name}.{column_name}"
    return f"{node.name} [{reference}]"


def project_graph(
    *,
    state: GraphState,
    selected_node_ids: set[str],
    choice_points: tuple[ChoicePoint, ...],
    selected_by_point: dict[str, tuple[str, ...]],
    explanations: dict[str, str] | None = None,
) -> tuple[GraphSpace, tuple[ProjectionDecision, ...]]:
    source_nodes = {node.id: node for node in state.graph.nodes}
    conflicts = {item.node_id: item for item in state.node_conflicts}
    alternative_owner = {
        alternative.alternative_id: conflict.node_id
        for conflict in state.node_conflicts
        for alternative in conflict.alternatives
    }
    target_owner = {
        _target_ref(node.id, index): node.id
        for node in state.graph.nodes
        if node.node_type == "concept"
        for index, target in enumerate(_dict_list(node.properties.get("targets")), 1)
    }
    projection_owner = {
        **{ref: ("alternative", owner) for ref, owner in alternative_owner.items()},
        **{ref: ("target", owner) for ref, owner in target_owner.items()},
    }
    expected_refs = {
        **{
            ("alternative", conflict.node_id): {
                alternative.alternative_id for alternative in conflict.alternatives
            }
            for conflict in state.node_conflicts
        },
        **{
            ("target", node.id): {
                _target_ref(node.id, index)
                for index, _target in enumerate(_dict_list(node.properties.get("targets")), 1)
            }
            for node in state.graph.nodes
            if node.node_type == "concept"
        },
    }
    safe_owners = _safe_projection_owners(
        choice_points, projection_owner=projection_owner, expected_refs=expected_refs
    )
    selected_alternatives: dict[str, set[str]] = {}
    selected_targets: dict[str, set[str]] = {}
    removed_targets: dict[str, set[str]] = {}
    traces: list[ProjectionDecision] = []

    for point in choice_points:
        option_by_id = {option.option_id: option for option in point.options}
        selected_ids = selected_by_point.get(point.choice_point_id, ())
        selected_refs = {
            ref for option_id in selected_ids for ref in option_by_id[option_id].graph_refs
        }
        all_refs = {ref for option in point.options for ref in option.graph_refs}
        point_owners = {projection_owner[ref] for ref in all_refs if ref in projection_owner}
        safe_owner = next(iter(point_owners & safe_owners), None)
        removed_refs: set[str] = set()
        if selected_ids and safe_owner is not None:
            allowed = {ref for ref in selected_refs if projection_owner.get(ref) == safe_owner}
            removed_refs = expected_refs[safe_owner] - allowed
            owner_kind, owner_node_id = safe_owner
            if owner_kind == "alternative":
                selected_alternatives[owner_node_id] = allowed
            else:
                selected_targets[owner_node_id] = allowed
                removed_targets[owner_node_id] = removed_refs
        traces.append(
            ProjectionDecision(
                choice_point_id=point.choice_point_id,
                selected_option_ids=selected_ids,
                retained_graph_refs=tuple(sorted(selected_refs)),
                removed_graph_refs=tuple(sorted(removed_refs)),
            )
        )

    nodes: dict[str, GraphNode] = {}
    for node_id in sorted(selected_node_ids):
        node = source_nodes.get(node_id)
        if node is None:
            continue
        if node.merge_status == MergeStatus.CONFLICT:
            alternative_ids = selected_alternatives.get(node_id, set())
            if not alternative_ids:
                # Guidance is authoritative. When Candidate-to-alternative
                # mapping is not structurally safe, retain the conflict node
                # instead of guessing which implementation may be discarded.
                nodes[node_id] = node
                continue
            if len(alternative_ids) != 1:
                # GraphSpace has one canonical node per stable ID and cannot carry
                # a pruned NodeMergeConflict overlay. Keep the canonical conflict
                # node when the user deliberately retains multiple Candidates;
                # the exact retained refs remain explicit in the projection trace.
                nodes[node_id] = node
                continue
            alternative_id = next(iter(alternative_ids))
            conflict = conflicts[node_id]
            alternative = next(
                item for item in conflict.alternatives if item.alternative_id == alternative_id
            )
            content = alternative.content
            nodes[node_id] = GraphNode(
                id=node.id,
                node_type=node.node_type,
                name=content["name"],
                description=content["description"],
                aliases=content["aliases"],
                tags=content["tags"],
                properties=content["properties"],
                sources=alternative.sources,
                merge_status=MergeStatus.NORMAL,
            )
            continue
        if node.node_type == "concept" and node_id in removed_targets:
            targets = _dict_list(node.properties.get("targets"))
            retained = [
                target
                for index, target in enumerate(targets, 1)
                if _target_ref(node_id, index) not in removed_targets[node_id]
                or _target_ref(node_id, index) in selected_targets.get(node_id, set())
            ]
            if not retained:
                continue
            node = node.model_copy(update={"properties": {**node.properties, "targets": retained}})
        nodes[node_id] = node

    excluded_alternative_ids = {ref for item in traces for ref in item.removed_graph_refs}
    references = {
        reference
        for node in nodes.values()
        for reference in _projection_references(
            node, conflicts.get(node.id), excluded_alternative_ids
        )
        if reference in source_nodes
    }
    pending = set(references)
    while pending:
        node_id = pending.pop()
        if node_id in nodes:
            continue
        node = source_nodes[node_id]
        nodes[node_id] = node
        pending.update(
            ref
            for ref in _projection_references(
                node, conflicts.get(node.id), excluded_alternative_ids
            )
            if ref in source_nodes and ref not in nodes
        )

    edges: dict[str, GraphEdge] = {}
    for edge in state.graph.edges:
        if edge.from_node_id in nodes and edge.to_node_id in nodes:
            edges[edge.id] = edge
    for node in nodes.values():
        for edge in _compiled_edges(node, nodes):
            edges[edge.id] = edge
    return (
        GraphSpace(
            nodes=tuple(nodes[node_id] for node_id in sorted(nodes)),
            edges=tuple(edges[edge_id] for edge_id in sorted(edges)),
        ),
        tuple(traces),
    )


def _safe_projection_owners(
    choice_points: tuple[ChoicePoint, ...],
    *,
    projection_owner: dict[str, tuple[str, str]],
    expected_refs: dict[tuple[str, str], set[str]],
) -> set[tuple[str, str]]:
    """Return owners whose alternatives are completely and uniquely enumerated.

    Projection is only a density optimization. A partial, overlapping, or
    multi-owner mapping cannot justify deleting Graph knowledge.
    """

    owner_points: dict[tuple[str, str], list[ChoicePoint]] = {}
    for point in choice_points:
        owners = {
            projection_owner[ref]
            for option in point.options
            for ref in option.graph_refs
            if ref in projection_owner
        }
        for owner in owners:
            owner_points.setdefault(owner, []).append(point)

    safe = set()
    for owner, points in owner_points.items():
        if len(points) != 1:
            continue
        point = points[0]
        point_owners = {
            projection_owner[ref]
            for option in point.options
            for ref in option.graph_refs
            if ref in projection_owner
        }
        if point_owners != {owner}:
            continue
        mapped = [
            refs[0]
            for option in point.options
            if len(refs := [ref for ref in option.graph_refs if projection_owner.get(ref) == owner])
            == 1
        ]
        if len(mapped) != len(point.options):
            continue
        if len(mapped) != len(set(mapped)):
            continue
        if set(mapped) == expected_refs.get(owner, set()):
            safe.add(owner)
    return safe


def _compiled_edges(node: GraphNode, nodes: dict[str, GraphNode]) -> tuple[GraphEdge, ...]:
    declarations: list[tuple[str, str, dict[str, Any]]] = []
    if node.node_type == "concept":
        for target in _dict_list(node.properties.get("targets")):
            for reference in target.get("references") or []:
                reference = str(reference)
                target_type = nodes[reference].node_type if reference in nodes else None
                if target_type == "dimension_value":
                    edge_type = "maps_to_value"
                elif target_type == "column":
                    edge_type = "maps_to_column"
                else:
                    continue
                declarations.append(
                    (
                        edge_type,
                        reference,
                        {
                            "target_id": str(target.get("id") or ""),
                            "binding_operator": str(target.get("binding_operator") or "exact"),
                        },
                    )
                )
    elif node.node_type == "metric":
        for reference in sorted(_references(node)):
            reference = str(reference)
            target_type = nodes[reference].node_type if reference in nodes else None
            if target_type == "metric":
                declarations.append(("depends_on", reference, {}))
            elif target_type in {"table", "column", "dimension_value"}:
                declarations.append(("grounded_on", reference, {}))
    elif node.node_type == "generation_rule":
        for reference in node.properties.get("references") or []:
            target_id = str(reference)
            target_type = nodes[target_id].node_type if target_id in nodes else None
            edge_type = "applies_to" if target_type in {"metric", "concept"} else "grounded_on"
            declarations.append((edge_type, str(reference), {}))
    result = []
    for edge_type, target_id, properties in declarations:
        if target_id not in nodes:
            continue
        identity = json.dumps(
            [node.id, edge_type, target_id, properties],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result.append(
            GraphEdge(
                id=f"task_edge_{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
                edge_type=edge_type,
                from_node_id=node.id,
                to_node_id=target_id,
                properties=properties,
                sources=node.sources,
            )
        )
    return tuple(result)


def _references(node: GraphNode) -> set[str]:
    result = set(map(str, node.properties.get("references") or []))
    if node.node_type == "metric":
        expression = node.properties.get("expression")
        if isinstance(expression, dict) and isinstance(expression.get("refs"), dict):
            result.update(map(str, expression["refs"].values()))
    for target in _dict_list(node.properties.get("targets")):
        result.update(map(str, target.get("references") or []))
        result.update(
            str(item["node_id"])
            for item in _dict_list(target.get("semantic_targets"))
            if item.get("node_id")
        )
    if node.node_type == "dimension_value" and node.properties.get("column_ref"):
        result.add(str(node.properties["column_ref"]))
    if node.node_type == "column" and node.properties.get("table_ref"):
        result.add(str(node.properties["table_ref"]))
    return result


def _projection_references(node: GraphNode, conflict, excluded: set[str]) -> set[str]:
    if node.merge_status != MergeStatus.CONFLICT or conflict is None:
        return _references(node)
    return {
        ref
        for alternative in conflict.alternatives
        if alternative.alternative_id not in excluded
        for ref in _references(
            GraphNode(
                id=node.id,
                node_type=node.node_type,
                name=node.name,
                properties=alternative.content.get("properties") or {},
            )
        )
    }


def _dict_list(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _target_ref(node_id: str, index: int) -> str:
    return f"target:{node_id}:{index}"


__all__ = [
    "asset_summary",
    "build_downstream_guidance",
    "project_graph",
    "semantic_asset_payload",
]
