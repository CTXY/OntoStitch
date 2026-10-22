from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cortex.bootstrap.graph_merge import build_graph_merge_operator
from cortex.bootstrap.knowledge import build_sql_training_evaluation_physical_graph
from cortex.capabilities.graph_merge.edges import remap_property_node_ids
from cortex.capabilities.sql_training.artifacts.cases import load_case_assets
from cortex.capabilities.sql_training.graph.adapter import NodeGraphAdapter
from cortex.capabilities.sql_training.graph.case import project_case_edges
from cortex.capabilities.sql_training.graph.join_edges import merge_join_edges
from cortex.capabilities.sql_training.graph_merge_evidence import (
    MappingEvidenceRepository,
    SqlTrainingGraphMergeEvidenceResolver,
)
from cortex.config import CortexConfig
from cortex.infrastructure.artifacts import (
    FileSystemGraphMergeTraceSink,
    render_graph_html,
)
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    load_root_schema,
    validate_graph_state,
)


def evaluate_merge_bundle(
    workspace: str | Path,
    *,
    output_dir: str | Path | None = None,
    config_path: str | Path | CortexConfig = "configs/cortex.yaml",
    case_ids: tuple[str, ...] | None = None,
    existing_state_path: str | Path | None = None,
    physical_state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run Graph Merge once over a bundle against the configured database catalog."""

    root = Path(workspace).resolve()
    cases = _load_cases(root, case_ids=case_ids)
    schema = load_root_schema()
    artifact_dir = Path(output_dir or root / "merge_live_review").resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    trace_path = artifact_dir / "llm_traces.jsonl"
    trace_path.unlink(missing_ok=True)
    if existing_state_path is not None and physical_state_path is not None:
        raise ValueError(
            "existing_state_path and physical_state_path are mutually exclusive"
        )
    baseline = (
        _load_existing_state(Path(existing_state_path).resolve())
        if existing_state_path is not None
        else None
    )
    reused_physical = (
        _physical_subgraph(
            _load_existing_state(Path(physical_state_path).resolve()).graph
        )
        if physical_state_path is not None
        else None
    )
    evidence_records = _load_evidence(
        root,
        # An incremental baseline can contain canonical nodes supported by cases outside the
        # focused replay. Their evidence remains necessary to reconcile those nodes losslessly.
        case_ids=None if baseline is not None else case_ids,
    )
    evidence = SqlTrainingGraphMergeEvidenceResolver(
        MappingEvidenceRepository(evidence_records)
    )
    operator = build_graph_merge_operator(
        config_path,
        evidence_resolver=evidence,
        trace_sink=FileSystemGraphMergeTraceSink(trace_path),
    )

    if reused_physical is not None:
        physical_graph = reused_physical
        physical_manifest = _reused_physical_manifest(physical_graph)
    elif baseline is None:
        physical_graph, physical_manifest = (
            build_sql_training_evaluation_physical_graph(
                config_path,
                projection_workspace=artifact_dir / "physical_projection",
                sql_records=list(evidence_records.values()),
            )
        )
    else:
        physical_graph = _physical_subgraph(baseline.graph)
        physical_manifest = _reused_physical_manifest(physical_graph)
    catalog_edges = _load_catalog_edges(root)
    physical_graph = _with_catalog_edges(physical_graph, catalog_edges)
    physical_manifest = {
        **physical_manifest,
        "bundle_catalog_edges": len(catalog_edges),
    }
    required_physical_graph = _with_referenced_dimension_values(physical_graph, cases)
    existing = (
        _augment_existing_physical_graph(
            baseline,
            required_physical_graph,
        )
        if baseline is not None
        else GraphState(graph=required_physical_graph, node_conflicts=())
    )
    incoming = _combined_incoming_state(cases, existing)
    input_nodes = len(incoming.graph.nodes)
    input_edges = len(incoming.graph.edges)
    input_source_ids = {case_id for case_id, _ in cases}
    result = operator.merge(existing, incoming, schema)
    state = result.state
    diagnostic_details = [item.model_dump(mode="json") for item in result.diagnostics]
    validate_graph_state(state, schema)
    physical_audit = _audit_final_physical_graph(state.graph, physical_manifest)
    output_source_ids = {
        source.case_id
        for node in state.graph.nodes
        for source in node.sources
        if source.case_id is not None
    }
    report = {
        "evaluation_mode": "live_llm",
        "acceptance_scope": "regression",
        "semantic_pipeline": {
            "identity": type(operator.identity_grouper.resolver).__name__,
            "reconcile": type(operator.node_reconciler.reconciler).__name__,
            "fact_fuse": type(
                operator.node_reconciler.fact_fuser_registry.default
            ).__name__,
            "generalize": type(operator.node_reconciler.generalizer).__name__,
            "coverage_verifier": type(
                operator.node_reconciler.generalization_verifier
            ).__name__,
            "branch": type(operator.node_reconciler.branch_selector).__name__,
            "concept_mine": type(operator.graph_miner).__name__,
        },
        "cases": len(cases),
        "incremental_existing_graph": existing_state_path is not None,
        "operator_invocations": 1,
        "physical_nodes": len(existing.graph.nodes),
        "physical_edges": len(existing.graph.edges),
        "physical_graph_manifest": physical_manifest,
        "physical_graph_audit": physical_audit,
        "input_nodes": input_nodes,
        "input_edges": input_edges,
        "canonical_nodes": len(state.graph.nodes),
        "canonical_edges": len(state.graph.edges),
        "conflicts": len(state.node_conflicts),
        "diagnostics": len(diagnostic_details),
        "diagnostic_details": diagnostic_details,
        "source_preservation": len(input_source_ids & output_source_ids)
        / len(input_source_ids),
        "invalid_final_state": 0,
        "state": state.model_dump(mode="json"),
    }
    _publish_artifacts(root, artifact_dir, trace_path, state, report, schema)
    return report


def _publish_artifacts(root, artifact_dir, trace_path, state, report, schema) -> None:
    state_path = artifact_dir / "graph_state.json"
    state_path.write_text(
        json.dumps(report["state"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (artifact_dir / "merge_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (artifact_dir / "physical_graph_manifest.json").write_text(
        json.dumps(report["physical_graph_manifest"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (artifact_dir / "graph.html").write_text(
        render_graph_html(
            state.graph,
            schema,
            title="Graph Merge · Live LLM",
            node_conflicts=state.node_conflicts,
        ),
        encoding="utf-8",
    )
    manifest = {
        "evaluation_mode": "live_llm",
        "acceptance_scope": "regression",
        "input_bundle_sha256": _tree_hash(root),
        "graph_state_sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
        "trace_sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
        "model": os.environ.get("CORTEX_LLM_MODEL"),
    }
    (artifact_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_cases(
    root: Path, *, case_ids: tuple[str, ...] | None = None
) -> tuple[tuple[str, GraphSpace], ...]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    selected = set(case_ids or ())
    cases = []
    for entry in manifest["cases"]:
        case_id = entry["source_id"]
        if selected and case_id not in selected:
            continue
        case_root = root / _case_path(entry)
        result = json.loads(
            (case_root / "result" / "result.json").read_text(encoding="utf-8")
        )
        if result["source_id"] != case_id:
            raise ValueError(f"case result source does not match collection: {case_id}")
        assets = load_case_assets(result, case_root=case_root)
        nodes = tuple(NodeGraphAdapter.project_asset_node(asset) for asset in assets)
        edges = project_case_edges(assets)
        cases.append(
            (
                case_id,
                GraphSpace(
                    nodes=tuple(_tag_case(node, case_id) for node in nodes),
                    edges=tuple(_tag_case(edge, case_id) for edge in edges),
                ),
            )
        )
    found = {case_id for case_id, _ in cases}
    if selected - found:
        raise ValueError(f"unknown bundle case IDs: {sorted(selected - found)}")
    if not cases:
        raise ValueError("merge bundle evaluation requires at least one case")
    return tuple(sorted(cases))


def _load_catalog_edges(root: Path) -> tuple[GraphEdge, ...]:
    path = root / "catalog_edges.json"
    if not path.exists():
        return ()
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise TypeError(f"collection catalog edges must be a list: {path}")
    return tuple(GraphEdge.model_validate(row) for row in rows)


def _with_catalog_edges(
    physical_graph: GraphSpace,
    catalog_edges: tuple[GraphEdge, ...],
) -> GraphSpace:
    if not catalog_edges:
        return physical_graph
    existing_join_edges = tuple(
        edge for edge in physical_graph.edges if edge.edge_type == "joins_with"
    )
    merged_join_edges = tuple(merge_join_edges(existing_join_edges, catalog_edges))
    passthrough_edges = tuple(
        edge for edge in physical_graph.edges if edge.edge_type != "joins_with"
    )
    return GraphSpace(
        nodes=physical_graph.nodes,
        edges=(*passthrough_edges, *merged_join_edges),
    )


def _load_evidence(
    root: Path, *, case_ids: tuple[str, ...] | None = None
) -> dict[str, dict[str, Any]]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    selected = set(case_ids or ())
    return {
        entry["source_id"]: json.loads(
            (root / _case_path(entry) / "case.json").read_text(encoding="utf-8")
        )["source"]
        for entry in manifest["cases"]
        if not selected or entry["source_id"] in selected
    }


def _case_path(entry: dict[str, Any]) -> str:
    case_path = entry.get("case_dir")
    if not isinstance(case_path, str) or not case_path.strip():
        raise ValueError(f"bundle case {entry.get('source_id')} is missing case_dir")
    return case_path


def _tag_case(item: GraphNode | GraphEdge, case_id: str):
    return item.model_copy(
        update={
            "sources": tuple(
                source.model_copy(update={"case_id": case_id})
                for source in item.sources
            )
        }
    )


def _with_referenced_dimension_values(
    physical_graph: GraphSpace,
    cases: tuple[tuple[str, GraphSpace], ...],
) -> GraphSpace:
    """Add bundle value literals without inventing tables or columns.

    Tables and columns must come from database introspection. A stale bundle reference therefore
    fails immediately instead of silently creating a placeholder physical schema.
    """

    physical_ids = {node.id for node in physical_graph.nodes}
    semantic_ids = {node.id for _, graph in cases for node in graph.nodes}
    references = {
        reference
        for _, graph in cases
        for node in graph.nodes
        for reference in _physical_references(node.properties)
    }
    references.update(
        endpoint
        for _, graph in cases
        for edge in graph.edges
        for endpoint in (edge.from_node_id, edge.to_node_id)
        if endpoint not in semantic_ids
    )
    required_schema = {
        item for item in references if item.startswith(("table.", "column."))
    }
    missing = sorted(required_schema - physical_ids)
    if missing:
        raise ValueError(
            f"bundle references columns/tables absent from database catalog: {missing}"
        )

    missing_values = sorted(
        item
        for item in references
        if item.startswith("dimension_value.") and item not in physical_ids
    )
    if missing_values:
        raise ValueError(
            "bundle references Dimension Values absent from verified database assets: "
            f"{missing_values}"
        )

    return physical_graph


def _load_existing_state(path: Path) -> GraphState:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "graph" not in payload:
        payload = {"graph": payload, "node_conflicts": []}
    return GraphState.model_validate(payload)


def _augment_existing_physical_graph(
    existing: GraphState,
    required_physical_graph: GraphSpace,
) -> GraphState:
    """Add newly required physical support without replaying existing semantic assets."""

    node_ids = {node.id for node in existing.graph.nodes}
    missing_nodes = tuple(
        node for node in required_physical_graph.nodes if node.id not in node_ids
    )
    nodes = (*existing.graph.nodes, *missing_nodes)
    complete_node_ids = {node.id for node in nodes}
    edge_ids = {edge.id for edge in existing.graph.edges}
    edges = (
        *existing.graph.edges,
        *(
            edge
            for edge in required_physical_graph.edges
            if edge.id not in edge_ids
            and edge.from_node_id in complete_node_ids
            and edge.to_node_id in complete_node_ids
        ),
    )
    return GraphState(
        graph=GraphSpace(nodes=nodes, edges=edges),
        node_conflicts=existing.node_conflicts,
    )


def _physical_subgraph(graph: GraphSpace) -> GraphSpace:
    physical_types = {"table", "column", "dimension_value"}
    nodes = tuple(node for node in graph.nodes if node.node_type in physical_types)
    node_ids = {node.id for node in nodes}
    edges = tuple(
        edge
        for edge in graph.edges
        if edge.from_node_id in node_ids and edge.to_node_id in node_ids
    )
    return GraphSpace(nodes=nodes, edges=edges)


def _reused_physical_manifest(graph: GraphSpace) -> dict[str, object]:
    counts = {
        node_type: sum(node.node_type == node_type for node in graph.nodes)
        for node_type in ("table", "column", "dimension_value")
    }
    return {
        "source": "incremental_existing_graph",
        "catalog": counts,
        "expected_value_node_ids": [],
        "dimension_value_columns": [],
        "projection_warnings": [],
    }


def _physical_references(value: Any) -> set[str]:
    if isinstance(value, str):
        return (
            {value}
            if value.startswith(("table.", "column.", "dimension_value."))
            else set()
        )
    if isinstance(value, dict):
        return {
            item for child in value.values() for item in _physical_references(child)
        }
    if isinstance(value, list | tuple):
        return {item for child in value for item in _physical_references(child)}
    return set()


def _audit_final_physical_graph(
    graph: GraphSpace, manifest: dict[str, object]
) -> dict[str, object]:
    node_ids = {node.id for node in graph.nodes}
    expected = {str(item) for item in manifest.get("expected_value_node_ids", [])}
    missing_expected = sorted(expected - node_ids)
    orphan_edges = sorted(
        edge.id
        for edge in graph.edges
        if edge.from_node_id not in node_ids or edge.to_node_id not in node_ids
    )
    value_nodes = {
        node.id for node in graph.nodes if node.node_type == "dimension_value"
    }
    owners: dict[str, list[str]] = {node_id: [] for node_id in value_nodes}
    for edge in graph.edges:
        if edge.edge_type == "value_of" and edge.from_node_id in owners:
            owners[edge.from_node_id].append(edge.to_node_id)
    invalid_value_owners = sorted(
        node_id
        for node_id, targets in owners.items()
        if len(targets) != 1 or targets[0] not in node_ids
    )
    high_enumerated = sorted(
        str(item.get("column_ref"))
        for item in manifest.get("dimension_value_columns", [])
        if isinstance(item, dict)
        and item.get("status") == "SKIPPED_HIGH_CARDINALITY"
        and (item.get("enumeration_attempted") or item.get("values_read"))
    )
    failures = {
        "missing_expected_value_nodes": missing_expected,
        "orphan_edges": orphan_edges,
        "invalid_value_owners": invalid_value_owners,
        "high_cardinality_enumerated": high_enumerated,
    }
    if any(failures.values()):
        raise ValueError(f"final physical graph audit failed: {failures}")
    return {
        "status": "PASS",
        "dimension_value_nodes": len(value_nodes),
        **failures,
    }


def _collision_safe_state(
    case_id: str, graph: GraphSpace, existing: GraphState
) -> GraphState:
    occupied_nodes = {node.id for node in existing.graph.nodes}
    node_mapping = {}
    for node in graph.nodes:
        node_mapping[node.id] = _available_incoming_id(
            f"{case_id}:{node.id}",
            prefix=f"incoming.{node.node_type}",
            occupied=occupied_nodes | set(node_mapping.values()),
        )
    nodes = tuple(
        node.model_copy(
            update={
                "id": node_mapping[node.id],
                "properties": remap_property_node_ids(node.properties, node_mapping),
            }
        )
        for node in graph.nodes
    )
    edges = []
    occupied_edges = {edge.id for edge in existing.graph.edges}
    for edge in graph.edges:
        edge_id = _available_incoming_id(
            f"{case_id}:{edge.id}",
            prefix="incoming.edge",
            occupied=occupied_edges | {item.id for item in edges},
        )
        edges.append(
            edge.model_copy(
                update={
                    "id": edge_id,
                    "from_node_id": node_mapping.get(
                        edge.from_node_id, edge.from_node_id
                    ),
                    "to_node_id": node_mapping.get(edge.to_node_id, edge.to_node_id),
                }
            )
        )
    return GraphState(
        graph=GraphSpace(nodes=nodes, edges=tuple(edges)), node_conflicts=()
    )


def _available_incoming_id(seed: str, *, prefix: str, occupied: set[str]) -> str:
    attempt = 0
    while True:
        replay_seed = seed if attempt == 0 else f"{seed}:replay:{attempt}"
        digest = hashlib.sha256(replay_seed.encode()).hexdigest()[:20]
        candidate = f"{prefix}.{digest}"
        if candidate not in occupied:
            return candidate
        attempt += 1


def _combined_incoming_state(
    cases: tuple[tuple[str, GraphSpace], ...], existing: GraphState
) -> GraphState:
    """Compose all case subgraphs before invoking GraphMergeOperator exactly once."""

    nodes = []
    edges = []
    occupied = existing
    for case_id, graph in cases:
        case_state = _collision_safe_state(case_id, graph, occupied)
        nodes.extend(case_state.graph.nodes)
        edges.extend(case_state.graph.edges)
        occupied = GraphState(
            graph=GraphSpace(
                nodes=(*occupied.graph.nodes, *case_state.graph.nodes),
                edges=(*occupied.graph.edges, *case_state.graph.edges),
            ),
            node_conflicts=(),
        )
    return GraphState(
        graph=GraphSpace(nodes=tuple(nodes), edges=tuple(edges)), node_conflicts=()
    )


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if "merge_live_review" in path.parts:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run live Graph Merge over an extraction bundle"
    )
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument(
        "--existing-state",
        type=Path,
        help="Existing merged GraphState JSON used as the incremental merge baseline",
    )
    parser.add_argument(
        "--physical-state",
        type=Path,
        help="Reuse only the physical catalog from a prior GraphState without ontology nodes",
    )
    parser.add_argument("--config", type=Path, default=Path("configs/cortex.yaml"))
    parser.add_argument(
        "--cases",
        help="Comma-separated case IDs for a focused single-invocation regression run",
    )
    args = parser.parse_args()
    case_ids = tuple(
        item.strip() for item in (args.cases or "").split(",") if item.strip()
    )
    report = evaluate_merge_bundle(
        args.workspace,
        output_dir=args.artifact_dir,
        config_path=args.config,
        case_ids=case_ids or None,
        existing_state_path=args.existing_state,
        physical_state_path=args.physical_state,
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "state"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
