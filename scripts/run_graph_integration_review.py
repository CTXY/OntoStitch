"""Merge 3–5 saved extraction workspaces using the production integration path.

The runner only namespaces inputs and collects source evidence. It contains no
business-specific decisions and never modifies a generated merge result.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from cortex.bootstrap.graph_merge import build_graph_merge_operator
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.graph.operations import remap_edge, remap_node
from cortex.capabilities.sql_training.graph_merge_evidence import (
    MappingEvidenceRepository,
    SqlTrainingGraphMergeEvidenceResolver,
)
from cortex.config import load_config
from cortex.infrastructure.artifacts.graph_html import render_graph_html
from cortex.infrastructure.artifacts.graph_merge_trace import (
    FileSystemGraphMergeTraceSink,
)
from cortex.schema import GraphSpace, GraphState, load_root_schema


def prepare(workspaces):
    nodes, edges, contracts, rows = [], [], {}, {}
    for index, root in enumerate(workspaces):
        state = GraphState.model_validate_json(
            (root / "incoming_state.json").read_text()
        )
        node_map = {n.id: f"source{index}::{n.id}" for n in state.graph.nodes}
        edge_map = {e.id: f"source{index}::{e.id}" for e in state.graph.edges}
        predicates = {}
        for c in state.graph.relation_contracts:
            key = c.id
            if key in contracts and c != contracts[key]:
                key = f"source{index}__{key}"
            predicates[c.id] = key
            contracts[key] = c.model_copy(update={"id": key})
        nodes.extend(remap_node(n, node_map, edge_map) for n in state.graph.nodes)
        edges.extend(
            remap_edge(e, node_map, edge_map).model_copy(
                update={"edge_type": predicates.get(e.edge_type, e.edge_type)}
            )
            for e in state.graph.edges
        )
        for line in (
            (root / "inputs/sql_training.qa_sql.jsonl").read_text().splitlines()
        ):
            row = json.loads(line)
            rows[row["source_id"]] = row
    return GraphState(
        graph=GraphSpace(
            nodes=tuple(nodes),
            edges=tuple(edges),
            relation_contracts=tuple(contracts.values()),
        )
    ), rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--case-workspace", type=Path, action="append")
    inputs.add_argument(
        "--prepared-input",
        type=Path,
        help="Use a saved immutable input/evidence/manifest bundle",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/cortex.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--run-id", default="integration-review")
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare and audit the exact inputs without any model call",
    )
    args = parser.parse_args()
    if args.case_workspace and not 3 <= len(args.case_workspace) <= 5:
        parser.error("Select 3–5 real case workspaces")
    load_dotenv(args.env_file, override=False)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.prepared_input:
        incoming = GraphState.model_validate_json(
            (args.prepared_input / "incoming_state.json").read_text()
        )
        rows = json.loads((args.prepared_input / "source_evidence.json").read_text())
        input_manifest = json.loads((args.prepared_input / "manifest.json").read_text())
        if not 3 <= len(rows) <= 5:
            parser.error("Prepared input must contain 3–5 source cases")
    else:
        incoming, rows = prepare(args.case_workspace)
        input_manifest = {"workspaces": [str(p.resolve()) for p in args.case_workspace]}
    config = load_config(args.config)
    config = config.model_copy(
        update={
            "services": config.services.model_copy(
                update={
                    "graph_merge": config.services.graph_merge.model_copy(
                        update={"consolidation_max_rounds": args.max_rounds}
                    )
                }
            )
        }
    )
    schema = load_root_schema(config.root_schema.external_path)
    (args.output / "incoming_state.json").write_text(incoming.model_dump_json(indent=2))
    (args.output / "source_evidence.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2)
    )
    (args.output / "manifest.json").write_text(
        json.dumps(
            {
                **input_manifest,
                "prepared_from": str(args.prepared_input.resolve())
                if args.prepared_input
                else None,
                "source_ids": list(rows),
                "run_id": args.run_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    resolver = SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(rows))
    if args.prepare_only:
        sources = tuple(
            dict.fromkeys(s for n in incoming.graph.nodes for s in n.sources)
        )
        context = IntegrationContext(
            GraphState(graph=GraphSpace()),
            incoming,
            schema,
            evidence=resolver.resolve(sources),
        )
        payload = context.payload("identity")
        (args.output / "prepared_identity_context.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )
        audit = {
            "status": "prepared_not_model_evaluated",
            "model_calls": 0,
            "source_cases": len(rows),
            "input_nodes": len(incoming.graph.nodes),
            "input_edges": len(incoming.graph.edges),
            "semantic_nodes": len(context.seeds),
            "binding_contests": len(context.mandatory),
            "payload_characters": len(json.dumps(payload, ensure_ascii=False)),
            "component_characters": {
                k: len(json.dumps(v, ensure_ascii=False)) for k, v in payload.items()
            },
            "operation_registry_in_initial_payload": False,
        }
        (args.output / "context_audit.json").write_text(json.dumps(audit, indent=2))
        print(json.dumps(audit))
        return
    engine = build_graph_merge_operator(
        config,
        evidence_resolver=resolver,
        workspace_path=args.output / "workspace.sqlite",
        trace_sink=FileSystemGraphMergeTraceSink(
            args.output / "agent_traces.jsonl"
        ),
    )
    result = engine.merge(
        GraphState(graph=GraphSpace()), incoming, schema, run_id=args.run_id
    )
    from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
    snapshot = WorkspaceStore(args.output / "workspace.sqlite").load(args.run_id)
    (args.output / "workflow.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    (args.output / "merge_report.json").write_text(result.model_dump_json(indent=2))
    (args.output / "graph_state.json").write_text(
        result.state.model_dump_json(indent=2)
    )
    (args.output / "graph.html").write_text(
        render_graph_html(
            result.state.graph, schema, title="Reviewed ontology integration"
        )
    )
    print(
        json.dumps(
            {
                "nodes": len(result.state.graph.nodes),
                "edges": len(result.state.graph.edges),
                "rewrites": len(result.rewrites),
                "output": str(args.output.resolve()),
            }
        )
    )


if __name__ == "__main__":
    main()
