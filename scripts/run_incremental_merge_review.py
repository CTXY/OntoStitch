"""Real-model incremental merge experiment; no domain decisions in the runner."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from audit_incremental_merge import audit_result
from dotenv import load_dotenv
from summarize_graph_merge_calls import summarize

from cortex.capabilities.graph_merge.integration.engine import implementation_version
from cortex.capabilities.graph_merge.integration.incremental import (
    IncrementalGraphIntegrationEngine,
)
from cortex.capabilities.graph_merge.integration.recovery import (
    load_reviewed_node_stage,
)
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
from cortex.capabilities.sql_training.graph_merge_evidence import (
    MappingEvidenceRepository,
    SqlTrainingGraphMergeEvidenceResolver,
)
from cortex.config import load_config
from cortex.infrastructure.artifacts.graph_html import render_graph_html
from cortex.infrastructure.artifacts.graph_merge_trace import (
    FileSystemGraphMergeTraceSink,
)
from cortex.infrastructure.llm import create_chat_model
from cortex.schema import GraphSpace, GraphState, load_root_schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--reviewed-node-stage", type=Path)
    parser.add_argument("--feedback-file", type=Path)
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Resume a fingerprinted accepted workspace; failed pending patches are not accepted",
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Reverse prepared blocks without renaming IDs or changing contracts",
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Run previous published state + one next source graph per merge",
    )
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--max-calls", type=int, default=10)
    parser.add_argument("--model-call-budget", type=int, default=80)
    args = parser.parse_args()
    load_dotenv(args.env_file, override=False)
    import os

    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    cfg = load_config(args.config)
    model = create_chat_model(cfg.llm, profile="graph_merge")
    if urlparse(str(model.openai_api_base)).hostname != "api.deepseek.com":
        raise ValueError(
            "This authorized experiment requires the official api.deepseek.com endpoint"
        )
    schema = load_root_schema(cfg.root_schema.external_path)
    incoming = GraphState.model_validate_json(
        (args.prepared_input / "incoming_state.json").read_text()
    )
    rows = json.loads((args.prepared_input / "source_evidence.json").read_text())
    empty = GraphState(graph=GraphSpace())
    blocks = list(dict.fromkeys(n.id.split("::")[0] for n in incoming.graph.nodes))
    if args.reverse:
        blocks.reverse()
        nodes = tuple(
            n for b in blocks for n in incoming.graph.nodes if n.id.split("::")[0] == b
        )
        edges = tuple(
            e for b in blocks for e in incoming.graph.edges if e.id.split("::")[0] == b
        )
        assert sorted(n.model_dump_json() for n in nodes) == sorted(
            n.model_dump_json() for n in incoming.graph.nodes
        )
        incoming = incoming.model_copy(
            update={
                "graph": incoming.graph.model_copy(
                    update={"nodes": nodes, "edges": edges}
                )
            }
        )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "incoming_state.json").write_text(incoming.model_dump_json(indent=2))
    (args.output / "source_evidence.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2)
    )
    (args.output / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": args.output.name,
                "prepared_from": str(args.prepared_input.resolve()),
                "order": blocks,
                "sequential": args.sequential,
                "model_call_budget": args.model_call_budget,
                "resumed_from": str(args.resume_from.resolve())
                if args.resume_from
                else None,
                "model": model.model_name,
                "implementation_version": implementation_version(),
                "host": "api.deepseek.com",
            },
            indent=2,
        )
    )
    resolver = SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(rows))
    current = empty
    inputs = []
    if args.sequential:
        for b in blocks:
            ns = tuple(n for n in incoming.graph.nodes if n.id.split("::")[0] == b)
            ids = {n.id for n in ns}
            es = tuple(
                e
                for e in incoming.graph.edges
                if e.from_node_id in ids and e.to_node_id in ids
            )
            inputs.append(
                GraphState(
                    graph=GraphSpace(
                        nodes=ns,
                        edges=es,
                        relation_contracts=incoming.graph.relation_contracts,
                    )
                )
            )
    else:
        inputs = [incoming]
    try:
        for index, fragment in enumerate(inputs):
            out = args.output / f"step{index}" if args.sequential else args.output
            out.mkdir(parents=True, exist_ok=True)
            agent = IncrementalIntegrationAgent(
                model,
                max_calls=args.max_calls,
                trace_sink=FileSystemGraphMergeTraceSink(out / "agent_traces.jsonl"),
            )
            engine = IncrementalGraphIntegrationEngine(
                agent,
                evidence_resolver=resolver,
                max_rounds=args.max_rounds,
                sql_dialect="sqlite",
                model_call_budget=args.model_call_budget,
            )
            engine.workspace_path = out / "workspace.sqlite"
            if args.resume_from:
                from cortex.capabilities.graph_merge.integration.workspace import (
                    WorkspaceStore,
                )

                saved_manifest = json.loads(
                    (args.resume_from / "manifest.json").read_text()
                )
                engine.resume_snapshot = WorkspaceStore(
                    args.resume_from / "workspace.sqlite"
                ).load(saved_manifest["run_id"] + "-0")
                if not engine.resume_snapshot:
                    raise ValueError("No accepted workspace to resume")
                (out / "resume_manifest.json").write_text(
                    json.dumps(
                        {
                            "resumed_from": str(args.resume_from.resolve()),
                            "previous_usage": json.loads(
                                (args.resume_from / "usage_report.json").read_text()
                            ),
                            "not_new_model_calls": True,
                        },
                        indent=2,
                    )
                )
            if args.reviewed_node_stage:
                engine.reviewed_node_seed = load_reviewed_node_stage(
                    args.reviewed_node_stage, empty, fragment, schema
                )
            if args.feedback_file:
                engine.external_feedback = json.loads(args.feedback_file.read_text())[
                    "issues"
                ]
            result = engine.merge(
                current, fragment, schema, run_id=f"{args.output.name}-{index}"
            )
            audit = audit_result(current, fragment, result, schema)
            (out / "audit.json").write_text(
                json.dumps(audit, ensure_ascii=False, indent=2)
            )
            (out / "merge_report.json").write_text(result.model_dump_json(indent=2))
            (out / "graph_state.json").write_text(
                result.state.model_dump_json(indent=2)
            )
            (out / "workflow.json").write_text(
                json.dumps(engine.last_session, ensure_ascii=False, indent=2)
            )
            (out / "graph.html").write_text(
                render_graph_html(
                    result.state.graph,
                    schema,
                    title=f"Incremental ontology merge: {args.output.name}",
                )
            )
            summarize(out)
            current = result.state
            print(
                json.dumps(
                    {
                        "step": index,
                        "audit": audit,
                        "nodes": len(current.graph.nodes),
                        "edges": len(current.graph.edges),
                    }
                ),
                flush=True,
            )
            if audit["status"] != "passed":
                raise ValueError("Independent public-result preservation audit failed")
        if args.sequential:
            (args.output / "graph_state.json").write_text(
                current.model_dump_json(indent=2)
            )
            (args.output / "graph.html").write_text(
                render_graph_html(
                    current.graph, schema, title=f"Sequential merge: {args.output.name}"
                )
            )
    finally:
        for out in (
            [args.output] if not args.sequential else sorted(args.output.glob("step*"))
        ):
            if (out / "model_calls.jsonl").exists():
                summarize(out)


if __name__ == "__main__":
    main()
