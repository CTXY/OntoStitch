"""One-type guidance for progressive disclosure; no global contract dump."""

from __future__ import annotations

from cortex.capabilities.sql_training.llm.prompts import load_prompt
from cortex.schema.relationships import reference_relation_type


def _reference_relations(contract, node_type, field):
    rows = []
    for edge in contract["relations"]:
        if node_type not in edge["managed_from"]:
            continue
        targets = []
        for target in edge["to"]:
            try:
                if reference_relation_type(node_type, target) == edge["type"]:
                    targets.append(target)
            except ValueError:
                continue
        rows.append({
            "field": field, "type": edge["type"], "from": [node_type],
            "to": targets, "description": edge["description"],
            "edge_properties": {"target_id": "targets[].id"} if node_type == "concept" else {},
        })
    return rows


def render_type_guide(contract, node_type):
    """Return one usable guide with the same schemas used by edit validation."""
    if node_type not in contract["editable_node_types"]:
        raise ValueError("choose concept, metric or generation_rule")
    result = {
        "guidance": load_prompt(f"learning_type_{node_type}"),
        "asset_envelope": contract["output_schema"]["asset_envelope"],
        "properties_schema": contract["output_schema"]["properties_by_type"][node_type],
        "reference_relations": _reference_relations(
            contract, node_type,
            "properties.targets[].references" if node_type == "concept" else "properties.references",
        ),
    }
    result["explicit_relation_types"] = [
        edge["type"]
        for edge in contract["relations"]
        if node_type in edge["editable_from"]
    ]
    result["relation_writes"] = contract["custom_relations"]
    if node_type == "generation_rule":
        result["direct_rule_registry"] = contract["direct_rule_registry"]
    return result


def render_write_guide(contract, kind):
    if kind == "dimension_value":
        return {
            "guidance": load_prompt("learning_values"),
            "value_schema": contract["output_schema"]["value_proposal"],
            "reference_relations": _reference_relations(contract, kind, "values[].column_id"),
        }
    if kind == "relations":
        return {
            "guidance": load_prompt("learning_relations"),
            "edge_operation_schema": contract["output_schema"]["edge_operation"],
        }
    raise ValueError("choose dimension_value or relations")
