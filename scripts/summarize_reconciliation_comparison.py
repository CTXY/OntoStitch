"""Compare full recorded runs, separating reconciliation-triggered assessment.

Episodes are serial. Their recorded call counts partition model starts in each
component; failed episodes and errors remain included, not just accepted units.
"""

import argparse
import json
from collections import Counter, defaultdict, deque
from pathlib import Path


def summarize_run(root):
    events = [
        json.loads(line)
        for line in (root / "model_calls.jsonl").read_text().splitlines()
    ]
    starts = defaultdict(deque)
    ends = {}
    for event in events:
        if event["event_type"] == "model_start":
            starts[event["component"]].append(event["call_id"])
        elif event["event_type"] == "model_end":
            ends[event["call_id"]] = event["message"]
    totals = defaultdict(Counter)
    tasks = defaultdict(Counter)
    packets = []
    tool_counts = Counter()
    for line in (root / "agent_traces.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event["event_type"] == "tool_result":
            tool_counts[event["name"]] += 1
        if event["event_type"] != "agent_episode":
            continue
        component = event["component"]
        task = event.get("task_id") or "identity"
        stage = (
            "reconciliation"
            if task.startswith("structure-")
            else "matched_and_identity"
        )
        packets.append(
            {
                "task_id": task,
                "component": component,
                "characters": len(
                    json.dumps(event["input_payload"], ensure_ascii=False)
                ),
            }
        )
        for _ in range(event["calls"]):
            call_id = starts[component].popleft()
            message = ends.get(call_id, {})
            usage = message.get("usage_metadata") or {}
            raw = message.get("response_metadata", {}).get("token_usage", {})
            values = {
                "calls": 1,
                "responses": int(call_id in ends),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "reasoning_tokens": usage.get("output_token_details", {}).get(
                    "reasoning", 0
                ),
                "cache_read_tokens": usage.get("input_token_details", {}).get(
                    "cache_read", raw.get("prompt_cache_hit_tokens", 0)
                ),
                "missing_usage": int(not usage),
            }
            totals[stage].update(values)
            totals["complete"].update(values)
            tasks[task].update(values)
    unassigned = sum(len(q) for q in starts.values())
    if unassigned:
        raise ValueError(f"{unassigned} recorded calls have no completed episode trace")
    return {
        "directory": str(root),
        "stages": dict(totals),
        "tasks": dict(tasks),
        "tool_executions": dict(tool_counts),
        "packets": packets,
    }


def summarize_job(root):
    """Include accepted-checkpoint continuations, never hide their ancestor cost."""
    segments, seen = [], set()
    current = root.resolve()
    while current is not None:
        if current in seen:
            raise ValueError("Cycle in experiment resume manifests")
        seen.add(current)
        segments.append(summarize_run(current))
        manifest = json.loads((current / "manifest.json").read_text())
        parent = manifest.get("resumed_from")
        current = Path(parent) if parent else None
    segments.reverse()
    totals, tasks, tools, packets = defaultdict(Counter), {}, Counter(), []
    for segment in segments:
        name = Path(segment["directory"]).name
        for stage, values in segment["stages"].items():
            totals[stage].update(values)
        tasks.update(
            {name + "/" + key: value for key, value in segment["tasks"].items()}
        )
        tools.update(segment["tool_executions"])
        packets.extend({**p, "segment": name} for p in segment["packets"])
    return {
        "directory": str(root),
        "segments": segments,
        "stages": dict(totals),
        "tasks": tasks,
        "tool_executions": dict(tools),
        "packets": packets,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline, candidate = summarize_job(args.baseline), summarize_job(args.candidate)
    report = {
        "baseline": baseline,
        "candidate": candidate,
        "scope": "Provider-reported tokens and recorded invocations; reasoning_tokens is already included in output_tokens. No estimated monetary charge or hidden-retry billing.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {"baseline": baseline["stages"], "candidate": candidate["stages"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
