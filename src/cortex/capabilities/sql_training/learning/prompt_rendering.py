"""One-type guidance for progressive disclosure; no global contract dump."""

from __future__ import annotations

from copy import deepcopy

from cortex.capabilities.sql_training.llm.generation_rule_taxonomy import (
    operation_registry,
)
from cortex.capabilities.sql_training.llm.prompts import load_prompt

_SCHEMA_ANNOTATIONS = frozenset({"title", "description", "default", "examples"})
_SCHEMA_MAPS = frozenset({"$defs", "definitions", "properties", "patternProperties", "dependentSchemas"})
_SCHEMA_ARRAYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_VALUES = frozenset({
    "additionalProperties", "additionalItems", "contains", "contentSchema", "else",
    "if", "items", "not", "propertyNames", "then", "unevaluatedItems",
    "unevaluatedProperties",
})


def compact_authoring_schema(schema):
    """Remove annotation keywords, preserving validation and literal property names.

    Only descend through schema-bearing keywords. An enum/const can contain JSON
    objects named description/default; those are data and must remain untouched.
    Property and definition maps likewise retain every key, including such names.
    """
    if not isinstance(schema, dict):
        return deepcopy(schema)
    result = {}
    for key, value in schema.items():
        if key in _SCHEMA_ANNOTATIONS:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            result[key] = {name: compact_authoring_schema(item) for name, item in value.items()}
        elif key in _SCHEMA_ARRAYS and isinstance(value, list):
            result[key] = [compact_authoring_schema(item) for item in value]
        elif key in _SCHEMA_VALUES:
            result[key] = (
                [compact_authoring_schema(item) for item in value]
                if isinstance(value, list)
                else compact_authoring_schema(value)
            )
        elif key == "dependencies" and isinstance(value, dict):
            result[key] = {name: compact_authoring_schema(item) for name, item in value.items()}
        else:
            result[key] = deepcopy(value)
    return result


def _reference_relations(contract, node_type, field):
    # Catalog ownership is deterministic. Semantic edges must be authored.
    if node_type not in {"column", "dimension_value"}:
        return []
    relation = "belongs_to" if node_type == "column" else "value_of"
    return [{"field": field, "type": relation, "from": [node_type],
             "to": ["table" if node_type == "column" else "column"]}]


def render_type_guide(contract, node_type):
    """Return one usable guide with the same schemas used by edit validation."""
    if node_type not in contract["editable_node_types"]:
        raise ValueError("choose an editable semantic node type: " + ", ".join(contract["editable_node_types"]))
    result = {
        "guidance": load_prompt(f"learning_type_{node_type}"),
        "asset_envelope": compact_authoring_schema(contract["output_schema"]["asset_envelope"]),
        "identifiers": "Use canonical <node_type>.<name> graph IDs; SQL expressions use catalog Table/Column names.",
        "properties_schema": compact_authoring_schema(contract["output_schema"]["properties_by_type"][node_type]),
        "reference_relations": _reference_relations(
            contract, node_type,
            "properties.expression.refs" if node_type == "metric" else "properties.targets",
        ),
    }
    result["explicit_relation_types"] = [
        edge["type"]
        for edge in contract["relations"]
        if node_type in edge["editable_from"]
    ]
    result["relation_writes"] = "Author all semantic edges through edge_operations with properties.description explaining the concrete meaning and restrictions. New predicates require relation_contracts in the same edit; no qualifier/role field. Targets may overlap and do not automatically create edges. Metric.expression.refs binds operands; author the corresponding depends_on or grounded_on edges."
    if node_type == "generation_rule":
        result["operation_registry"] = contract["operation_registry"] if "operation_registry" in contract else operation_registry()
    return result


def render_write_guide(contract, kind):
    if kind == "dimension_value":
        return {
            "guidance": load_prompt("learning_values"),
            "value_schema": compact_authoring_schema(contract["output_schema"]["value_proposal"]),
            "reference_relations": _reference_relations(contract, kind, "values[].column_id"),
        }
    if kind == "relations":
        return {
            "guidance": load_prompt("learning_relations"),
            "edge_operation_schema": compact_authoring_schema(contract["output_schema"]["edge_operation"]),
            "relation_contract_schema": compact_authoring_schema(contract["output_schema"]["relation_contract"]),
            "predicates": [{"type": edge["type"], "from": edge["from"], "to": edge["to"], "properties": list(edge["properties"])} for edge in contract["relations"]],
            "custom_relations": contract["custom_relations"],
        }
    raise ValueError("choose dimension_value or relations")
