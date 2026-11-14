"""Live, gold-free Filter Agent audit using archived BIRD slots and real databases."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler

from cortex.application.online_context import ChoicePoint
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.online_context.filtering import ClarificationFilterAgent
from cortex.capabilities.online_context.llm.prompts import prompt_fingerprint
from cortex.config import load_config
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.online_context.probes import SQLiteFilterDatabase
from cortex.schema import (
    ConflictPoint,
    GraphNode,
    GraphSpace,
    GraphState,
    SourceReference,
)

DEFAULT_RESULTS = Path(
    "/Users/yichendezaizai/MY_CODE/autoonto-nl2sql-release/results_final"
)
DEFAULT_DATABASES = Path(
    "/Users/yichendezaizai/CodeBase/Text2SQL/datasets/bird/dev/dev_databases"
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def normalize_properties(properties):
    """Preserve historical target definitions while supplying current read views."""
    result = dict(properties)
    targets = result.get("targets")
    if targets and isinstance(targets[0], dict):
        groups = [list(t.get("references") or []) for t in targets]
        if any(not g for g in groups):
            raise ValueError("legacy target without physical references")
        result["legacy_target_definitions"] = targets
        result["targets"] = groups
        result["target_expressions"] = {
            str(i): t["sql_expression"]
            for i, t in enumerate(targets)
            if t.get("sql_expression")
        }
    return result


def load_archived_graph(path, selected_ids):
    """Format-only conversion; no root-schema rewriting or semantic regeneration."""
    raw = json.loads(path.read_text())
    raw_nodes = raw["graph"]["nodes"]
    conflicts = {c["node_id"]: c for c in raw.get("node_conflicts", [])}
    nodes = []
    for raw_node in raw_nodes:
        if raw_node["id"] not in selected_ids:
            continue
        payload = dict(raw_node)
        payload["properties"] = normalize_properties(payload["properties"])
        payload["merge_status"] = "normal"
        node = GraphNode.model_validate(payload)
        conflict = conflicts.get(node.id)
        if conflict:
            candidates = []
            for alternative in conflict["alternatives"]:
                content = dict(alternative["content"])
                content["properties"] = normalize_properties(content["properties"])
                candidates.append(
                    (
                        alternative["alternative_id"],
                        content,
                        tuple(
                            SourceReference.model_validate(s)
                            for s in alternative["sources"]
                        ),
                    )
                )
            node, _ = GraphManager.compact_conflict(
                node,
                candidates,
                conflict_points=tuple(
                    ConflictPoint.model_validate(
                        {
                            **p,
                            "choices": [
                                {k: v for k, v in c.items() if k != "source_case_ids"}
                                for c in p["choices"]
                            ],
                        }
                    )
                    for p in conflict["conflict_points"]
                ),
            )
        nodes.append(node)
    retained = {n.id for n in nodes}
    edges = [
        e
        for e in raw["graph"]["edges"]
        if e["from_node_id"] in retained and e["to_node_id"] in retained
    ]
    return GraphState(graph=GraphSpace(nodes=tuple(nodes), edges=edges))


def build_cases(results_root):
    root = results_root / "workspace/BIRD/autoonto"
    records = {}
    for db in (
        "debit_card_specializing",
        "formula_1",
        "financial",
        "codebase_community",
    ):
        records[db] = {
            str(x["source_id"]): x
            for x in json.loads(
                (root / db / "online_guidance/online_results.json").read_text()
            )["results"]
        }
    cases = []

    def original(db, source, suffix, expected, reason):
        record = records[db][source]
        before = record["online_before_interaction"]
        question = next(
            q
            for q in before["interaction_questions"]
            if q["question_id"].endswith(suffix)
        )
        point = ChoicePoint(
            choice_point_id=question["choice_point_ids"][0],
            scope=question["scope"],
            decision_question=question["prompt"],
            options=question["options"],
        )
        state = load_archived_graph(
            root / db / "merged_graph/graph_state.json",
            set(before["retrieved_node_ids"]),
        )
        case = {
            "case_id": f"{db}_{source}_{suffix}",
            "kind": "archived",
            "database_id": db,
            "source_id": source,
            "source_file": str(root / db / "online_guidance/online_results.json"),
            "question": before["question"],
            "initial_sql": before["initial_sql"],
            "choice_points": [point.model_dump(mode="json")],
            "graph_state": state.model_dump(mode="json"),
            "expected_outcomes": expected,
            "expectation_reason": reason,
        }
        cases.append(case)
        return case

    resolved = ["question_resolved"]
    equivalent = ["database_equivalent", "not_relevant"]
    morning = original(
        "debit_card_specializing",
        "1516",
        "slot_time_filter",
        resolved,
        "Evidence explicitly defines morning as before 13:00.",
    )
    counting = original(
        "debit_card_specializing",
        "1516",
        "slot_count_method",
        equivalent,
        "TransactionID is globally non-NULL; inner join does not introduce NULL.",
    )
    original(
        "debit_card_specializing",
        "1501",
        "slot_date_filter",
        equivalent,
        "yearmonth.Date is six-character YYYYMM; prefix and exact month match coincide.",
    )
    original(
        "debit_card_specializing",
        "1512",
        "slot_1",
        resolved,
        "The stated day excludes direct use of unfiltered monthly consumption.",
    )
    original(
        "debit_card_specializing",
        "1512",
        "slot_3",
        ["keep"],
        "No explicit tie policy; base-column properties do not settle grouped daily ranking.",
    )
    original(
        "debit_card_specializing",
        "1494",
        "slot_premium_segment_filter",
        resolved,
        "Question explicitly requests Premium, not Value for money.",
    )
    original(
        "debit_card_specializing",
        "1494",
        "slot_percentage_calculation",
        equivalent,
        "The two conditional percentage implementations are equivalent, not different policies.",
    )
    original(
        "formula_1",
        "969",
        "slot_born_1980_filter",
        resolved,
        "Question/evidence specifies 1980, not all years before 1982.",
    )
    original(
        "formula_1",
        "969",
        "slot_count_aggregation",
        equivalent,
        "drivers.driverId is globally non-NULL in the same single-table row source.",
    )
    original(
        "formula_1",
        "969",
        "slot_british_nationality_binding",
        resolved,
        "British drivers constrains the entity; initial SQL FROM drivers does not bind constructors.",
    )
    original(
        "formula_1",
        "947",
        "slot_dob_filter",
        resolved,
        "Evidence explicitly says year(dob)>1980.",
    )
    card = original(
        "financial",
        "139",
        "slot_1",
        equivalent,
        "Card ID uniqueness is preserved by card -> disp unique-key join.",
    )
    users = original(
        "codebase_community",
        "672",
        "slot_count_method",
        ["keep"],
        "Base users.Id uniqueness does not survive the user-to-post one-to-many join; no explicit DISTINCT instruction.",
    )

    def variant(base, suffix, expected, reason, **updates):
        value = {
            **base,
            **updates,
            "case_id": base["case_id"] + "__" + suffix,
            "kind": "controlled_variant",
            "expected_outcomes": expected,
            "expectation_reason": reason,
            "derived_from": base["case_id"],
        }
        cases.append(value)
        return value

    variant(
        morning,
        "no_boundary_evidence",
        ["keep"],
        "Morning alone does not explicitly choose 12:00 or 13:00; actual Time values cannot define that policy.",
        question=morning["question"].split("\nEvidence:")[0],
    )
    variant(
        morning,
        "contradictory_evidence",
        ["keep"],
        "Question and evidence prescribe conflicting boundaries; neither is an unambiguous instruction.",
        question=morning["question"].split("\nEvidence:")[0]
        + " Use morning as before 12:00:00.\nEvidence: The morning refers to the time before '13:00:00'.",
    )
    variant(
        card,
        "one_to_many_join",
        ["keep"],
        "Unique card IDs can repeat after joining disposition account to transaction rows.",
        initial_sql=(
            "SELECT COUNT(DISTINCT card.card_id) FROM card JOIN disp ON card.disp_id=disp.disp_id "
            "JOIN trans ON disp.account_id=trans.account_id WHERE card.type='gold' AND disp.type='OWNER'"
        ),
    )
    variant(
        counting,
        "database_unavailable",
        ["keep"],
        "No database evidence; question contains no explicit COUNT operand instruction.",
        database_available=False,
    )
    point = ChoicePoint.model_validate(users["choice_points"][0])
    non_distinct = tuple(
        o for o in point.options if "DISTINCT" not in o.statement.upper()
    )
    variant(
        users,
        "nonnull_inner_join_operands",
        equivalent,
        "COUNT(*) and COUNT(users.Id) coincide in this inner join even if users repeat.",
        choice_points=[
            point.model_copy(update={"options": non_distinct}).model_dump(mode="json")
        ],
    )
    return cases


class Usage(BaseCallbackHandler):
    def __init__(self):
        self.calls = 0
        self.tokens = Counter()

    def on_llm_end(self, response, **kwargs):
        self.calls += 1
        usage = (response.llm_output or {}).get("token_usage")
        if usage:
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                self.tokens[k] += usage.get(k, 0) or 0
        else:
            for generation in response.generations:
                for item in generation:
                    data = getattr(item.message, "usage_metadata", None) or {}
                    self.tokens["total_tokens"] += data.get("total_tokens", 0)


async def evaluate(args):
    load_dotenv(".env", override=False)
    config = load_config(args.config)
    settings = config.services.online_context.clarification_filter
    resolved = config.llm.resolve("online_context")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    cases = build_cases(args.results_root)
    if args.case_id:
        cases = [c for c in cases if c["case_id"] in set(args.case_id)]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise ValueError("No matching cases")
    fingerprint = hashlib.sha256(
        Path("src/cortex/capabilities/online_context/filtering.py").read_bytes()
    ).hexdigest()
    manifest = {
        "model": resolved.model,
        "endpoint_host": urlparse(resolved.base_url or "").hostname,
        "temperature": resolved.temperature,
        "thinking": resolved.enable_thinking,
        "production_agent_sha256": fingerprint,
        "prompt_sha256": prompt_fingerprint("filter_clarifications"),
        "filter_settings": settings.model_dump(mode="json"),
        "repeats": args.repeats,
        "concurrency": args.concurrency,
        "cases": [{k: v for k, v in c.items() if k != "graph_state"} for c in cases],
        "gold_boundary": "Gold SQL, simulated decisions and expectations are not sent to the model.",
        "format_conversion": "Legacy complete alternatives compacted; old target definitions retained alongside current read views.",
    }
    write_json(output / "manifest.json", manifest)
    for case in cases:
        write_json(output / "inputs" / f"{case['case_id']}.json", case)
    sem = asyncio.Semaphore(args.concurrency)
    records = []

    async def one(case, repetition):
        async with sem:
            traces, usage = [], Usage()
            database_path = (
                args.database_root
                / case["database_id"]
                / (case["database_id"] + ".sqlite")
            )
            database = (
                None
                if case.get("database_available") is False
                else SQLiteFilterDatabase(
                    database_path,
                    timeout=settings.sql_timeout,
                    max_rows=settings.max_rows,
                )
            )
            model = create_chat_model(config.llm, profile="online_context").with_config(
                {"callbacks": [usage]}
            )
            agent = ClarificationFilterAgent(
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
                retrieved_graph_state=GraphState.model_validate(case["graph_state"]),
                choice_points=tuple(
                    ChoicePoint.model_validate(s) for s in case["choice_points"]
                ),
                dialect="sqlite",
            )
            outcomes = [d.outcome for d in result.decisions]
            expected = case["expected_outcomes"]
            record = {
                "case_id": case["case_id"],
                "kind": case["kind"],
                "repetition": repetition,
                "expected_outcomes": expected,
                "outcomes": outcomes,
                "matches_predeclared_expectation": all(x in expected for x in outcomes),
                "seconds": round(time.monotonic() - started, 2),
                "model_calls": usage.calls,
                "tokens": dict(usage.tokens),
                "result": result.model_dump(mode="json"),
                "traces": traces,
            }
            write_json(
                output / "runs" / f"{case['case_id']}__r{repetition}.json", record
            )
            records.append(
                {k: v for k, v in record.items() if k not in ("result", "traces")}
            )
            write_json(
                output / "summary.json", {"manifest": manifest, "records": records}
            )
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in record.items()
                        if k not in ("result", "traces", "tokens")
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    await asyncio.gather(
        *(one(c, r) for c in cases for r in range(1, args.repeats + 1))
    )
    summary = {
        "manifest": manifest,
        "records": sorted(records, key=lambda r: (r["case_id"], r["repetition"])),
        "total_runs": len(records),
        "matches_predeclared_expectation": sum(
            r["matches_predeclared_expectation"] for r in records
        ),
        "model_calls": sum(r["model_calls"] for r in records),
        "tokens": dict(sum((Counter(r["tokens"]) for r in records), Counter())),
    }
    write_json(output / "summary.json", summary)
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in ("manifest", "records")},
            ensure_ascii=False,
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--database-root", type=Path, default=DEFAULT_DATABASES)
    parser.add_argument("--config", default="configs/cortex.yaml")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1 or args.concurrency < 1:
        parser.error("repeats and concurrency must be positive")
    asyncio.run(evaluate(args))


if __name__ == "__main__":
    main()
