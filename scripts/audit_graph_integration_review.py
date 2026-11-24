"""Verify correspondence and actual Assessment visibility in a completed real run.

This checks mechanical invariants, not semantic truth, and never edits the graph.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cortex.application.merge_graph import GraphMergeResult
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.assessment import assessment_view
from cortex.capabilities.graph_merge.graph.publication import compile_proposal
from cortex.capabilities.graph_merge.inputs import (
    PHYSICAL,
    IntegrationContext,
)
from cortex.capabilities.graph_merge.graph.edit_contract import (
    IdentityDecision,
    IntegrationProposal,
)
from cortex.capabilities.graph_merge.llm.packing import expand_payload
from cortex.capabilities.graph_merge.integration.revision import compose_revision
from cortex.schema import GraphState, SchemaDefinition


def audit(root):
    workflow = json.loads((root / "workflow.json").read_text())
    result = GraphMergeResult.model_validate_json(
        (root / "merge_report.json").read_text()
    )
    request = workflow["request"]
    schema = SchemaDefinition.model_validate(request["schema"])
    old = GraphState.model_validate(request["existing"])
    incoming = GraphState.model_validate(request["incoming"])
    GraphManager(schema).validate_state(result.state)
    assert result.state == GraphState.model_validate_json(
        (root / "graph_state.json").read_text()
    )
    final_nodes = {n.id: n for n in result.state.graph.nodes}
    final_edges = {e.id: e for e in result.state.graph.edges}
    default_count = 0
    for view, source, node_maps, edge_maps in (
        ("incoming", incoming, result.node_mappings, result.edge_mappings),
        ("existing", old, result.existing_node_mappings, result.existing_edge_mappings),
    ):
        nm = {m.input_id: m.canonical_id for m in node_maps}
        em = {m.input_id: m.canonical_id for m in edge_maps}
        nodes = {n.id: n for n in source.graph.nodes}
        rewritten = {
            (r.kind, r.id) for c in result.rewrites for r in c.before if r.view == view
        }
        for node in source.graph.nodes:
            assert node.id in nm or ("node", node.id) in rewritten, node.id
            if node.id in nm:
                assert node.node_type == final_nodes[nm[node.id]].node_type, node.id
        for edge in source.graph.edges:
            assert edge.id in em or ("edge", edge.id) in rewritten, edge.id
            if edge.id in em:
                assert em[edge.id] in final_edges, edge.id
            if edge.reference_origin == "default" and all(
                nodes[k].node_type in PHYSICAL
                for k in (edge.from_node_id, edge.to_node_id)
            ):
                actual = final_edges[em[edge.id]]
                assert (actual.edge_type, actual.from_node_id, actual.to_node_id) == (
                    edge.edge_type,
                    nm[edge.from_node_id],
                    nm[edge.to_node_id],
                ), edge.id
                default_count += 1

    traces = [
        json.loads(line)
        for line in (root / "agent_traces.jsonl").read_text().splitlines()
    ]
    if any(
        t["component"] in {"integration_consolidate", "integration_reconcile"}
        for t in traces
    ):
        context = IntegrationContext(old, incoming, schema)
        context.read_ids.update(context.nodes)
        decision = IdentityDecision.model_validate(workflow["identity"])
        proposal = IntegrationProposal.model_validate(workflow["proposal"])
        preview = compile_proposal(context, decision, proposal)
        from cortex.capabilities.graph_merge.graph.provenance import (
            validate_rewrite_sources,
        )

        validate_rewrite_sources(context, proposal, preview)
        node_reviews = [
            t
            for t in traces
            if t["component"] == "integration_node_assess" and t.get("parsed_response")
        ]
        graph_reviews = [
            t
            for t in traces
            if t["component"] == "integration_reconcile" and t.get("parsed_response")
        ]
        assert (
            graph_reviews
            and graph_reviews[-1]["parsed_response"]["verdict"] == "complete"
        )
        assert workflow.get("review", {}).get("verdict") == "accept" or not node_reviews
        report = {
            "status": "passed",
            "source_node_and_edge_correspondence_complete": True,
            "default_physical_source_edges_mapped": default_count,
            "strict_final_compilation_passed": True,
            "exact_replacement_source_coverage_passed": True,
            "independent_node_assessments": len(node_reviews),
            "graph_reconciliation_completed": True,
            "graph_review_is_self_check": True,
            "scope": "Mechanical invariants and role completion only; not independent graph-semantic proof.",
        }
        (root / "integrity_audit.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        print(json.dumps(report, ensure_ascii=False))
        return

    decision = proposal = valid_proposal = valid_preview = None
    reviews = []
    for line in (root / "agent_traces.jsonl").read_text().splitlines():
        trace = json.loads(line)
        parsed = trace.get("parsed_response")
        if trace["component"] == "integration_identity" and parsed:
            decision = IdentityDecision.model_validate(parsed)
            valid_proposal = valid_preview = None
        elif trace["component"] == "integration_propose" and parsed:
            context = IntegrationContext(old, incoming, schema)
            context.read_ids.update(context.nodes)
            try:
                proposal = IntegrationProposal.model_validate(parsed)
                if trace["payload"].get("proposal_mode") == "delta":
                    proposal = compose_revision(
                        context, decision, valid_proposal, valid_preview, proposal
                    )
                preview = compile_proposal(context, decision, proposal)
            except ValueError:
                continue  # Failed proposals never advance the current valid candidate.
            valid_proposal, valid_preview = proposal, preview
        elif trace["component"] == "integration_assess":
            context = IntegrationContext(old, incoming, schema)
            context.read_ids.update(context.nodes)
            preview = compile_proposal(context, decision, proposal)
            expected = assessment_view(context, decision, preview)
            payload = expand_payload(trace["payload"])
            assert payload["actual_changes"] == expected["actual_changes"]
            for kind, field in (
                ("nodes", "candidate_nodes"),
                ("relations", "candidate_relations"),
            ):
                delta = expected["actual_changes"][kind]
                visible = {obj["id"] for obj in payload[field]}
                assert set(delta["created"] + delta["updated"]) <= visible
            for field in (
                "deleted_nodes",
                "deleted_relations",
                "changed_relation_contracts",
            ):
                assert payload[field] == expected[field]
            reviews.append(
                {
                    "verdict": (parsed or {}).get("verdict"),
                    "actual_changes": expected["actual_changes"],
                }
            )
    assert reviews and reviews[-1]["verdict"] == "accept"
    report = {
        "status": "passed",
        "default_physical_source_edges_mapped": default_count,
        "source_node_and_edge_correspondence_complete": True,
        "safe_node_mappings_preserve_type": True,
        "assessment_actual_changes_visible": True,
        "assessments": reviews,
        "scope": "Mechanical invariants only; semantic review is separate.",
    }
    (root / "integrity_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "assessments"}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    audit(parser.parse_args().run_directory)
