"""Aggregate real provider usage without double-counting resumed/copied traces."""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def ancestry(directory):
    seen = set()
    while directory and directory.resolve() not in seen:
        directory = directory.resolve()
        seen.add(directory)
        yield directory
        path = directory / "resume_manifest.json"
        previous = (
            json.loads(path.read_text()).get("resumed_from") if path.exists() else None
        )
        directory = Path(previous) if previous else None


def summarize(directories):
    starts, ends, errors = {}, {}, {}
    for directory in directories:
        path = directory / "model_calls.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            row = json.loads(line)
            target = {
                "model_start": starts,
                "model_end": ends,
                "model_error": errors,
            }.get(row["event_type"])
            if target is not None:
                target[row["call_id"]] = row
    total = {
        "calls": len(starts),
        "responses": len(ends),
        "errors": len(errors),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "missing_usage_calls": [],
    }
    phases = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0})
    for cid, row in starts.items():
        phase = phases[row["component"]]
        phase["calls"] += 1
        message = ends.get(cid, {}).get("message", {})
        usage = message.get("usage_metadata")
        if not usage:
            total["missing_usage_calls"].append(cid)
            continue
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key, 0)
            total[key] += value
            phase[key] += value
        raw = message.get("response_metadata", {}).get("token_usage", {})
        total["cache_read_tokens"] += usage.get("input_token_details", {}).get(
            "cache_read", raw.get("prompt_cache_hit_tokens", 0)
        )
    total["total_reported_tokens"] = total["input_tokens"] + total["output_tokens"]
    total["phases"] = dict(phases)
    total["usage_complete"] = not total["missing_usage_calls"]
    total["scope"] = (
        "Unique model invocation IDs, including failed experiments. Missing provider usage is not estimated. SDK transport retries may lack separate usage."
    )
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--all-experiments", action="store_true")
    args = parser.parse_args()
    directories = (
        [p.parent for p in args.directory.rglob("model_calls.jsonl")]
        if args.all_experiments
        else list(ancestry(args.directory))
    )
    report = summarize(directories)
    output = args.directory / (
        "all_experiments_usage.json"
        if args.all_experiments
        else "cumulative_usage.json"
    )
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
