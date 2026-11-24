"""Bounded region experiment using the production lightweight merge agents."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from summarize_graph_merge_calls import summarize

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.batch import alias_view
from cortex.capabilities.graph_merge.graph.publication import Preview
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.capabilities.graph_merge.runtime.contracts import implementation_sources
from cortex.capabilities.graph_merge.engine import (
    IncrementalGraphIntegrationEngine,
)
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.graph.edit_contract import IntegrationProposal
from cortex.capabilities.graph_merge.graph.provenance import (
    validate_rewrite_sources,
)
from cortex.capabilities.graph_merge.reconciliation.context import (
    relation_context,
)
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
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
from cortex.schema.relation_vocabulary import require_concrete_relation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-noop", action="store_true")
    parser.add_argument("--max-calls", type=int, default=4)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Refuse to overwrite/repeat a paid run")
    load_dotenv(".env")
    os.environ["LANGSMITH_TRACING"] = os.environ["LANGCHAIN_TRACING_V2"] = "false"
    cfg = load_config(args.config)
    model = create_chat_model(cfg.llm.model_copy(update={"max_retries": 0}), profile="graph_merge")
    host = urlparse(str(model.openai_api_base)).hostname
    if host not in {"api.deepseek.com", "ciyuanzhiyan.com"}:
        raise ValueError("Endpoint outside existing authorization")
    model.streaming, model.stream_usage = True, True
    out = args.output
    out.mkdir(parents=True)
    dump = lambda name, value: (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))
    original = GraphState.model_validate_json(args.state.read_text())
    schema = load_root_schema()
    ctx = IntegrationContext(original, GraphState(graph=GraphSpace()), schema)
    resolver = SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(json.loads(args.evidence.read_text())))
    refs = tuple(dict.fromkeys(
        [s for n in original.graph.nodes for s in (*n.sources, *(s for a in n.alternatives for s in a.sources))]
        + [s for e in original.graph.edges for s in e.sources]))
    ctx.evidence = resolver.resolve(refs)
    agent = IncrementalIntegrationAgent(model, max_calls=args.max_calls,
            trace_sink=FileSystemGraphMergeTraceSink(out / "agent_traces.jsonl"))
    agent.call_budget = {"used": 0, "limit": 10}
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=1)
    base, edge_map = alias_view(original, {})
    engine.session.inputs, engine.session.current = ctx, base
    engine.session.lineage = LineageIndex(ctx, initial_edge_map=edge_map)
    engine.session.revision, engine.session.reviewed_sources = 0, {}
    targets = set(json.loads(args.targets.read_text()))
    dump("manifest.json", {"host": host, "model": model.model_name,
        "implementation_version": canonical_hash(implementation_sources()), "max_model_calls": 10,
        "state": str(args.state), "evidence": str(args.evidence),
        "targets": sorted(targets), "streaming": True, "sdk_retries": 0,
        "full_merge": False, "targeted_feedback": False})
    dump("base_state.json", base.model_dump(mode="json"))
    result = {"status": "running"}
    try:
        engine._run_task(LocalTask("local-region", "reconcile", targets), "local-region")
        accepted = engine.session.current
        GraphManager(schema).validate_state(accepted)
        ctx.candidate = accepted
        view = relation_context(ctx, accepted, LocalTask("view", "reconcile", targets))
        for e in view["edges"]:
            require_concrete_relation(e["edge_type"])
        preview = Preview(accepted, engine.session.lineage.node_map, engine.session.lineage.edge_map, [])
        validate_rewrite_sources(ctx, IntegrationProposal(changes=engine.session.lineage.source_changes(accepted)), preview)
        conflicts_before = [n.model_dump(mode="json") for n in base.graph.nodes if n.alternatives]
        conflicts_after = [n.model_dump(mode="json") for n in accepted.graph.nodes if n.alternatives]
        if conflicts_before != conflicts_after:
            raise ValueError("Conflict preservation control changed; inspect before full merge")
        restored = GraphState.model_validate_json(accepted.model_dump_json())
        assert restored == accepted
        dump("graph_state.json", accepted.model_dump(mode="json"))
        dump("lineage.json", engine.session.lineage.snapshot())
        dump("history.json", engine.session.history)
        dump("relation_view.json", view)
        chosen = {e["id"] for e in view["edges"]}
        ids = {n["id"] for n in view["nodes"]}
        graph = GraphSpace(nodes=tuple(n for n in accepted.graph.nodes if n.id in ids),
                           edges=tuple(e for e in accepted.graph.edges if e.id in chosen),
                           relation_contracts=accepted.graph.relation_contracts)
        (out / "graph.html").write_text(render_graph_html(graph, schema, title="Production lightweight relationship review"))
        result.update(status="local_accepted", conflicts_preserved=True,
                      exact_source_coverage=True, graph_roundtrip=True)
        if args.check_noop:
            before_count = agent.call_budget["used"]
            engine._run_task(LocalTask("noop-control", "reconcile", targets), "local-region")
            result["noop_control"] = {
                "unchanged": engine.session.current == accepted,
                "calls": agent.call_budget["used"] - before_count,
                "assessment": engine.session.history[-1]["assessment"],
            }
            if engine.session.current != accepted or engine.session.history[-1]["assessment"] != "skipped_no_change":
                raise ValueError("No-op control did not remain unchanged; stop before full merge")
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        dump("outcome.json", result)
        summarize(out)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
