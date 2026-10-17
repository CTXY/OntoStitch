"""Evaluate normalized Cases through the existing online and baseline implementations."""

import argparse
import asyncio
import json
from pathlib import Path

import yaml
from run_bird_dev_online_guided_baseline import (
    _load_env_file,
    _run_guided_baseline,
    _write_online_guidance,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--graph-state", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--database-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/cortex.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--database-description", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _load_env_file(args.env_file)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
    if not rows or len({row["source_id"] for row in rows}) != len(rows):
        raise ValueError("Cases must be nonempty with unique source IDs")
    rows = [{**row, "database_id": args.database} for row in rows]
    online_cases_path = output / "online_cases.jsonl"
    online_cases_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    summary = asyncio.run(
        _write_online_guidance(
            database_id=args.database,
            graph_state_path=args.graph_state.resolve(),
            cases_path=online_cases_path,
            output_dir=output / "guidance",
            config_path=args.config.resolve(),
            database_description=args.database_description or output / "no_descriptions",
        )
    )
    guidance_path = output / "guidance/guidance_cases.json"
    guidance = json.loads(guidance_path.read_text())["cases"]
    if [case["source_id"] for case in guidance] != [row["source_id"] for row in rows] or any(
        not case["downstream_guidance"]["agent_handoff"]["guidance_content"].strip()
        for case in guidance
    ):
        raise ValueError("every input Case must have nonempty terminal guidance")
    test_set = output / "cases.json"
    test_set.write_text(json.dumps([{**row, "db_id": args.database} for row in rows], indent=2))
    baseline_config = {
        "dataset": {"type": "bird"},
        "test_set_path": str(test_set),
        "workspace_root": str(output / "baseline"),
        "database": {"root": str(args.database_root.resolve())},
        "schema_linking": {"type": "full_schema"},
        "memory": {"type": "downstream_guidance", "source_path": str(guidance_path)},
        "llm": {
            "model": "${CORTEX_LLM_MODEL}",
            "base_url": "${CORTEX_LLM_BASE_URL}",
            "api_key": "${CORTEX_LLM_API_KEY}",
            "temperature": 0,
            "timeout": 300,
        },
        "evaluation": {"top_k": 5, "sql_timeout": 30},
    }
    config_path = output / "baseline.yaml"
    config_path.write_text(yaml.safe_dump(baseline_config, sort_keys=False))
    baseline = _run_guided_baseline(
        baseline_root=Path(__file__).resolve().parents[1] / "baseline",
        config_path=config_path,
        env_file=args.env_file.resolve(),
        final_root=output / "final_results",
    )
    (output / "summary.json").write_text(
        json.dumps({"guidance": summary, "baseline": baseline}, indent=2)
    )
    print(json.dumps({"output": str(output), "baseline": baseline}, indent=2))


if __name__ == "__main__":
    main()
