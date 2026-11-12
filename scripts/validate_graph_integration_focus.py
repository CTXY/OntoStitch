"""Real controlled validation. Seeds are never counted as model executions.

Select the implementation with PYTHONPATH (snapshot or current src). The same
saved workflow/evidence seeds both arms; no graph edits are applied by this driver.
"""

import argparse
import json
from contextlib import nullcontext
from pathlib import Path

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver

from cortex.capabilities.graph_merge.integration.assessment import assessment_view
from cortex.capabilities.graph_merge.integration.compiler import compile_proposal
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.engine import GraphIntegrationEngine
from cortex.capabilities.graph_merge.integration.models import (
    IdentityDecision,
    IntegrationProposal,
    IntegrationReview,
)
from cortex.capabilities.graph_merge.llm.integration import IntegrationAgent
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.capabilities.sql_training.graph_merge_evidence import (
    MappingEvidenceRepository,
    SqlTrainingGraphMergeEvidenceResolver,
)
from cortex.config import config_fingerprint, load_config
from cortex.infrastructure.artifacts.graph_html import render_graph_html
from cortex.infrastructure.artifacts.graph_merge_trace import (
    FileSystemGraphMergeTraceSink,
)
from cortex.infrastructure.llm import create_chat_model
from cortex.schema import GraphState, SchemaDefinition


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=["revision", "assess"], required=True)
    parser.add_argument("--feedback-run", type=Path)
    parser.add_argument(
        "--issue-refs",
        type=Path,
        help="Optional JSON list annotating the fixed feedback with object IDs",
    )
    args = parser.parse_args()
    if args.mode == "revision" and args.feedback_run is None:
        parser.error("revision requires --feedback-run")
    root = Path.cwd()
    args.out.mkdir(parents=True, exist_ok=False)
    w = json.loads((args.source / "workflow.json").read_text())
    req = w["request"]
    load_dotenv(root / ".env", override=False)
    config = load_config(root / "configs/cortex.yaml")
    sink = FileSystemGraphMergeTraceSink(args.out / "agent_traces.jsonl")
    live = IntegrationAgent(
        create_chat_model(config.llm, profile="graph_merge"),
        max_calls=config.services.graph_merge.consolidation_max_calls,
        trace_sink=sink,
    )
    schema = SchemaDefinition.model_validate(req["schema"])
    old = GraphState.model_validate(req["existing"])
    incoming = GraphState.model_validate(req["incoming"])
    decision = IdentityDecision.model_validate(w["identity"])
    proposal = IntegrationProposal.model_validate(w["proposal"])
    for name in ("incoming_state.json", "source_evidence.json"):
        (args.out / name).write_text((args.source / name).read_text())
    rows = json.loads((args.source / "source_evidence.json").read_text())
    manifest = {
        "mode": args.mode,
        "source": str(args.source),
        "implementation": __import__("cortex").__file__,
        "config_fingerprint": config_fingerprint(config),
    }
    if args.mode == "assess":
        context = IntegrationContext(
            old,
            incoming,
            schema,
            evidence=tuple(EvidenceItem.model_validate(e) for e in w["evidence"]),
        )
        context.read_ids.update(w.get("read_ids", []))
        payload = context.payload("assess")
        preview = compile_proposal(context, decision, proposal)
        context.candidate = preview.state
        payload.update(
            identity=w["identity"],
            proposal={"changes": w["proposal"]["changes"]},
            identity_map={
                k: v
                for k, v in preview.node_map.items()
                if k in context.nodes
                and context.nodes[k].node_type
                not in {"table", "column", "dimension_value"}
            },
            impacts=preview.impacts,
            **assessment_view(context, decision, preview),
        )
        # Blind assessment: no prior review, defect hint, or expected verdict.
        (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
        review = live.invoke("assess", context, payload)
        (args.out / "review.json").write_text(review.model_dump_json(indent=2))
        print(review.model_dump_json())
        return
    feedback = json.loads((args.feedback_run / "manifest.json").read_text())[
        "external_review_claim"
    ]
    # Stable object IDs annotate the same feedback, not additional semantic advice.
    refs = json.loads(args.issue_refs.read_text()) if args.issue_refs else []
    manifest.update(
        feedback=feedback,
        issue_refs=refs,
        seeded_phases=["identity", "initial proposal", "reviewer revision request"],
        live_phases=["revision proposal", "independent assessment"],
    )
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    class SeedThenLive:
        index = 0

        def invoke(self, phase, context, payload):
            if self.index >= 3:
                return live.invoke(phase, context, payload)
            assert phase == ("identity", "propose", "assess")[self.index]
            if phase == "identity":
                result = decision
            elif phase == "propose":
                result = proposal
            else:
                fields = {
                    "verdict": "revise",
                    "decision_check": "Use the completed identity decision.",
                    "quality_check": feedback,
                    "preservation_check": "Preserve the validated meanings and provenance.",
                    "issues": (feedback,),
                }
                if "issue_refs" in IntegrationReview.model_fields:
                    fields["issue_refs"] = refs
                result = IntegrationReview(**fields)
            sink.record(
                {
                    "component": "integration_" + phase,
                    "execution_origin": "reviewer_seed_not_model",
                    "model_calls": 0,
                    "payload": payload,
                    "parsed_response": result.model_dump(mode="json"),
                    "messages": [],
                    "tools": [],
                }
            )
            self.index += 1
            return result

    resolver = SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(rows))
    with SqliteSaver.from_conn_string(str(args.out / "checkpoints.sqlite")) as saver:
        engine = GraphIntegrationEngine(
            SeedThenLive(),
            evidence_resolver=resolver,
            checkpoint_factory=lambda: nullcontext(saver),
            max_rounds=3,
            top_k=config.services.graph_merge.candidate_top_k,
            runtime_signature=config_fingerprint(config),
        )
        try:
            result = engine.merge(old, incoming, schema, run_id=args.out.name)
        finally:
            snapshot = saver.get_tuple({"configurable": {"thread_id": args.out.name}})
            if snapshot:
                (args.out / "workflow.json").write_text(
                    json.dumps(
                        snapshot.checkpoint["channel_values"],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
    (args.out / "merge_report.json").write_text(result.model_dump_json(indent=2))
    (args.out / "graph_state.json").write_text(result.state.model_dump_json(indent=2))
    (args.out / "graph.html").write_text(
        render_graph_html(
            result.state.graph,
            schema,
            title="Controlled live revision: " + args.out.name,
        )
    )
    print(
        json.dumps(
            {
                "out": str(args.out),
                "nodes": len(result.state.graph.nodes),
                "edges": len(result.state.graph.edges),
            }
        )
    )


if __name__ == "__main__":
    main()
