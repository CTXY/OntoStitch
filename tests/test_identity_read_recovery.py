import json

import pytest

from cortex.capabilities.graph_merge.integration.identity_reads import identity_reads_from_trace


def test_legacy_identity_reads_require_actual_tool_results_and_a_matching_decision(tmp_path):
    decision = {"groups": [{"members": ["seed", "read", "never_read"]}], "distinctions": []}
    records = [
        {"component": "integration_identity", "event_type": "tool_result", "name": "inspect_graph",
         "arguments": {"mode": "nodes", "view": "input"},
         "result": {"view": "input", "items": [{"id": "read"}]}},
        {"component": "integration_identity", "event_type": "tool_result", "name": "inspect_graph",
         "arguments": {"mode": "nodes", "view": "candidate"},
         "result": {"view": "candidate", "items": [{"id": "not_an_input_read"}]}},
        {"component": "integration_identity", "event_type": "tool_result", "name": "inspect_graph",
         "arguments": {"mode": "candidates"},
         "result": {"pages": {"seed": {"items": [{"id": "candidate_read"}]}}}},
        {"component": "integration_identity", "event_type": "agent_episode",
         "input_payload": {"nodes": [{"id": "seed"}]}, "parsed_response": decision},
    ]
    path = tmp_path / "agents.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in records))
    assert identity_reads_from_trace(path, decision) == ["candidate_read", "read", "seed"]
    with pytest.raises(ValueError, match="matches"):
        identity_reads_from_trace(path, {"groups": [], "distinctions": []})
