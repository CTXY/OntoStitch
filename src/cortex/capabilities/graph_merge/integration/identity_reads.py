"""Recover legacy identity read bookkeeping from actual recorded tool results."""
import json
from pathlib import Path


def identity_reads_from_trace(path, decision):
    """Return observed read IDs only for a trace with this exact accepted decision.

    The caller must first verify the checkpoint's graph/schema/evidence fingerprint.
    Never manufacture reads from the identity decision's referenced member IDs.
    """
    observed = set()
    matched = None
    with Path(path).open() as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # An interrupted final JSONL write contains no read proof.
            if row.get("component") != "integration_identity":
                continue
            if row.get("event_type") == "tool_result" and row.get("name") == "inspect_graph":
                args, result = row.get("arguments") or {}, row.get("result") or {}
                if not isinstance(result, dict) or result.get("error"):
                    continue
                if args.get("mode") == "nodes" and result.get("view", args.get("view", "input")) == "input":
                    observed.update(item["id"] for item in result.get("items", []) if "id" in item)
                elif args.get("mode") == "candidates":
                    pages = result.get("pages", {"single": result})
                    for page in pages.values():
                        observed.update(item["id"] for item in page.get("items", []) if "id" in item)
            if row.get("event_type") == "agent_episode" and row.get("parsed_response") == decision:
                initial = row.get("input_payload") or {}
                observed.update(item["id"] for item in initial.get("nodes", []) if "id" in item)
                matched = sorted(observed)
    if matched is None:
        raise ValueError("No recorded identity episode matches the checkpoint decision")
    return matched
