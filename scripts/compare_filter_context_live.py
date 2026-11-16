"""Fresh full-graph/candidate-context A/B on the frozen prior Filter audit inputs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from collections import Counter
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from cortex.application.online_context import ChoicePoint
from cortex.capabilities.online_context import filtering
from cortex.capabilities.online_context.discovery.evidence import (
    _analysis_graph_payload,
)
from cortex.capabilities.online_context.llm.prompts import prompt_fingerprint
from cortex.config import load_config
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.online_context.probes import SQLiteFilterDatabase
from cortex.schema import GraphState
from scripts.evaluate_clarification_filter_live import (
    DEFAULT_DATABASES,
    Usage,
    write_json,
)


class DetailedUsage(Usage):
    def __init__(self):
        super().__init__()
        self.raw = []

    def on_llm_end(self, response, **kwargs):
        super().on_llm_end(response, **kwargs)
        self.raw.append((response.llm_output or {}).get("token_usage"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def summarize(records):
    result = {}
    for mode in ("full", "candidate"):
        rows = [r for r in records if r["mode"] == mode]
        if not rows:
            continue
        result[mode] = {
            "runs": len(rows),
            "decisions": dict(
                Counter(d["outcome"] for r in rows for d in r["result"]["decisions"])
            ),
            "tokens": dict(sum((Counter(r["tokens"]) for r in rows), Counter())),
            "model_calls": sum(r["model_calls"] for r in rows),
            "seconds_mean": statistics.mean(r["seconds"] for r in rows),
            "seconds_median": statistics.median(r["seconds"] for r in rows),
            "seconds_sum": sum(r["seconds"] for r in rows),
            "notes": [n for r in rows for n in r["result"]["notes"]],
        }
    paired = []
    for index in sorted({r["audit_index"] for r in records}):
        pair = {r["mode"]: r for r in records if r["audit_index"] == index}
        if len(pair) != 2:
            continue
        full = {d["choice_point_id"]: d for d in pair["full"]["result"]["decisions"]}
        candidate = {
            d["choice_point_id"]: d for d in pair["candidate"]["result"]["decisions"]
        }
        for slot, decision in full.items():
            other = candidate[slot]
            paired.append(
                {
                    "audit_index": index,
                    "case_id": pair["full"]["case_id"],
                    "choice_point_id": slot,
                    "full": decision["outcome"],
                    "candidate": other["outcome"],
                    "same_outcome": decision["outcome"] == other["outcome"],
                    "no_selected_implementation": "selected_option_id" not in decision
                    and "selected_option_id" not in other,
                }
            )
    return {"arms": result, "paired_decisions": paired}


async def evaluate(args):
    load_dotenv(".env", override=False)
    config = load_config(args.config)
    settings = config.services.online_context.clarification_filter
    resolved = config.llm.resolve("online_context")
    output = args.output_dir
    if output.exists():
        raise ValueError(
            "Use a fresh output directory; prior runs are never overwritten"
        )
    audit = json.loads(args.audit.read_text())
    jobs = []
    for index, record in enumerate(audit["records"]):
        if args.audit_index is not None and index not in args.audit_index:
            continue
        source = Path(record["artifact_path"])
        input_path = source.parent.parent / "inputs" / (record["case_id"] + ".json")
        case = json.loads(input_path.read_text())
        # Parsing/validation happens before any paid call, with no format regeneration.
        GraphState.model_validate(case["graph_state"])
        for point in case["choice_points"]:
            ChoicePoint.model_validate(point)
        modes = ("full", "candidate") if index % 2 == 0 else ("candidate", "full")
        for mode in modes:
            jobs.append((index, record, case, mode))
        write_json(output / "inputs" / f"{index:02d}.json", case)
    manifest = {
        "model": resolved.model,
        "endpoint_host": urlparse(resolved.base_url or "").hostname,
        "temperature": resolved.temperature,
        "thinking": resolved.enable_thinking,
        "concurrency": args.concurrency,
        "audit": str(args.audit),
        "filter_settings": settings.model_dump(mode="json"),
        "prompt_sha256": prompt_fingerprint("filter_clarifications"),
        "source_hashes": {
            p: sha(p)
            for p in (
                "src/cortex/capabilities/online_context/filtering.py",
                "src/cortex/capabilities/online_context/filter_evidence.py",
                "src/cortex/capabilities/online_context/discovery/prioritization.py",
                "src/cortex/infrastructure/llm/providers.py",
                "src/cortex/infrastructure/llm/deepseek.py",
            )
        },
        "input_sha256": {
            str(i): sha(output / "inputs" / f"{i:02d}.json")
            for i in sorted({j[0] for j in jobs})
        },
        "boundary": "Only question, initial SQL, graph, slots and database availability enter Filter; no gold SQL or expectation labels.",
        "comparison": "Fresh calls in both arms. Test-only ContextVar selects legacy full payload or production builder; all other code/config/rules identical. Pair order alternates; API latency may vary.",
        "input_layout": "Reusable system/tools/dialect/graph prefix, then question/initial SQL/slots/database availability, then changing observations and round controls. Identical ordering in both arms.",
    }
    write_json(output / "manifest.json", manifest)
    mode_context = ContextVar("filter_ab_mode", default="candidate")
    production_builder = filtering.build_filter_evidence

    def builder(state, points):
        if mode_context.get() == "full":
            return _analysis_graph_payload(state), {}
        return production_builder(state, points)

    filtering.build_filter_evidence = builder
    records = []
    sem = asyncio.Semaphore(args.concurrency)

    async def one(index, prior, case, mode):
        async with sem:
            token = mode_context.set(mode)
            try:
                traces, usage = [], DetailedUsage()
                database = (
                    None
                    if case.get("database_available") is False
                    else SQLiteFilterDatabase(
                        args.database_root
                        / case["database_id"]
                        / (case["database_id"] + ".sqlite"),
                        timeout=settings.sql_timeout,
                        max_rows=settings.max_rows,
                    )
                )
                model = create_chat_model(
                    config.llm, profile="online_context"
                ).with_config({"callbacks": [usage]})
                agent = filtering.ClarificationFilterAgent(
                    model,
                    database=database,
                    trace_sink=lambda stage, payload: traces.append(
                        {"stage": stage, "payload": payload}
                    ),
                    max_rounds=settings.max_rounds,
                    max_probes=settings.max_probes,
                )
                started = time.monotonic()
                result = await agent.filter(
                    question=case["question"],
                    initial_sql=case["initial_sql"],
                    retrieved_graph_state=GraphState.model_validate(
                        case["graph_state"]
                    ),
                    choice_points=tuple(
                        ChoicePoint.model_validate(p) for p in case["choice_points"]
                    ),
                    dialect="sqlite",
                )
                row = {
                    "audit_index": index,
                    "case_id": case["case_id"],
                    "mode": mode,
                    "historical_outcomes": prior["outcomes"],
                    "seconds": round(time.monotonic() - started, 3),
                    "model_calls": usage.calls,
                    "tokens": dict(usage.tokens),
                    "raw_usage": usage.raw,
                    "result": result.model_dump(mode="json"),
                    "traces": traces,
                }
                write_json(output / "runs" / f"{index:02d}__{mode}.json", row)
                records.append(row)
                write_json(output / "summary.json", summarize(records))
                print(
                    json.dumps(
                        {
                            k: row[k]
                            for k in (
                                "audit_index",
                                "case_id",
                                "mode",
                                "seconds",
                                "tokens",
                            )
                        }
                        | {"outcomes": [d.outcome for d in result.decisions]},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            finally:
                mode_context.reset(token)

    try:
        await asyncio.gather(*(one(*job) for job in jobs))
    finally:
        filtering.build_filter_evidence = production_builder
    write_json(output / "summary.json", summarize(records))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit",
        type=Path,
        default=Path("artifacts/filter_agent_live/2026-11-14-audit.json"),
    )
    parser.add_argument("--config", default="configs/cortex.yaml")
    parser.add_argument("--database-root", type=Path, default=DEFAULT_DATABASES)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--audit-index", type=int, action="append")
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("concurrency must be positive")
    asyncio.run(evaluate(args))


if __name__ == "__main__":
    main()
