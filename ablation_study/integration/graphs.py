"""Source-scoped union and normalized-name winner-take-all, with no models."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict
from pathlib import Path

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.operations import remap_property_node_ids
from cortex.capabilities.sql_training.graph.nodes import build_node
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.schema import GraphSpace, GraphState, load_root_schema

SEMANTIC = {"concept", "metric", "generation_rule"}
ARMS = ("without_integration", "naive_name_merge")


def normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def scoped_assets(
    cases: list[tuple[str, list[dict]]],
) -> tuple[list[NodeAsset], list[dict]]:
    """Rewrite every case occurrence, including nested refs; reject ambiguous external refs."""
    maps = {}
    occurrences = defaultdict(list)
    for case_id, rows in sorted(cases):
        if case_id in maps:
            raise ValueError(f"duplicate Case ID: {case_id}")
        local = {}
        for row in rows:
            if row["node_type"] not in SEMANTIC or row["id"] in local:
                raise ValueError(f"invalid or duplicate semantic asset in {case_id}")
            scoped = (
                f"{row['node_type']}.source.{fingerprint([case_id, row['id']])[:24]}"
            )
            local[row["id"]] = scoped
            occurrences[row["id"]].append(scoped)
        maps[case_id] = local
    assets, audit = [], []
    for case_id, rows in sorted(cases):
        local = maps[case_id]
        for row in sorted(rows, key=lambda item: item["id"]):
            parsed = NodeAsset.model_validate(row)
            mapping = dict(local)
            for ref in parsed.references():
                if ref.split(".", 1)[0] in SEMANTIC and ref not in mapping:
                    choices = occurrences.get(ref, [])
                    if len(choices) != 1:
                        raise ValueError(
                            f"ambiguous/missing semantic reference {case_id}: {ref}"
                        )
                    mapping[ref] = choices[0]
            payload = parsed.model_dump(mode="json")
            payload["id"] = local[row["id"]]
            payload["properties"] = remap_property_node_ids(
                payload["properties"], mapping
            )
            asset = NodeAsset.model_validate(payload)
            assets.append(asset)
            audit.append(
                {
                    "case_id": case_id,
                    "original_id": row["id"],
                    "scoped_id": asset.id,
                    "payload_hash": fingerprint(row),
                    "normalized_name": normalized_name(asset.name),
                }
            )
    return assets, audit


def build_graph(
    physical: GraphState,
    cases: list[tuple[str, list[dict]]],
    arm: str,
) -> tuple[GraphState, dict]:
    if arm not in ARMS:
        raise ValueError(arm)
    if physical.conflict_nodes or any(
        n.node_type in SEMANTIC for n in physical.graph.nodes
    ):
        raise ValueError("physical snapshot contains semantic content or conflicts")
    assets, audit = scoped_assets(cases)
    audit_by_id = {row["scoped_id"]: row for row in audit}
    groups = defaultdict(list)
    for asset in assets:
        key = (asset.node_type.value, normalized_name(asset.name))
        groups[key if arm == "naive_name_merge" and key[1] else (asset.id, "")].append(
            asset
        )
    mappings, selected, representatives = {}, [], []
    for key, members in sorted(groups.items()):
        members.sort(
            key=lambda a: (
                audit_by_id[a.id]["case_id"],
                audit_by_id[a.id]["original_id"],
                audit_by_id[a.id]["payload_hash"],
            )
        )
        representative = members[0]
        canonical = (
            f"{representative.node_type.value}.naive.{fingerprint(key)[:24]}"
            if arm == "naive_name_merge" and key[1]
            else representative.id
        )
        mappings.update({a.id: canonical for a in members})
        selected.append((canonical, representative))
        representatives.append(
            {
                "output_id": canonical,
                "representative_id": representative.id,
                "members": [a.id for a in members],
            }
        )
    kept = []
    for canonical, representative in selected:
        payload = representative.model_dump(mode="json")
        payload["id"] = canonical
        payload["properties"] = remap_property_node_ids(payload["properties"], mappings)
        kept.append(NodeAsset.model_validate(payload))
    nodes = [build_node(asset) for asset in kept]
    # Case source is authoritative and retained only for the chosen representative.
    from cortex.schema import SourceReference

    for index, (_, representative) in enumerate(selected):
        case_id = audit_by_id[representative.id]["case_id"]
        nodes[index] = nodes[index].model_copy(
            update={
                "sources": tuple(
                    SourceReference(
                        source_id=s.source_id or case_id,
                        locator=s.source_type.value,
                        case_id=case_id,
                    )
                    for s in representative.sources
                )
            }
        )
    all_nodes = (*physical.graph.nodes, *nodes)
    state, _ = GraphManager(load_root_schema()).prepare_state(
        GraphState(
            graph=GraphSpace(nodes=all_nodes, edges=physical.graph.edges),
        ),
    )
    edges = state.graph.edges
    return state, {
        "arm": arm,
        "input_cases": len(cases),
        "input_semantic_nodes": len(assets),
        "output_semantic_nodes": len(kept),
        "output_nodes": len(all_nodes),
        "output_edges": len(edges),
        "conflicts": 0,
        "integration_llm_calls": 0,
        "integration_embedding_calls": 0,
        "input_occurrences": audit,
        "node_mappings": mappings,
        "representatives": representatives,
        "normalization": "NFKC -> casefold -> whitespace split/join; punctuation retained",
        "representative_policy": "min(case_id, original_id, payload_hash)",
        "sources_policy": "representative_only; all members in audit only",
    }
