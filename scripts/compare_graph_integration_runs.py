"""Compare measured provider usage for two completed integration runs.

No model calls, price assumptions, or semantic-equivalence claims.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

COUNTS = (
    "model_calls",
    "usage_records",
    "input_tokens",
    "cache_read_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "visible_output_tokens",
    "tool_errors",
    "invalid_tool_calls",
    "plain_text_submissions",
)


def summarize(root):
    traces = [
        json.loads(s) for s in (root / "agent_traces.jsonl").read_text().splitlines()
    ]
    workflow = json.loads((root / "workflow.json").read_text())
    graph = json.loads((root / "graph_state.json").read_text())["graph"]
    phases = []
    for trace in traces:
        messages = trace.get("messages", [])
        usages = [m["usage_metadata"] for m in messages if m.get("usage_metadata")]
        count = Counter(
            model_calls=trace.get("model_calls", 0), usage_records=len(usages)
        )
        for usage in usages:
            count["input_tokens"] += usage.get("input_tokens", 0)
            count["cache_read_tokens"] += usage.get("input_token_details", {}).get(
                "cache_read", 0
            )
            count["output_tokens"] += usage.get("output_tokens", 0)
            count["reasoning_tokens"] += usage.get("output_token_details", {}).get(
                "reasoning", 0
            )
        count["uncached_input_tokens"] = (
            count["input_tokens"] - count["cache_read_tokens"]
        )
        count["visible_output_tokens"] = (
            count["output_tokens"] - count["reasoning_tokens"]
        )
        for message in messages:
            count["invalid_tool_calls"] += len(message.get("invalid_tool_calls", []))
            if (
                message.get("type") == "ai"
                and message.get("content")
                and not message.get("tool_calls")
            ):
                count["plain_text_submissions"] += 1
            if message.get("type") == "tool":
                try:
                    value = json.loads(message.get("content", ""))
                except (ValueError, TypeError):
                    value = {}
                if message.get("status") == "error" or (
                    isinstance(value, dict) and value.get("error")
                ):
                    count["tool_errors"] += 1
        phases.append(
            {
                "phase": trace["component"],
                **{key: count[key] for key in COUNTS},
                "verdict": (trace.get("parsed_response") or {}).get("verdict"),
                "phase_error": trace.get("error"),
                "tools": dict(Counter(t["tool"] for t in trace.get("tools", []))),
            }
        )
    totals = {key: sum(p[key] for p in phases) for key in COUNTS}
    semantics = {
        n["id"]: n
        for n in graph["nodes"]
        if n["node_type"] not in {"table", "column", "dimension_value"}
    }
    return {
        "run": str(root),
        "totals": totals,
        "usage_complete": totals["usage_records"] == totals["model_calls"],
        "phase_counts": dict(Counter(p["phase"] for p in phases)),
        "phases": phases,
        "final_review": workflow.get("review"),
        "semantic_types": dict(Counter(n["node_type"] for n in semantics.values())),
        "business_edges": sum(
            e["from_node_id"] in semantics and e["to_node_id"] in semantics
            for e in graph["edges"]
        ),
        "request": workflow["request"],
    }


def compare(before, after):
    a, b = summarize(before), summarize(after)
    ar, br = a.pop("request"), b.pop("request")
    parity = {
        key: ar.get(key) == br.get(key)
        for key in (
            "existing",
            "incoming",
            "schema",
            "max_rounds",
            "top_k",
            "runtime_signature",
        )
    }
    parity["source_evidence"] = json.loads(
        (before / "source_evidence.json").read_text()
    ) == json.loads((after / "source_evidence.json").read_text())
    reductions = {
        key: round(100 * (1 - b["totals"][key] / value), 2)
        for key, value in a["totals"].items()
        if value
    }
    return {
        "baseline": a,
        "optimized": b,
        "same_inputs_and_configuration": parity,
        "reduction_percent": reductions,
        "limits": [
            "One observed pair, not an estimate of expected savings across cases or repeated trials.",
            "Token counts are provider-reported; no dollar price assumptions.",
            "Counts and final accept do not themselves establish semantic quality.",
            "If usage_complete is false, token totals are a lower bound for recorded calls.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--optimized", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.baseline, args.optimized)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "parity": report["same_inputs_and_configuration"],
                "before": report["baseline"]["totals"],
                "after": report["optimized"]["totals"],
                "reduction_percent": report["reduction_percent"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
