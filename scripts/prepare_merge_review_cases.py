"""Migrate five legacy thrombosis case bundles to the current graph contract."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from cortex.application.ontology_graph import GraphManager
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    calculate_schema_hash,
    load_root_schema,
)
from cortex.schema.references import content_reference_ids, remap_references

CASE_IDS = tuple(
    f"bird_thrombosis_prediction_{suffix}" for suffix in (1151, 1153, 1154, 1157, 1171)
)
DEFAULT_SOURCE = (
    Path(__file__).resolve().parents[1] / "results_final/"
    "workspace/BIRD/autoonto/thrombosis_prediction/selected_case_bundles/cases"
)
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1] / "artifacts/merge_review/thrombosis_prediction"
)


def read_json(filename):
    return json.loads(filename.read_text(encoding="utf-8"))


def write_json(filename, value):
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def prepare(source: Path, output: Path):
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing review bundle: {output}")
    schema = load_root_schema()
    manager = GraphManager(schema)
    database_root = source.parent.parent
    catalog_file = database_root / "merged_graph/graph_state.json"
    joins_file = source.parent / "catalog_edges.json"
    physical_types = {"table", "column", "dimension_value"}
    catalog = {
        node["id"]: node
        for node in read_json(catalog_file)["graph"]["nodes"]
        if node["node_type"] in physical_types
    }
    raw_cases = {
        case_id: read_json(source / case_id / "draft_nodes.json")
        for case_id in CASE_IDS
    }
    required_values = {
        reference
        for nodes in raw_cases.values()
        for node in nodes
        for reference in content_reference_ids(node["properties"])
        if reference.startswith("dimension_value.")
    }
    physical_nodes = []
    for node in catalog.values():
        if node["node_type"] == "dimension_value" and node["id"] not in required_values:
            continue
        node = copy.deepcopy(node)
        node["sources"] = [
            item
            for item in node["sources"]
            if item.get("case_id") in CASE_IDS or item.get("locator") == "database"
        ]
        if not node["sources"]:
            raise ValueError(
                f"Physical node lacks selected-case/catalog evidence: {node['id']}"
            )
        physical_nodes.append(GraphNode.model_validate(node))
    physical_ids = {node.id for node in physical_nodes}
    if not required_values <= physical_ids:
        raise ValueError(f"Missing governed values: {required_values - physical_ids}")

    joins = []
    changes = []
    for edge in read_json(joins_file):
        if edge["edge_type"] != "joins_with":
            raise ValueError(f"Unexpected catalog relation: {edge['edge_type']}")
        conditions = edge["properties"]["conditions"]
        if not conditions or any(item["operator"] != "=" for item in conditions):
            raise ValueError("Only complete equality JOINs can be migrated")
        extra = set(edge["properties"]) - {"conditions", "default_join_type"}
        if extra:
            raise ValueError(f"Unreviewed legacy JOIN properties: {extra}")
        migrated = copy.deepcopy(edge)
        migrated["properties"] = {
            "column_pairs": [
                {
                    "from_column_ref": item["left_column_ref"],
                    "to_column_ref": item["right_column_ref"],
                }
                for item in conditions
            ]
        }
        migrated["sources"] = [
            item
            for item in edge["sources"]
            if item.get("locator") == "database" or item.get("case_id") in CASE_IDS
        ]
        if not migrated["sources"]:
            changes.append(
                {
                    "edge_id": edge["id"],
                    "omitted": "No selected-case or database evidence; unrelated case JOIN excluded",
                }
            )
            continue
        joins.append(GraphEdge.model_validate(migrated))
        changes.append(
            {
                "edge_id": edge["id"],
                "before": edge["properties"],
                "after": migrated["properties"],
            }
        )
    physical, _ = manager.prepare_state(
        GraphState(
            graph=GraphSpace(nodes=tuple(physical_nodes), edges=tuple(joins)),
        ),
        strict=True,
    )
    manager.validate_state(physical)
    generated = {"physical_state.json": physical.model_dump(mode="json")}
    evidence = []
    case_reports = []
    fragments = []
    for case_id, nodes in raw_cases.items():
        mappings = {node["id"]: f"{node['id']}__{case_id}" for node in nodes}
        converted = []
        removed = []
        for original in nodes:
            node = copy.deepcopy(original)
            node["id"] = mappings[node["id"]]
            node["sources"] = [
                {
                    "source_id": item["source_id"],
                    "case_id": case_id,
                    "locator": item["source_type"],
                }
                for item in node["sources"]
            ]
            properties = node["properties"]
            if "mapping_type" in properties and properties["mapping_type"] is None:
                removed.append(
                    {
                        "node_id": original["id"],
                        "field": "mapping_type",
                        "value": properties.pop("mapping_type"),
                    }
                )
            if node["node_type"] == "metric":
                for key in ("metric_kind", "aggregation_policy", "calculation_policy"):
                    if key in properties:
                        if properties[key] not in (None, {}, []):
                            raise ValueError(
                                f"Nonempty obsolete Metric field requires review: {key}"
                            )
                        removed.append(
                            {
                                "node_id": original["id"],
                                "field": key,
                                "value": properties.pop(key),
                            }
                        )
            for target in properties.get("targets", []):
                if "binding_operator" in target:
                    if target["binding_operator"] != "exact":
                        raise ValueError(
                            "Non-exact legacy binding requires semantic review"
                        )
                    removed.append(
                        {
                            "node_id": original["id"],
                            "target_id": target["id"],
                            "field": "binding_operator",
                            "value": target.pop("binding_operator"),
                        }
                    )
            node["properties"] = remap_references(properties, mappings)
            converted.append(GraphNode.model_validate(node))
        fragment = manager.prepare_fragment(
            GraphState(graph=GraphSpace(nodes=tuple(converted))),
            existing=physical,
        )
        standalone, _ = manager.prepare_state(
            GraphState(
                graph=GraphSpace(
                    nodes=(*physical.graph.nodes, *fragment.graph.nodes),
                    edges=(*physical.graph.edges, *fragment.graph.edges),
                ),
                ),
            strict=True,
        )
        manager.validate_state(standalone)
        fragments.append(fragment)
        generated[f"cases/{case_id}/incoming_state.json"] = fragment.model_dump(
            mode="json"
        )
        generated[f"cases/{case_id}/standalone_state.json"] = standalone.model_dump(
            mode="json"
        )
        source_row = read_json(source / case_id / "source.json")
        evidence.append(source_row)
        generated[f"cases/{case_id}/source.json"] = source_row
        legacy_edges = [
            json.loads(line)
            for line in (source / case_id / "draft_edges.jsonl")
            .read_text()
            .splitlines()
            if line.strip()
        ]
        case_reports.append(
            {
                "case_id": case_id,
                "node_mappings": mappings,
                "removed_fields": removed,
                "semantic_nodes": len(converted),
                "legacy_edges": len(legacy_edges),
                "current_edges": len(fragment.graph.edges),
                "legacy_edge_types": sorted(
                    {item["edge_type"] for item in legacy_edges}
                ),
                "current_edge_types": sorted(
                    {item.edge_type for item in fragment.graph.edges}
                ),
            }
        )
    batch = manager.prepare_fragment(
        GraphState(
            graph=GraphSpace(
                nodes=tuple(
                    node for fragment in fragments for node in fragment.graph.nodes
                ),
                edges=tuple(
                    edge for fragment in fragments for edge in fragment.graph.edges
                ),
            ),
        ),
        existing=physical,
    )
    generated["incoming_state.json"] = batch.model_dump(mode="json")
    generated["source_evidence.json"] = evidence
    input_files = [
        catalog_file,
        joins_file,
        *(
            source / case_id / filename
            for case_id in CASE_IDS
            for filename in ("draft_nodes.json", "draft_edges.jsonl", "source.json")
        ),
    ]
    generated["migration_report.json"] = {
        "schema_hash": calculate_schema_hash(schema),
        "source_cases": str(source),
        "case_ids": CASE_IDS,
        "input_sha256": {
            str(filename): hashlib.sha256(filename.read_bytes()).hexdigest()
            for filename in input_files
        },
        "physical_nodes": len(physical.graph.nodes),
        "physical_edges": len(physical.graph.edges),
        "cases": case_reports,
        "join_changes": changes,
        "validation": "GraphManager.prepare_state(strict=True), prepare_fragment, validate_state",
        "notes": [
            "Physical support comes only from legacy physical nodes, never merged semantic nodes.",
            "All catalog Tables/Columns and only selected-case governed Values are included.",
            "Concept mappings are rebuilt as grounded_on with target_id; supporting Columns are retained.",
            "SQL, prose, formula and necessary literal values remain unchanged.",
            "default_join_type is removed: physical equality connectivity does not prescribe query JOIN type.",
            "Database structure is migrated from saved artifacts; the live database was not re-inspected.",
            "Batch incoming_state requires physical_state as existing; standalone files are self-contained.",
            "Structural validation does not establish extraction correctness or semantic merge quality.",
        ],
    }
    for filename, value in generated.items():
        write_json(output / filename, value)
    print(
        json.dumps(
            {
                "output": str(output),
                "cases": len(CASE_IDS),
                "semantic_nodes": len(batch.graph.nodes),
                "physical_nodes": len(physical.graph.nodes),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-cases", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    prepare(arguments.source_cases.resolve(), arguments.output.resolve())
