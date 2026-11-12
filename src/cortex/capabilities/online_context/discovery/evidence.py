"""Graph reference vocabulary, evidence validation, and discovery payloads."""

import re

from cortex.application.online_context import ChoicePoint
from cortex.capabilities.online_context.discovery.models import IntraNodeOutput
from cortex.capabilities.online_context.graph.context import (
    without_untrusted_dimension_values,
)
from cortex.schema import GraphState
from cortex.schema.bindings import target_views


def _validate_slots(
    slots: tuple[ChoicePoint, ...], state: GraphState, initial_sql: str | None
) -> None:
    graph_ids = _graph_ids(state)
    slot_ids = [item.choice_point_id for item in slots]
    if len(slot_ids) != len(set(slot_ids)):
        raise ValueError("conflict slot IDs must be unique")
    for slot in slots:
        option_ids = [item.option_id for item in slot.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("candidate IDs must be unique within a slot")
        statements = [item.statement.strip() for item in slot.options]
        if len(statements) != len(set(statements)):
            raise ValueError(
                f"duplicate candidate statements: slot={slot.choice_point_id}, "
                f"candidates={[(o.option_id, o.statement, o.graph_refs) for o in slot.options]}"
            )
        for option in slot.options:
            if (
                not option.graph_refs
                and not (option.initial_sql_evidence or "").strip()
            ):
                raise ValueError("candidate has no Graph or Initial SQL evidence")
            unknown_refs = set(option.graph_refs) - graph_ids
            invalid_refs = unknown_refs
            if invalid_refs:
                raise ValueError(
                    "candidate references unknown Graph objects: "
                    + ", ".join(sorted(invalid_refs))
                )
            if option.initial_sql_evidence and not _sql_contains(
                initial_sql, option.initial_sql_evidence
            ):
                raise ValueError(
                    "candidate Initial SQL evidence is not a verbatim fragment of Initial SQL"
                )


def _graph_ids(state: GraphState) -> set[str]:
    result = {node.id for node in state.graph.nodes}
    result.update(edge.id for edge in state.graph.edges)
    for conflict in state.conflict_nodes:
        result.add(conflict.id)
        for alternative in conflict.alternatives:
            result.add(alternative.alternative_id)
        for point in conflict.conflict_points:
            result.add(point.point_id)
    for node in state.graph.nodes:
        for index, _target in enumerate(target_views(node.properties), 1):
            result.add(_target_ref(node.id, index))
    return result


def _graph_ref_payloads(state: GraphState) -> dict[str, object]:
    result: dict[str, object] = {
        node.id: node.model_dump(mode="json") for node in state.graph.nodes
    }
    result.update((edge.id, edge.model_dump(mode="json")) for edge in state.graph.edges)
    for conflict in state.conflict_nodes:
        for alternative in conflict.alternatives:
            result[alternative.alternative_id] = {
                "alternative": alternative.model_dump(mode="json"),
                "conflict_choices": [
                    choice.statement
                    for point in conflict.conflict_points
                    for choice in point.choices
                    if alternative.alternative_id in choice.alternative_ids
                ],
            }
        for point in conflict.conflict_points:
            result[point.point_id] = point.model_dump(mode="json")
    for node in state.graph.nodes:
        for index, target in enumerate(target_views(node.properties), 1):
            result[_target_ref(node.id, index)] = target
    return result


def _qualified_physical_refs(value: str, table_names: set[str]) -> set[str]:
    matches = re.findall(
        r"(?:(?:column|table)\.)?([A-Za-z_][A-Za-z0-9_]*)\."
        r'(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))',
        value,
    )
    return {
        _physical_ref(table, quoted or backticked or plain)
        for table, quoted, backticked, plain in matches
        if _identifier(table) in table_names
    }


def _physical_ref(table: str, column: str) -> str:
    return f"{_identifier(table)}.{_identifier(column)}"


def _identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _analysis_graph_payload(state: GraphState) -> dict:
    payload = without_untrusted_dimension_values(state).model_dump(mode="json")
    for node in payload["graph"]["nodes"]:
        node.pop("sources", None)
        node["match_views"] = [
            {**target, "graph_ref": _target_ref(node["id"], index)}
            for index, target in enumerate(target_views(node["properties"]), 1)
        ]
    for edge in payload["graph"]["edges"]:
        edge.pop("sources", None)
    return payload


def _residual_graph_payload(graph_payload: dict, intra: IntraNodeOutput) -> dict:
    """A bounded analysis view, never another stored conflict representation."""
    from copy import deepcopy

    payload = deepcopy(graph_payload)
    handled = {
        assessment.signal_node_id
        for assessment in intra.assessments
        if assessment.status in {"confirmed", "not_conflicting"}
    }
    # A handled conflict need not be reanalysed by later discovery stages.
    # Remove that node from this evidence view rather than detach its metadata.
    removed = {
        node["id"]
        for node in payload["graph"]["nodes"]
        if node["id"] in handled and node.get("alternatives")
    }
    payload["graph"]["nodes"] = [
        node for node in payload["graph"]["nodes"] if node["id"] not in removed
    ]
    payload["graph"]["edges"] = [
        edge
        for edge in payload["graph"]["edges"]
        if edge["from_node_id"] not in removed and edge["to_node_id"] not in removed
    ]
    for node in payload["graph"]["nodes"]:
        if node["id"] in handled and node["node_type"] == "concept":
            node["properties"] = {
                key: value
                for key, value in node["properties"].items()
                if key != "targets"
            }
    return payload


def _sql_contains(sql: str | None, evidence: str) -> bool:
    return bool(sql) and bool(evidence.strip()) and evidence in sql


def _target_ref(node_id: str, index: int) -> str:
    return f"target:{node_id}:{index}"


def _replace_refs(value, replacements: dict[str, str]):
    """Replace exact ref values without altering prose, SQL, or dictionary keys."""
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, tuple):
        return tuple(_replace_refs(item, replacements) for item in value)
    if isinstance(value, list):
        return [_replace_refs(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_refs(item, replacements) for key, item in value.items()}
    return value


def _dict_list(value: object) -> list[dict]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _union(values: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in values for item in group))
