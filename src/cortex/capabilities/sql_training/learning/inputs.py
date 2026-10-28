"""Mandatory learning inputs, derived from the same models used to validate edits."""

from __future__ import annotations

import sqlglot
from langchain_core.utils.function_calling import convert_to_openai_function
from sqlglot import expressions as exp
from sqlglot.optimizer.scope import traverse_scope

from cortex.capabilities.sql_training.builders.metadata import compact_column_metadata
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.extractors.sql_normalizer import (
    physical_column_sources,
)
from cortex.capabilities.sql_training.graph.values import ValueWrite
from cortex.capabilities.sql_training.llm.generation_rule_taxonomy import (
    DIRECT_GENERATION_RULE_TAXONOMY,
)
from cortex.capabilities.sql_training.llm.prompts import load_prompt
from cortex.capabilities.sql_training.models import (
    ConceptProperties,
    GenerationRuleProperties,
    MetricProperties,
    NodeAsset,
)
from cortex.schema import GraphEdge, GraphNode, NodeMergeConflict
from cortex.schema.relationships import MANAGED_RELATIONS, is_dependency_edge

PROPERTY_MODELS = {
    "concept": ConceptProperties,
    "metric": MetricProperties,
    "generation_rule": GenerationRuleProperties,
}

AUTHORING_MODELS = PROPERTY_MODELS


def edge_operation_schema():
    """Expose the writable GraphEdge fields, without code-maintained metadata."""
    edge = convert_to_openai_function(GraphEdge)["parameters"]
    edge["properties"] = {
        key: value
        for key, value in edge["properties"].items()
        if key in {"id", "edge_type", "from_node_id", "to_node_id", "properties"}
    }
    edge["required"] = ["id", "edge_type", "from_node_id", "to_node_id"]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["create_edge", "update_edge", "delete_edge"],
            },
            "edge": edge,
            "edge_id": {"type": "string"},
        },
        "required": ["operation"],
    }


def conflict_operation_schema():
    node = convert_to_openai_function(GraphNode)["parameters"]
    # The proposal carries candidate metadata once, in conflict; node is common content.
    common_fields = {"id", "node_type", "name", "description", "aliases", "tags", "properties"}
    node["properties"] = {key: value for key, value in node["properties"].items() if key in common_fields}
    node["required"] = [key for key in node.get("required", []) if key in common_fields]
    conflict = convert_to_openai_function(NodeMergeConflict)["parameters"]
    conflict["properties"]["alternatives"]["items"]["properties"].pop("sources", None)

    def compact(value):
        if isinstance(value, dict):
            return {
                key: compact(item)
                for key, item in value.items()
                if key not in {"title", "default"}
                and not (key == "description" and isinstance(item, str))
            }
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    return compact(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["set_conflict", "resolve_conflict"],
                },
                "node": node,
                "conflict": conflict,
                "node_id": {"type": "string"},
                "alternative_id": {"type": "string"},
            },
            "required": ["operation"],
        }
    )


def semantic_asset_schema():
    """One envelope shared by the tool schema and the startup output contract."""
    schema = convert_to_openai_function(NodeAsset)["parameters"]
    schema["properties"]["properties"] = {
        "type": "object",
        "description": "Use the properties schema and complete node example from inspect_graph(mode='guide', node_type=<this node_type>).",
    }
    schema["properties"]["node_type"] = {
        "type": "string",
        "enum": list(AUTHORING_MODELS),
    }
    schema["properties"]["id"]["description"] = (
        "Canonical <node_type>.<name>. Propose Values separately through edit_graph.values."
    )
    schema["properties"].pop("sources", None)
    return schema


def ontology_contract(schema):
    """Expose case-authoring types and compiled relations, not unrelated global types."""
    active = {"table", "column", "dimension_value", *PROPERTY_MODELS}
    return {
        "custom_relations": "Any nonempty edge_type is accepted without registration; declared types retain their constraints. Explicit relations override default relations on the same anchor. reference_origin is code-maintained.",
        "physical_node_types": sorted(active - PROPERTY_MODELS.keys()),
        "editable_node_types": list(AUTHORING_MODELS),
        "node_definitions": {
            node.id: node.description for node in schema.node_types if node.id in active
        },
        "relations": [
            {
                "type": e.id,
                "from": [kind for kind in e.from_node_types if kind in active],
                "to": [kind for kind in e.to_node_types if kind in active],
                "managed_from": [
                    kind
                    for kind in e.from_node_types
                    if kind in active and e.id in MANAGED_RELATIONS.get(kind, ())
                ],
                "editable_from": [
                    kind for kind in e.from_node_types if kind in PROPERTY_MODELS
                ],
                "description": e.description,
                "properties": {
                    name: field.model_dump(mode="json", exclude_none=True)
                    for name, field in e.fields.items()
                },
                "directionality": e.directionality.value,
                "identity_fields": list(e.identity_fields),
            }
            for e in schema.edge_types
            if set(e.from_node_types) & active and set(e.to_node_types) & active
        ],
        "output_schema": {
            "value_proposal": ValueWrite.model_json_schema(),
            "edge_operation": edge_operation_schema(),
            "conflict_operation": conflict_operation_schema(),
            "asset_envelope": semantic_asset_schema(),
            "properties_by_type": {
                kind: model.model_json_schema()
                for kind, model in AUTHORING_MODELS.items()
            },
        },
        "direct_rule_registry": {
            item.intent_class: item.description
            for item in DIRECT_GENERATION_RULE_TAXONOMY
        },
    }


def system_prompt(mode, schema):
    return load_prompt("learning_system").replace(
        "{stage_task}", load_prompt(f"learning_{mode}")
    )


def case_source(record):
    return {key: record[key] for key in ("question", "sql", "evidence") if key in record}


def graph_context(snapshot, record, dialect):
    """Resolve only Gold's physical sources locally; no database calls or global scan."""
    tables = {
        a.properties.table_name: a
        for a in snapshot.assets
        if a.node_type == NodeType.TABLE
    }
    columns = [a for a in snapshot.assets if a.node_type == NodeType.COLUMN]
    owners = {
        a.id: next(
            edge.to_node_id
            for edge in snapshot.graph.edges
            if edge.from_node_id == a.id
            and is_dependency_edge(edge)
            and edge.to_node_id in {table.id for table in tables.values()}
        )
        for a in columns
    }
    table_by_id = {a.id: name for name, a in tables.items()}
    catalog = {
        name: {a.properties.column_name for a in columns if owners[a.id] == table.id}
        for name, table in tables.items()
    }
    tree = sqlglot.parse_one(str(record["sql"]), read=dialect)
    used_tables = {
        source.name.casefold()
        for scope in traverse_scope(tree)
        for _, source in scope.selected_sources.values()
        if isinstance(source, exp.Table)
    }
    used_columns = set(physical_column_sources(tree, catalog).values())
    used_table_ids = {
        a.id for name, a in tables.items() if name.casefold() in used_tables
    }
    return {
        "gold_bindings": {
            "tables": [
                {"id": a.id, "table_name": name}
                for name, a in tables.items()
                if name.casefold() in used_tables
            ],
            "columns": [
                {
                    "id": a.id,
                    "table_id": owners[a.id],
                    "column_name": a.properties.column_name,
                    "data_type": a.properties.data_type,
                    **({"description": a.description} if a.description else {}),
                    **({"source_metadata": compact_column_metadata(a.properties.source_metadata)} if a.properties.source_metadata else {}),
                }
                for a in columns
                if (table_by_id[owners[a.id]], a.properties.column_name) in used_columns
            ],
            "joins": [
                e.model_dump(mode="json", exclude={"sources"})
                for e in snapshot.graph.edges
                if e.edge_type == "joins_with"
                and e.from_node_id in used_table_ids
                and e.to_node_id in used_table_ids
            ],
        },
    }


def semantic_overview(snapshot, bindings, question, pinned_ids):
    """A bounded discovery index; full nodes remain accessible through tools."""
    columns = {row["id"] for row in bindings["columns"]}
    tables = {row["id"] for row in bindings["tables"]}
    question = question.casefold()
    anchors = {}
    for edge in snapshot.graph.edges:
        if edge.to_node_id in columns | tables:
            anchors.setdefault(edge.from_node_id, set()).add(edge.to_node_id)

    def relevance(asset):
        refs = set(asset.references()) | anchors.get(asset.id, set())
        named = any(name and name.casefold() in question for name in [asset.name, *asset.aliases])
        return 4 * len(refs & columns) + len(refs & tables) + 2 * named

    ranked = sorted(snapshot.semantic_assets, key=lambda a: (-relevance(a), a.id))
    relevant = [a for a in ranked if relevance(a) > 0][:20]
    included = {a.id for a in relevant} | set(pinned_ids)
    return [
        {"id": a.id, "type": a.node_type.value, "name": a.name}
        for a in ranked if a.id in included
    ]
