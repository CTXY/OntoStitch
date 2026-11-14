from __future__ import annotations

import json
from typing import Any

from cortex.application.online_context import (
    ChoicePoint,
    DownstreamGuidance,
    InteractionGuidance,
    ProjectionDecision,
)
from cortex.application.ontology_graph import GraphManager
from cortex.schema import (
    GraphNode,
    GraphSpace,
    GraphState,
    MergeStatus,
    SchemaDefinition,
)
from cortex.schema.bindings import select_target_groups, target_groups, target_views
from cortex.schema.relationships import is_dependency_edge


def build_downstream_guidance(
    *,
    question: str,
    initial_sql: str | None,
    graph: GraphSpace,
    relevant_semantic_node_ids: set[str] | None = None,
    relevant_grounding_node_ids: set[str] | None = None,
    excluded_alternative_ids: set[str] | None = None,
    confirmed_graph_refs: set[str] | None = None,
    unselected_graph_refs: set[str] | None = None,
    interaction_guidance: tuple[InteractionGuidance, ...],
    resolved_guidance_content: str,
    resolved_join_paths: tuple[str, ...] = (),
    query_examples=(),
    conflicts=(),
    include_interaction_preamble: bool = True,
) -> DownstreamGuidance:
    """Render the projected ontology and resolved choices as one agent handoff."""
    chinese = any("\u4e00" <= char <= "\u9fff" for char in question)
    # Legacy keyword input is accepted only at this rendering boundary.
    conflict_by_node = {item.node_id: item for item in conflicts}
    nodes_by_id = {node.id: node for node in graph.nodes}
    asset_ids = (
        set(nodes_by_id)
        if relevant_semantic_node_ids is None
        else set(relevant_semantic_node_ids)
    )
    # Physical dependencies stay in the graph; they do not promote additional
    # business rules to task guidance. Explicit user selections can add evidence.
    asset_ids.update(confirmed_graph_refs or ())
    asset_ids.difference_update(
        (unselected_graph_refs or set()) - (confirmed_graph_refs or set())
    )
    assets = tuple(
        task_asset_payload(
            node,
            node if node.alternatives else conflict_by_node.get(node.id),
            nodes_by_id=nodes_by_id,
            relations=graph.edges,
        )
        for node in graph.nodes
        if node.node_type in _SEMANTIC_TYPES and node.id in asset_ids
    )
    assets = tuple(
        {
            **asset,
            "summary": task_asset_summary(asset, chinese=chinese)
            .replace(f" [{asset['node_id']}]", "")
            .removeprefix("- "),
        }
        for asset in assets
    )
    content = []
    automatic = any(
        item.source in {"question_resolved", "database_equivalent"}
        for item in interaction_guidance
    )
    if include_interaction_preamble:
        content.append(
            (
                "生成 SQL 时先遵守已确定的任务 guidance；数据库等效观察仅适用于本次查询。"
                if automatic
                else "生成 SQL 时必须先遵守以下用户交互 guidance；其优先级高于 ontology assets。"
            )
            if chinese
            else (
                "Follow resolved task guidance first; database equivalence observations "
                "apply only to this query."
                if automatic
                else "When generating SQL, you MUST follow the resolved user guidance first; "
                "it has highest priority since it is user-confirmed."
            )
        )
    if resolved_guidance_content.strip():
        content.append(
            (
                ("已确定的任务 guidance：" if chinese else "Resolved task guidance:")
                if automatic
                else ("用户交互 guidance：" if chinese else "Resolved user guidance:")
            )
            + "\n"
            + resolved_guidance_content.strip()
        )
    if resolved_join_paths:
        content.append(
            (
                "相关 JOIN 路径（仅作参考）："
                if chinese
                else "Relevant join paths (reference only):"
            )
            + "\n"
            + "\n".join(f"- {path}" for path in resolved_join_paths)
        )
    content.append(
        (
            "Ontology assets（仅作参考）："
            if chinese
            else "Ontology assets (reference only):"
        )
        + "\n"
        + "\n".join(task_asset_summary(asset, chinese=chinese) for asset in assets)
    )
    grounding_ids = asset_ids | (relevant_grounding_node_ids or set())
    bindings = []
    for node in graph.nodes:
        if node.node_type != "dimension_value" or node.id not in grounding_ids:
            continue
        column = _reference_label(
            str(node.properties.get("column_ref") or ""), nodes_by_id
        )
        bindings.append(f"- {node.name}: {column} = {node.properties.get('value')!r}")
    if bindings:
        content.append(
            "Selected schema value bindings (reference only):\n" + "\n".join(bindings)
        )
    from cortex.capabilities.online_context.output.query_examples import (
        render_query_examples,
    )

    guidance_content = "\n\n".join(content)
    guidance_content = render_query_examples(
        guidance_content, tuple(query_examples), chinese=chinese
    )
    return DownstreamGuidance(
        usage=(
            (
                (
                    "先遵守已确定的任务 guidance；ontology assets 作为补充参考。"
                    if automatic
                    else "必须先遵守用户交互 guidance；ontology assets 仅作为补充参考。"
                )
                if chinese
                else (
                    "Follow resolved task guidance first; ontology assets are reference only."
                    if automatic
                    else "Follow resolved user guidance first; ontology assets are supplementary "
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


_SEMANTIC_TYPES = {"concept", "event", "metric", "generation_rule"}


def guidance_selection_refs(choice_points, selected_by_point):
    """References are evidence, not permission to delete global graph knowledge."""
    confirmed, unselected = set(), set()
    for point in choice_points:
        if point.choice_point_id not in selected_by_point:
            continue
        selected = selected_by_point[point.choice_point_id]
        for option in point.options:
            (confirmed if option.option_id in selected else unselected).update(
                option.graph_refs
            )
    return confirmed, unselected - confirmed


def task_asset_payload(node, conflict=None, *, nodes_by_id=None, relations=()):
    """One task-facing record per node; raw variants remain inside the graph."""
    if node.alternatives or conflict is not None:
        return {
            "node_id": node.id,
            "type": node.node_type,
            "name": (
                {
                    "concept": "概念节点",
                    "metric": "指标节点",
                    "generation_rule": "生成规则节点",
                }.get(node.node_type, "知识节点")
                if node.name == node.id
                else node.name
            ),
            "status": "source_only",
            "description": "节点含有尚未安全映射为本次适用定义的内部变体；不作为 SQL 生成规则。",
        }
    asset = semantic_asset_payload(node, nodes_by_id=nodes_by_id, relations=relations)
    # Internal relations, triggers and historical cases belong to node inspection.
    asset.pop("relations", None)
    asset.pop("triggers", None)
    return {**asset, "name": node.name, "status": "reference"}


def task_asset_summary(asset, *, chinese):
    if asset.get("status") == "source_only":
        return (
            f"- {asset['name']} [{asset['node_id']}]：内部变体仅保留在节点详情中；"
            "未确定本次适用定义，不作为 SQL 生成规则。"
            if chinese
            else f"- {asset['name']} [{asset['node_id']}]: internal variants remain in node details; "
            "no task-applicable definition confirmed. Do not use as an SQL rule."
        )
    title = f"- {asset.get('name') or asset.get('metric_term')} [{asset['node_id']}]"
    lines = [title]
    if asset.get("description"):
        lines.append(str(asset["description"]))
    if asset["type"] in {"concept", "event"}:
        lines.extend(
            target["expression"] or " + ".join(target["physical_ids"])
            for target in target_views(asset)
        )
    elif asset["type"] == "metric":
        lines.append(str(asset.get("expression", "")))
        if asset.get("sql_generation_plans"):
            lines.append(str(asset["sql_generation_plans"]))
    else:
        lines.extend(str(asset.get(k, "")) for k in ("condition", "action", "reason"))
    bindings = asset.get("reference_bindings", {})
    if bindings:
        lines.append(
            ("关联字段：" if chinese else "Bindings: ") + ", ".join(bindings.values())
        )
    return "\n  ".join(line for line in lines if line)


def semantic_asset_payload(
    node: GraphNode,
    conflict=None,
    *,
    nodes_by_id: dict[str, GraphNode] | None = None,
    relations=(),
    excluded_alternative_ids: set[str] | None = None,
) -> dict[str, Any]:
    if conflict is not None and not node.alternatives:
        node = node.with_conflict(conflict)
    conflict = node if node.alternatives else None
    properties = node.properties
    if node.node_type in {"concept", "event"}:
        asset = {
            "type": node.node_type,
            "name": node.name,
            "description": node.description,
            "targets": target_groups(properties),
            "target_expressions": dict(properties.get("target_expressions") or {}),
        }
    elif node.node_type == "metric":
        expression = properties.get("expression") or {}
        asset = {
            "type": "metric",
            "metric_term": node.name,
            "description": node.description,
            "expression": (
                expression.get("expr", expression)
                if isinstance(expression, dict)
                else expression
            ),
            "sql_generation_plans": [
                dict(item)
                for item in _dict_list(properties.get("sql_generation_plans"))
            ],
            "expression_refs": expression.get("refs", {})
            if isinstance(expression, dict)
            else {},
        }
        asset["operand_bindings"] = {
            key: _reference_label(str(ref), nodes_by_id or {})
            for key, ref in asset["expression_refs"].items()
        }
        if properties.get("pattern"):
            asset["pattern"] = properties["pattern"]
    else:
        asset = {
            "type": "generation_rule",
            "condition": properties.get("condition", ""),
            "action": properties.get("action", ""),
            "reason": properties.get("reason", ""),
            "triggers": properties.get("triggers", {}),
        }
    asset["relations"] = [
        e.model_dump(mode="json")
        for e in relations
        if e.from_node_id == node.id or e.to_node_id == node.id
    ]
    asset["reference_bindings"] = {
        reference: _reference_label(str(reference), nodes_by_id or {})
        for reference in sorted(
            {
                e.to_node_id
                for e in relations
                if e.from_node_id == node.id and is_dependency_edge(e)
            }
        )
    }
    if conflict is not None:
        asset = {
            "type": node.node_type,
            "common": {
                key: getattr(node, key)
                for key in ("name", "description", "aliases", "tags", "properties")
            },
            "relations": asset["relations"],
            "consolidation_status": conflict.consolidation_status,
            "preservation_reasons": list(conflict.preservation_reasons),
            "conflict_points": [
                point.model_dump(mode="json") for point in conflict.conflict_points
            ],
            "alternatives": [
                {
                    "alternative_id": item.alternative_id,
                    "overrides": item.overrides,
                    "sources": [
                        source.model_dump(mode="json") for source in item.sources
                    ],
                }
                for item in conflict.alternatives
                if item.alternative_id not in (excluded_alternative_ids or set())
            ],
        }
    return {"node_id": node.id, **asset}


def asset_summary(asset: dict[str, Any], *, chinese: bool) -> str:
    if asset.get("alternatives"):
        return (
            "- Unresolved ontology alternatives (reference only; no confirmed default):\n"
            + "  Common content: "
            + json.dumps(asset["common"], ensure_ascii=False)
            + "\n"
            + "\n".join(
                f"  Alternative {item.get('alternative_id', index)}: "
                + json.dumps(item["overrides"], ensure_ascii=False)
                for index, item in enumerate(asset["alternatives"], 1)
            )
            + "\n  Consolidation status: "
            + str(asset.get("consolidation_status", "unclassified"))
            + f"; preservation reasons: {asset.get('preservation_reasons', [])}"
            + f"\n  Conflict points: {asset.get('conflict_points', [])}"
        )
    references = f"; reference bindings: {asset.get('reference_bindings') or asset.get('references', [])}"
    if asset["type"] in {"concept", "event"}:
        return (
            f"- {asset['type'].capitalize()} name: {asset['name']}; description: {asset['description']}; "
            f"targets: {asset['targets']}; target expressions: {asset.get('target_expressions', {})}{references}"
        )
    if asset["type"] == "metric":
        return (
            f"- Metric term: {asset['metric_term']}; expression: {asset['expression']}; "
            f"description: {asset.get('description') or ''}; "
            f"generation plan: {asset['sql_generation_plans']}; "
            f"operand bindings: {asset.get('operand_bindings') or asset.get('expression_refs', {})}"
            + f"; pattern: {asset.get('pattern') or ''}"
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
    schema: SchemaDefinition,
    state: GraphState,
    selected_node_ids: set[str],
    choice_points: tuple[ChoicePoint, ...],
    selected_by_point: dict[str, tuple[str, ...]],
    explanations: dict[str, str] | None = None,
) -> tuple[GraphSpace, tuple[ProjectionDecision, ...]]:
    resolved_alternatives = {}
    source_nodes = {node.id: node for node in state.graph.nodes}
    conflicts = {item.id: item for item in state.conflict_nodes}
    alternative_owner = {
        alternative.alternative_id: conflict.id
        for conflict in state.conflict_nodes
        for alternative in conflict.alternatives
    }
    target_owner = {
        _target_ref(node.id, index): node.id
        for node in state.graph.nodes
        if node.node_type == "concept"
        for index, target in enumerate(target_views(node.properties), 1)
    }
    projection_owner = {
        **{ref: ("alternative", owner) for ref, owner in alternative_owner.items()},
        **{ref: ("target", owner) for ref, owner in target_owner.items()},
    }
    expected_refs = {
        **{
            ("alternative", conflict.id): {
                alternative.alternative_id for alternative in conflict.alternatives
            }
            for conflict in state.conflict_nodes
        },
        **{
            ("target", node.id): {
                _target_ref(node.id, index)
                for index, _target in enumerate(target_views(node.properties), 1)
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
            ref
            for option_id in selected_ids
            for ref in option_by_id[option_id].graph_refs
        }
        all_refs = {ref for option in point.options for ref in option.graph_refs}
        point_owners = {
            projection_owner[ref] for ref in all_refs if ref in projection_owner
        }
        safe_owner = next(iter(point_owners & safe_owners), None)
        removed_refs: set[str] = set()
        if selected_ids and safe_owner is not None:
            allowed = {
                ref for ref in selected_refs if projection_owner.get(ref) == safe_owner
            }
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
    target_reindexes: dict[str, dict[str, str]] = {}
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
                # Subset compaction is performed by project_graph_state; keep
                # this node intact until complete candidate contents are available.
                nodes[node_id] = node
                continue
            alternative_id = next(iter(alternative_ids))
            conflict = conflicts[node_id]
            alternative = next(
                item
                for item in conflict.alternatives
                if item.alternative_id == alternative_id
            )
            resolved_alternatives[node_id] = alternative_id
            content = GraphManager.resolve_alternative(node, alternative).model_dump(
                mode="json"
            )
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
            retained_indices = [
                index - 1
                for index, _target in enumerate(target_views(node.properties), 1)
                if _target_ref(node_id, index) not in removed_targets[node_id]
                or _target_ref(node_id, index) in selected_targets.get(node_id, set())
            ]
            if not retained_indices:
                continue
            target_reindexes[node_id] = {
                str(old): str(new) for new, old in enumerate(retained_indices)
            }
            node = node.model_copy(
                update={
                    "properties": select_target_groups(
                        node.properties, retained_indices
                    )
                }
            )
        nodes[node_id] = node

    # Selection filters already stored relations; it never recompiles refs.
    changed = {key for key, node in nodes.items() if node != source_nodes[key]}
    available = {**source_nodes, **nodes}
    kept_edges = []
    for edge in state.graph.edges:
        owner = available[edge.from_node_id]
        if edge.from_node_id in changed:
            if owner.node_type == "concept" and "target_id" in edge.properties:
                reindex = target_reindexes.get(edge.from_node_id)
                old_id = str(edge.properties["target_id"])
                if reindex is not None:
                    if old_id not in reindex:
                        continue
                    edge = edge.model_copy(
                        update={
                            "properties": {
                                **edge.properties,
                                "target_id": reindex[old_id],
                            }
                        }
                    )
                elif old_id not in {t["id"] for t in target_views(owner.properties)}:
                    continue
            if (
                "alternative_ids" in edge.properties
                and edge.from_node_id in resolved_alternatives
                and resolved_alternatives[edge.from_node_id]
                not in edge.properties.get("alternative_ids", ())
            ):
                continue
        if (
            edge.from_node_id in resolved_alternatives
            and "alternative_ids" in edge.properties
        ):
            selected = resolved_alternatives[edge.from_node_id]
            if selected not in edge.properties["alternative_ids"]:
                continue
            edge = edge.model_copy(
                update={
                    "properties": {
                        key: value
                        for key, value in edge.properties.items()
                        if key != "alternative_ids"
                    }
                }
            )
        kept_edges.append(edge)
    # Preserve authored outgoing business context as well as formula/physical
    # relations. This reads saved edges, never infers new edges from references
    # or requires a predicate to belong to a dependency whitelist.
    adjacency: dict[str, set[str]] = {}
    for edge in kept_edges:
        adjacency.setdefault(edge.from_node_id, set()).add(edge.to_node_id)
    pending = list(nodes)
    retained = set(nodes)
    while pending:
        source_id = pending.pop()
        for reference in adjacency.get(source_id, ()):
            if reference not in retained:
                retained.add(reference)
                pending.append(reference)
    return (
        GraphSpace(
            relation_contracts=state.graph.relation_contracts,
            nodes=tuple(available[node_id] for node_id in sorted(retained)),
            edges=tuple(
                e
                for e in kept_edges
                if e.from_node_id in retained and e.to_node_id in retained
            ),
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
            if len(
                refs := [
                    ref
                    for ref in option.graph_refs
                    if projection_owner.get(ref) == owner
                ]
            )
            == 1
        ]
        if len(mapped) != len(point.options):
            continue
        if len(mapped) != len(set(mapped)):
            continue
        if set(mapped) == expected_refs.get(owner, set()):
            safe.add(owner)
    return safe


def project_graph_state(
    *,
    schema: SchemaDefinition,
    state: GraphState,
    selected_node_ids: set[str],
    choice_points: tuple[ChoicePoint, ...],
    selected_by_point: dict[str, tuple[str, ...]],
    explanations: dict[str, str] | None = None,
) -> tuple[GraphState, tuple[ProjectionDecision, ...]]:
    """Project session choices while preserving complete unresolved candidates.

    GraphSpace already carries complete inline nodes. The first pass supplies
    safe mapping decisions and single-candidate materialization. Subsets are then
    compacted again from complete contents, so newly shared fields move onto the
    common node rather than becoming invalid redundant overrides.
    """
    graph, trace = project_graph(
        schema=schema,
        state=state,
        selected_node_ids=selected_node_ids,
        choice_points=choice_points,
        selected_by_point=selected_by_point,
        explanations=explanations,
    )
    removed = {ref for decision in trace for ref in decision.removed_graph_refs}
    nodes = {node.id: node for node in graph.nodes}
    for conflict in state.conflict_nodes:
        node = nodes.get(conflict.id)
        if node is None or node.merge_status != MergeStatus.CONFLICT:
            continue
        retained = tuple(
            alternative
            for alternative in conflict.alternatives
            if alternative.alternative_id not in removed
        )
        if len(retained) == len(conflict.alternatives):
            continue
        retained_ids = {alternative.alternative_id for alternative in retained}
        points = []
        for point in conflict.conflict_points:
            choices = tuple(
                choice.model_copy(
                    update={
                        "alternative_ids": tuple(
                            key for key in choice.alternative_ids if key in retained_ids
                        )
                    }
                )
                for choice in point.choices
                if retained_ids.intersection(choice.alternative_ids)
            )
            distinct = {frozenset(choice.alternative_ids) for choice in choices}
            if len(distinct) >= 2:
                points.append(point.model_copy(update={"choices": choices}))
        common, _ = GraphManager.compact_conflict(
            node,
            [
                (
                    alternative.alternative_id,
                    GraphManager.resolve_alternative(node, alternative),
                    alternative.sources,
                )
                for alternative in retained
            ],
            consolidation_status=conflict.consolidation_status,
            preservation_reasons=conflict.preservation_reasons,
            conflict_points=tuple(points),
        )
        nodes[node.id] = common
    retained_edges = []
    for edge in graph.edges:
        branches = edge.properties.get("alternative_ids")
        if branches:
            owner = nodes[edge.from_node_id]
            if owner.alternatives:
                allowed = {a.alternative_id for a in owner.alternatives}
                selected = sorted(set(branches) & allowed)
                if not selected:
                    continue
                edge = edge.model_copy(
                    update={
                        "properties": {**edge.properties, "alternative_ids": selected}
                    }
                )
        retained_edges.append(edge)
    projected = GraphState(
        graph=GraphSpace(
            nodes=tuple(nodes.values()),
            edges=tuple(retained_edges),
            relation_contracts=graph.relation_contracts,
        ),
    )
    # Validate the explicitly retained edges and branch ownership; no relations
    # are reconstructed and no fields from separate candidates are combined.
    prepared, _ = GraphManager(schema).prepare_state(projected, strict=True)
    return prepared, trace


def _dict_list(value: object) -> list[dict]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _target_ref(node_id: str, index: int) -> str:
    return f"target:{node_id}:{index}"


__all__ = [
    "asset_summary",
    "build_downstream_guidance",
    "project_graph",
    "project_graph_state",
    "semantic_asset_payload",
]
