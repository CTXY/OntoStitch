"""Run the prepared five-case merge with official SQLite checkpoints and native tracing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from cortex.application.ontology_graph import GraphManager
from cortex.bootstrap.graph_merge import build_graph_merge_operator
from cortex.capabilities.graph_merge.identity.legacy import IdentityGrouper
from cortex.capabilities.graph_merge.models import IdentityGroupingResult
from cortex.capabilities.sql_training.graph_merge_evidence import (
    MappingEvidenceRepository,
    SqlTrainingGraphMergeEvidenceResolver,
)
from cortex.config import load_config
from cortex.infrastructure.artifacts.graph_html import write_graph_html_from_json
from cortex.infrastructure.artifacts.graph_merge_trace import (
    FileSystemGraphMergeTraceSink,
)
from cortex.schema import GraphState, load_root_schema


def run(args):
    if args.env_file.exists():
        for line in args.env_file.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith(("#", ";")) or "=" not in value:
                continue
            key, value = value.removeprefix("export ").split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    root = args.input.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    marker = output / "run_id.txt"
    if marker.exists() and marker.read_text().strip() != args.run_id:
        raise ValueError("Output belongs to a different run ID")
    marker.write_text(args.run_id + "\n")
    existing = GraphState.model_validate_json(
        (root / "physical_state.json").read_text()
    )
    incoming = GraphState.model_validate_json(
        (root / "incoming_state.json").read_text()
    )
    rows = json.loads((root / "source_evidence.json").read_text())
    config = load_config(args.config)
    schema = load_root_schema(config.root_schema.external_path)
    resolver = SqlTrainingGraphMergeEvidenceResolver(
        MappingEvidenceRepository({row["source_id"]: row for row in rows})
    )
    with SqliteSaver.from_conn_string(str(output / "checkpoints.sqlite")) as saver:
        operator = build_graph_merge_operator(
            config,
            live_semantics=args.live,
            checkpointer=saver,
            evidence_resolver=resolver,
            trace_sink=FileSystemGraphMergeTraceSink(output / "agent_traces.jsonl"),
        )
        if args.reuse_matching_from:
            if getattr(operator, "manages_input_namespaces", False):
                raise ValueError("Legacy matching checkpoints cannot be reused by the explicit identity integration workflow")
            previous_root = args.reuse_matching_from.resolve()
            previous_run = (previous_root / "run_id.txt").read_text().strip()
            with SqliteSaver.from_conn_string(
                str(previous_root / "checkpoints.sqlite")
            ) as previous:
                saved = previous.get_tuple(
                    {"configurable": {"thread_id": previous_run, "checkpoint_ns": ""}}
                )
            values = saved.checkpoint["channel_values"]
            request = values["request"]
            if (
                request["existing"] != existing.model_dump(mode="json")
                or request["incoming"] != incoming.model_dump(mode="json")
                or request["schema"] != schema.model_dump(mode="json")
            ):
                raise ValueError(
                    "Reviewed matching may be reused only for exactly identical graph and schema inputs"
                )
            grouped = IdentityGroupingResult.model_validate(values["grouping"])

            class ReviewedIdentity(IdentityGrouper):
                def group(self, *arguments, **options):
                    return grouped.model_copy(
                        update={
                            "groups": tuple(
                                group
                                for group in grouped.groups
                                if group.node_type
                                not in {"table", "column", "dimension_value"}
                            )
                        }
                    )

            operator.identity_grouper = ReviewedIdentity(None)
            operator.workflow_version += (
                ":reviewed-matching:"
                + hashlib.sha256(grouped.model_dump_json().encode()).hexdigest()
            )
        result = operator.merge(existing, incoming, schema, run_id=args.run_id)
        GraphManager(schema).validate_state(result.state)
        (output / "graph_state.json").write_text(
            result.state.model_dump_json(indent=2) + "\n"
        )
        (output / "merge_report.json").write_text(
            result.model_dump_json(indent=2) + "\n"
        )
        saved = saver.get_tuple(
            {"configurable": {"thread_id": args.run_id, "checkpoint_ns": ""}}
        )
        values = saved.checkpoint["channel_values"]
        assembly = values.get("assembly", {})
        summary = {
            "live_semantics": args.live,
            "run_id": args.run_id,
            "reviewed_matching_from": str(args.reuse_matching_from)
            if args.reuse_matching_from
            else None,
            "nodes": len(result.state.graph.nodes),
            "edges": len(result.state.graph.edges),
            "semantic_nodes": [
                {
                    "id": n.id,
                    "type": n.node_type,
                    "name": n.name,
                    "merge_status": n.merge_status,
                    "source_ids": sorted({s.source_id for s in n.sources}),
                }
                for n in result.state.graph.nodes
                if n.node_type in {"concept", "metric", "generation_rule", "event"}
            ],
            "conflicts": [
                c.model_dump(mode="json") for c in result.state.conflict_nodes
            ],
            "transactions": assembly.get("completed", []),
            "identity": values.get("identity"),
            "assessment": values.get("review"),
            "rewrites": [r.model_dump(mode="json") for r in result.rewrites],
            "diagnostics": [d.model_dump(mode="json") for d in result.diagnostics],
            "limitations": [
                "Graph structure validation does not prove business correctness.",
                "No database SQL replay is performed by this script.",
            ],
        }
        (output / "review_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        )
    write_graph_html_from_json(
        output / "graph_state.json",
        output / "graph.html",
        schema,
        title="Reviewed five-case ontology merge",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "live": args.live,
                "nodes": summary["nodes"],
                "semantic_nodes": len(summary["semantic_nodes"]),
                "conflicts": len(summary["conflicts"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("artifacts/merge_review/thrombosis_prediction"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/cortex.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--reuse-matching-from", type=Path)
    run(parser.parse_args())
