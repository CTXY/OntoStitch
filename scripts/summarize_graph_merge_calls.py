"""Count recorded model calls/usage, including failed merge episodes, without billing guesses."""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def summarize(root):
    events = [
        json.loads(l) for l in (root / "model_calls.jsonl").read_text().splitlines()
    ]
    starts = {e["call_id"]: e for e in events if e["event_type"] == "model_start"}
    ends = {e["call_id"]: e for e in events if e["event_type"] == "model_end"}
    errors = {e["call_id"]: e for e in events if e["event_type"] == "model_error"}
    phases = defaultdict(
        lambda: {
            "calls": 0,
            "responses": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "errors": 0,
            "cache_read_tokens": 0,
        }
    )
    for key, e in starts.items():
        row = phases[e["component"]]
        row["calls"] += 1
        if key in errors:
            row["errors"] += 1
        if key not in ends:
            continue
        m = ends[key]["message"]
        u = m.get("usage_metadata") or {}
        row["responses"] += 1
        row["input_tokens"] += u.get("input_tokens", 0)
        row["output_tokens"] += u.get("output_tokens", 0)
        raw = m.get("response_metadata", {}).get("token_usage", {})
        row["cache_read_tokens"] += u.get("input_token_details", {}).get(
            "cache_read", raw.get("prompt_cache_hit_tokens", 0)
        )
    total = {
        k: sum(r[k] for r in phases.values())
        for k in [
            "calls",
            "responses",
            "input_tokens",
            "output_tokens",
            "errors",
            "cache_read_tokens",
        ]
    }
    total.update(
        phases=dict(phases),
        usage_missing_calls=sorted(set(starts) - set(ends)),
        usage_complete=all(
            (ends.get(k, {}).get("message", {}).get("usage_metadata")) for k in starts
        ),
        scope="Recorded model invocations and provider-reported response tokens. SDK internal transport retries may not have usage. No invented monetary price.",
    )
    (root / "usage_report.json").write_text(
        json.dumps(total, ensure_ascii=False, indent=2)
    )
    print(json.dumps(total, ensure_ascii=False))
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    summarize(parser.parse_args().run_directory)
