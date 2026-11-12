"""Explicit, audited archive conversion; the training schema is never relaxed."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from cortex.application.ontology_graph import GraphManager
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState, load_root_schema
from cortex.schema.graph import SourceReference
from cortex.schema.physical_joins import merge_join_edges
from cortex.schema.relationships import synchronize_reference_edges
from cortex.schema.validation import calculate_schema_hash


def load_graph(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept current GraphState or migrate the known archived BIRD format.

    Return the validated current state plus an audit preserving obsolete fields.
    No archive is overwritten and no unknown property is silently discarded.
    """
    if not isinstance(payload, dict):
        raise ValueError("Graph file must contain a JSON object")
    if "state" in payload and isinstance(payload["state"], dict):
        payload = payload["state"]
    if "relevant_graph" in payload:
        if not isinstance(payload["relevant_graph"], dict):
            raise ValueError("Result contains no relevant graph")
        payload = payload["relevant_graph"]
    raw = deepcopy(payload if "graph" in payload else {"graph": payload})
    schema = load_root_schema()
    legacy = any("content" in a for c in raw.get("node_conflicts", []) for a in c.get("alternatives", [])) or any(
        e.get("edge_type") in {"maps_to_column", "maps_to_value"} or (
            e.get("edge_type") == "joins_with" and "conditions" in e.get("properties", {})
        ) for e in raw.get("graph", {}).get("edges", []))
    audit: list[dict[str, Any]] = []
    if legacy:
        aliases = {
            f"column.{n['properties']['table_ref'].removeprefix('table.')}.{n['properties']['column_name']}": n["id"]
            for n in raw["graph"]["nodes"] if n["node_type"] == "column"
        }
        def canonical_refs(value):
            if isinstance(value, dict):
                return {k: canonical_refs(v) for k, v in value.items()}
            if isinstance(value, list):
                return [canonical_refs(v) for v in value]
            if isinstance(value, str) and value in aliases and aliases[value] != value:
                audit.append({"field": "canonical_column_reference", "from": value, "to": aliases[value]})
                return aliases[value]
            return value
        raw = canonical_refs(raw)
        allowed = {n.id: set(n.fields) for n in schema.node_types}
        original_nodes = {n["id"]: n for n in raw["graph"]["nodes"]}

        def content(kind, value, owner):
            value = deepcopy(value)
            props = value.get("properties", {})
            if kind == "metric" and any(original_nodes.get(r, {}).get("node_type") == "concept" for r in props.get("expression", {}).get("refs", {}).values()):
                before = deepcopy(props["expression"])
                bindings = dict(before["refs"])
                expr = before["expr"]
                for key, ref in list(bindings.items()):
                    concept = original_nodes.get(ref, {})
                    if concept.get("node_type") != "concept":
                        continue
                    targets = concept["properties"].get("targets", [])
                    if len(targets) != 1:
                        raise ValueError(f"Cannot unambiguously inline metric concept {ref}")
                    target = targets[0]
                    sql = target["sql_expression"]
                    del bindings[key]
                    for index, anchor in enumerate(target.get("references", [])):
                        physical = original_nodes[anchor]
                        local = f"{key}_{index}"
                        if local in bindings:
                            raise ValueError("Metric placeholder collision")
                        if physical["node_type"] == "column":
                            table = original_nodes[physical["properties"]["table_ref"]]["properties"]["table_name"]
                            token = f"{table}.{physical['properties']['column_name']}"
                        elif physical["node_type"] == "dimension_value":
                            literal = physical["properties"]["value"]
                            token = "'" + str(literal).replace("'", "''") + "'" if isinstance(literal, str) else str(literal)
                        else:
                            raise ValueError("Unsupported metric concept anchor")
                        if token not in sql:
                            raise ValueError(f"Cannot safely inline target token {token}")
                        sql = sql.replace(token, "${" + local + "}")
                        bindings[local] = anchor
                    expr = expr.replace("${" + key + "}", f"({sql})")
                props["expression"] = {"expr": expr, "refs": bindings}
                props["references"] = sorted(set(bindings.values()))
                audit.append({"owner": owner, "field": "inline_metric_concepts", "archived_value": before})
            if kind not in allowed:
                raise ValueError(f"Unsupported node type: {kind}")
            for key in list(props):
                if key not in allowed[kind]:
                    audit.append({"owner": owner, "field": f"properties.{key}", "archived_value": props.pop(key)})
            for target in props.get("targets", []):
                if "binding_operator" in target:
                    audit.append({"owner": owner, "field": "target.binding_operator", "archived_value": target.pop("binding_operator")})
            if "targets" in props:
                targets, identities, used = [], set(), set()
                for target in props["targets"]:
                    identity = (target["sql_expression"].strip(), tuple(sorted(target.get("references", []))))
                    if identity in identities:
                        audit.append({"owner": owner, "field": "duplicate_target", "archived_value": target})
                        continue
                    identities.add(identity)
                    original = target["id"]
                    target["id"] = re.sub(r"[^A-Za-z0-9_]", "_", original)[:90]
                    if not target["id"] or not target["id"][0].isalpha():
                        target["id"] = "target_" + target["id"]
                    while target["id"] in used:
                        target["id"] += "_next"
                    if target["id"] != original:
                        audit.append({"owner": owner, "field": "target_id", "from": original, "to": target["id"]})
                    used.add(target["id"])
                    targets.append(target)
                props["targets"] = targets
            value["properties"] = props
            return value

        nodes = {}
        for row in raw["graph"]["nodes"]:
            row = content(row["node_type"], row, row["id"])
            nodes[row["id"]] = GraphNode.model_validate(row)
        for conflict in raw.get("node_conflicts", []):
            node = nodes[conflict["node_id"]]
            if not all("content" in a for a in conflict["alternatives"]):
                raise ValueError("Mixed legacy/current conflict alternatives are not supported")
            candidates = []
            for alternative in conflict["alternatives"]:
                candidate = content(node.node_type, alternative["content"], alternative["alternative_id"])
                candidates.append((alternative["alternative_id"], candidate, tuple(SourceReference.model_validate(s) for s in alternative.get("sources", []))))
            points = deepcopy(conflict.get("conflict_points", []))
            for point in points:
                for choice in point.get("choices", []):
                    if "source_case_ids" in choice:
                        audit.append({"owner": node.id, "field": "choice.source_case_ids", "archived_value": choice.pop("source_case_ids")})
            compact, _ = GraphManager.compact_conflict(node, candidates, conflict_points=tuple(points))
            nodes[node.id] = compact
        edges = []
        for row in raw["graph"]["edges"]:
            if row["edge_type"] == "joins_with" and "conditions" in row["properties"]:
                old = row["properties"]
                if any(p.get("operator", "=") != "=" for p in old["conditions"]):
                    raise ValueError("Cannot convert a non-equality physical join")
                row["properties"] = {"column_pairs": [{"from_column_ref": p["left_column_ref"], "to_column_ref": p["right_column_ref"]} for p in old["conditions"]]}
                audit.append({"owner": row["id"], "field": "physical_join", "archived_value": old})
            elif row["edge_type"] in {"maps_to_column", "maps_to_value"}:
                audit.append({"owner": row["id"], "field": "edge_type", "archived_value": {"edge_type": row["edge_type"], "properties": deepcopy(row["properties"])}})
                row["edge_type"] = "grounded_on"
                row["properties"].pop("binding_operator", None)
            if row["edge_type"] in {"grounded_on", "depends_on", "applies_to", "belongs_to", "value_of"}:
                row["reference_origin"] = "default"
            edges.append(GraphEdge.model_validate(row))
        joins = merge_join_edges((), [e for e in edges if e.edge_type == "joins_with"])
        rest = tuple(e for e in edges if e.edge_type != "joins_with")
        synced, dropped = synchronize_reference_edges(rest, tuple(nodes.values()), schema=schema)
        audit.append({"field": "compiled_reference_edges", "removed_edge_ids": list(dropped), "before": len(rest), "after": len(synced)})
        state = GraphState(graph=GraphSpace(nodes=tuple(nodes.values()), edges=(*joins, *synced)))
    else:
        state = GraphState.model_validate(raw)
    GraphManager(schema).validate_state(state)
    return {
        "state": state.model_dump(mode="json"),
        "schema_hash": calculate_schema_hash(schema),
        "node_type_labels": {n.id: n.name for n in schema.node_types},
        "edge_type_labels": {e.id: e.name for e in schema.edge_types},
        "compatibility": {"migrated": legacy, "changes": audit},
    }
