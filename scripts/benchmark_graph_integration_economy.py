"""Offline paired replay of a saved merge: identical decisions, smaller representation.

No provider calls. Token counts use a local tokenizer proxy, not provider billing.
Graph equality is a preservation control, not a new LLM semantic-quality evaluation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cortex.application.merge_graph import GraphMergeResult
from cortex.capabilities.graph_merge.graph.publication import (
    commit_result,
    compile_proposal,
)
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.integration.engine import GraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.edit_contract import (
    IdentityDecision,
    IntegrationProposal,
    IntegrationReview,
)
from cortex.capabilities.graph_merge.llm.packing import (
    expand_payload,
    pack_payload,
    prompt_json,
)
from cortex.capabilities.graph_merge.integration.revision import (
    compose_revision,
    graph_edits,
)
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import GraphState, SchemaDefinition


def compare_run(root, encoding):
    workflow = json.loads((root / "workflow.json").read_text())
    request = workflow["request"]
    schema = SchemaDefinition.model_validate(request["schema"])
    old = GraphState.model_validate(request["existing"])
    incoming = GraphState.model_validate(request["incoming"])
    ctx = IntegrationContext(old, incoming, schema)
    ctx.read_ids.update(ctx.nodes)

    def measure(obj, *, compact=False):
        text = prompt_json(obj) if compact else json.dumps(obj, ensure_ascii=False)
        return {"characters": len(text), "token_proxy": len(encoding.encode(text))}

    def pair(before, after, *, compact=False):
        a, b = measure(before), measure(after, compact=compact)
        return {
            "before": a,
            "after": b,
            "token_proxy_reduction_percent": round(
                100 * (1 - b["token_proxy"] / a["token_proxy"]), 2
            ),
        }

    def content(state):
        return (
            {n.id: n for n in state.graph.nodes},
            {e.id: e for e in state.graph.edges},
            {c.id: c for c in state.graph.relation_contracts},
        )

    decision = previous = previous_preview = None
    packing, revisions, responses = [], [], []
    traces = [
        json.loads(line)
        for line in (root / "agent_traces.jsonl").read_text().splitlines()
    ]
    if any(t["payload"].get("proposal_mode") == "delta" for t in traces):
        raise ValueError(
            "Use a baseline run with full source-relative proposals, not incremental proposals"
        )
    for trace in traces:
        parsed = trace.get("parsed_response")
        if trace["component"] == "integration_identity" and parsed:
            decision = IdentityDecision.model_validate(parsed)
            previous = previous_preview = None
            responses.append(decision)
        elif trace["component"] == "integration_propose" and parsed:
            full = IntegrationProposal.model_validate(parsed)
            target = compile_proposal(ctx, decision, full)
            if previous is not None:
                # Derive only syntactic edits; reuse the recorded semantic decisions.
                edits = graph_edits(
                    {n.id: n for n in previous_preview.state.graph.nodes},
                    {e.id: e for e in previous_preview.state.graph.edges},
                    {n.id: n for n in target.state.graph.nodes},
                    {e.id: e for e in target.state.graph.edges},
                )
                old_changes = set(previous.changes)
                changes = tuple(c for c in full.changes if c not in old_changes)
                # Include complete replacements if a joint correspondence is touched.
                touched = {e for c in changes for e in c.before}
                for prior in previous.changes:
                    if set(prior.before) & touched:
                        changes = tuple(
                            dict.fromkeys(
                                (
                                    *changes,
                                    *(
                                        c
                                        for c in full.changes
                                        if set(c.before) & set(prior.before)
                                    ),
                                )
                            )
                        )
                old_contracts = {c.id: c for c in previous.relation_contracts}
                patch = IntegrationProposal(
                    edits=edits,
                    changes=changes,
                    relation_contracts=tuple(
                        c
                        for c in full.relation_contracts
                        if old_contracts.get(c.id) != c
                    ),
                )
                cumulative = compose_revision(
                    ctx, decision, previous, previous_preview, patch
                )
                rebuilt = compile_proposal(ctx, decision, cumulative)
                assert content(rebuilt.state) == content(target.state)
                assert rebuilt.node_map == target.node_map
                assert rebuilt.edge_map == target.edge_map
                assert set(cumulative.changes) == set(full.changes)
                baseline_result = commit_result(ctx, full, target, run_id="paired")
                revised_result = commit_result(
                    ctx, cumulative, rebuilt, run_id="paired"
                )
                assert content(baseline_result.state) == content(revised_result.state)
                for field in (
                    "node_mappings",
                    "edge_mappings",
                    "existing_node_mappings",
                    "existing_edge_mappings",
                ):
                    assert getattr(baseline_result, field) == getattr(
                        revised_result, field
                    )
                assert set(baseline_result.rewrites) == set(revised_result.rewrites)
                revisions.append(
                    {
                        **pair(
                            full.model_dump(mode="json"), patch.model_dump(mode="json")
                        ),
                        "full_edit_count": len(full.edits),
                        "delta_edit_count": len(patch.edits),
                        "graph_and_correspondence_equal": True,
                    }
                )
                responses.append(patch)
            else:
                responses.append(full)
            previous, previous_preview = full, target
        elif trace["component"] == "integration_assess":
            payload = trace["payload"]
            packed = pack_payload(payload)
            assert expand_payload(packed) == payload
            packing.append({**pair(payload, packed, compact=True), "lossless": True})
            responses.append(IntegrationReview.model_validate(parsed))

    # Exercise the actual controller with the same semantic decisions, including
    # failed-review transitions, preloading and the new candidate-relative tools.
    class Replay:
        def __init__(self):
            self.index, self.payloads = 0, []

        def invoke(self, phase, context, payload):
            assert traces[self.index]["component"] == "integration_" + phase
            self.payloads.append(
                {
                    "phase": phase,
                    **pair(
                        traces[self.index]["payload"],
                        pack_payload(payload),
                        compact=True,
                    ),
                }
            )
            response = responses[self.index]
            self.index += 1
            return response

    class Evidence:
        def resolve(self, sources):
            return tuple(
                EvidenceItem.model_validate(row) for row in workflow.get("evidence", [])
            )

    replay = Replay()
    result = GraphIntegrationEngine(
        replay, evidence_resolver=Evidence(), max_rounds=len(revisions) + 1
    ).merge(old, incoming, schema, run_id="offline-paired")
    recorded = GraphState.model_validate_json((root / "graph_state.json").read_text())
    assert content(result.state) == content(recorded)
    recorded_result = GraphMergeResult.model_validate_json(
        (root / "merge_report.json").read_text()
    )
    for field in (
        "node_mappings",
        "edge_mappings",
        "existing_node_mappings",
        "existing_edge_mappings",
        "rewrites",
    ):
        assert set(getattr(result, field)) == set(getattr(recorded_result, field))
    assert replay.index == len(responses)
    before_total = sum(p["before"]["token_proxy"] for p in replay.payloads)
    after_total = sum(p["after"]["token_proxy"] for p in replay.payloads)
    return {
        "baseline": str(root),
        "mode": "offline_fixed_decision_replay",
        "tokenizer_proxy": encoding.name,
        "provider_calls": 0,
        "assessment_packing": packing,
        "revision_outputs": revisions,
        "controller_replay": {
            "final_graph_equal": True,
            "final_correspondence_equal": True,
            "initial_payload_tokens": {
                "before": before_total,
                "after": after_total,
                "reduction_percent": round(100 * (1 - after_total / before_total), 2),
            },
            "phase_payloads": replay.payloads,
        },
        "limits": [
            "Token proxy is not DeepSeek billing.",
            "Initial payload totals exclude system prompts, tool schemas, tool results and repeated model turns.",
            "Fixed decisions prove representation preservation, not new model behavior.",
            "Evidence preloading increases initial input; savings require avoided subsequent reads.",
            "Prompt submission reliability and total live cost require a later paired model run.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Public vocabulary may be downloaded by tiktoken if it is not already cached.
    import tiktoken

    report = compare_run(args.baseline_run, tiktoken.get_encoding("cl100k_base"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
