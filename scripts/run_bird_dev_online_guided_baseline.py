"""Run BIRD dev online-context guidance and guided baseline EA.

The runner starts from existing merged graph_state.json artifacts under
workspace/bird/training/<database>/, generates terminal Online Context guidance
for each BIRD dev case, runs the standalone guided baseline, and summarizes EA by
database plus overall.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from cortex.bootstrap.online_context import (
    OnlineContextEvaluationWorker,
    build_online_context_evaluation_pool,
)
from cortex.infrastructure.online_context import (
    DatabaseDescriptionLoader,
    FileSystemGraphStateLoader,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE_ROOT = REPO_ROOT / "workspace/bird/training"
DEFAULT_DEV_ROOT = REPO_ROOT / "data/bird/dev"
DEFAULT_DATABASE_ROOT = REPO_ROOT / "data/databases"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "workspace/bird/online"
DEFAULT_BASELINE_ROOT = REPO_ROOT / "baseline"
DEFAULT_CONFIG = REPO_ROOT / "configs/cortex.yaml"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


def main() -> None:
    args = _parse_args()
    _load_env_file(args.env_file)
    run_root = _reserve_run_root(args.output_root, args.run_name, resume=args.resume)
    summaries: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    if args.global_online and not args.skip_guidance:
        _run_global_online_guidance(run_root, args)
    if args.database_max_workers <= 1:
        for database_id in args.database:
            try:
                summaries.append(_run_database(database_id, run_root, args))
            except Exception as exc:  # noqa: BLE001 - keep the batch useful.
                failure = _record_failure(run_root, database_id, exc)
                failures.append(failure)
                if not args.keep_going:
                    raise
    else:
        with ThreadPoolExecutor(max_workers=args.database_max_workers) as executor:
            future_by_database = {
                executor.submit(_run_database, database_id, run_root, args): database_id
                for database_id in args.database
            }
            for future in as_completed(future_by_database):
                database_id = future_by_database[future]
                try:
                    summaries.append(future.result())
                except Exception as exc:  # noqa: BLE001 - keep the batch useful.
                    failure = _record_failure(run_root, database_id, exc)
                    failures.append(failure)
                    if not args.keep_going:
                        raise
    aggregate = {
        "run_root": str(run_root),
        "databases": summaries,
        "failures": failures,
        "totals": _aggregate(summaries),
    }
    _write_json(run_root / "summary.json", aggregate)
    print(json.dumps(aggregate, ensure_ascii=False, indent=2), flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", action="append", help="BIRD database ID; repeatable")
    parser.add_argument("--workspace-root", type=Path, default=DEFAULT_WORKSPACE_ROOT)
    parser.add_argument("--dev-root", type=Path, default=DEFAULT_DEV_ROOT)
    parser.add_argument("--database-root", type=Path, default=DEFAULT_DATABASE_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--baseline-root", type=Path, default=DEFAULT_BASELINE_ROOT)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-name", help="Use a stable run directory name")
    parser.add_argument("--resume", action="store_true", help="Reuse completed artifacts")
    parser.add_argument(
        "--database-max-workers",
        type=int,
        default=1,
        help="Maximum number of databases to process concurrently",
    )
    parser.add_argument(
        "--global-online",
        action="store_true",
        help="Run online guidance for all databases through one shared worker pool first",
    )
    parser.add_argument("--keep-going", action="store_true")
    parser.add_argument("--skip-guidance", action="store_true")
    parser.add_argument("--skip-baseline", action="store_true")
    args = parser.parse_args()
    if not args.database:
        args.database = sorted(path.stem for path in args.dev_root.glob("*.json"))
    return args


def _record_failure(run_root: Path, database_id: str, exc: BaseException) -> dict[str, str]:
    failure = {"database_id": database_id, "error": f"{type(exc).__name__}: {exc}"}
    _write_json(run_root / database_id / "failure.json", failure)
    return failure


def _run_global_online_guidance(run_root: Path, args: argparse.Namespace) -> None:
    asyncio.run(_run_global_online_guidance_async(run_root, args))


async def _run_global_online_guidance_async(
    run_root: Path, args: argparse.Namespace
) -> None:
    pool = build_online_context_evaluation_pool(args.config.resolve())
    pending_by_database: dict[str, deque[dict[str, Any]]] = {}
    state_by_database: dict[str, dict[str, Any]] = {}
    for database_id in args.database:
        db_root = run_root / database_id
        db_root.mkdir(parents=True, exist_ok=True)
        cases_path = _write_cases(database_id, args.dev_root, db_root)
        graph_state_path = _find_graph_state(args.workspace_root, database_id)
        guidance_path = db_root / "online_guidance/guidance_cases.json"
        if _guidance_complete(guidance_path, cases_path):
            continue
        output_dir = db_root / "online_guidance"
        graph_state = FileSystemGraphStateLoader().load(graph_state_path)
        database_description = args.database_root / database_id / "database_description"
        column_context = (
            DatabaseDescriptionLoader().load(database_description)
            if database_description.is_dir()
            else ()
        )
        cases = _read_jsonl(cases_path)
        checkpoint = output_dir / "online_results.json"
        existing_by_source = _load_checkpoint_results(checkpoint)
        results: list[dict[str, Any] | None] = [None] * len(cases)
        for index, case in enumerate(cases):
            if record := existing_by_source.get(str(case["source_id"])):
                results[index] = record
        state_by_database[database_id] = {
            "cases": cases,
            "cases_path": cases_path,
            "checkpoint": checkpoint,
            "column_context": column_context,
            "database_description": database_description,
            "graph_state": graph_state,
            "graph_state_path": graph_state_path,
            "output_dir": output_dir,
            "results": results,
        }
        pending_count = 0
        for index, case in enumerate(cases):
            if results[index] is None:
                pending_count += 1
                pending_by_database.setdefault(database_id, deque()).append(
                    {
                        "database_id": database_id,
                        "index": index,
                        "case": case,
                    }
                )
        print(
            json.dumps(
                {
                    "database_id": database_id,
                    "event": "global_online_database_queued",
                    "completed": len(cases) - pending_count,
                    "pending": pending_count,
                    "total": len(cases),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    work: list[dict[str, Any]] = []
    while any(pending_by_database.values()):
        for database_id in args.database:
            pending = pending_by_database.get(database_id)
            if pending:
                work.append(pending.popleft())

    lock = asyncio.Lock()

    async def run_one(item: dict[str, Any]) -> None:
        database_id = str(item["database_id"])
        state = state_by_database[database_id]
        async with pool.lease() as worker:
            record = await _online_case(
                worker,
                graph_state=state["graph_state"],
                column_context=state["column_context"],
                case=item["case"],
            )
        async with lock:
            state["results"][item["index"]] = record
            _write_json(
                state["checkpoint"],
                {"results": [row for row in state["results"] if row is not None]},
            )
        print(
            json.dumps(
                {
                    "database_id": database_id,
                    "source_id": record["source_id"],
                    "status": record.get("status"),
                    "guidance": bool(record.get("guidance_content")),
                    "error": record.get("error"),
                    "error_chain": record.get("error_chain"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    await asyncio.gather(*(run_one(item) for item in work))
    for database_id, state in state_by_database.items():
        _write_guidance_artifacts(
            database_id=database_id,
            cases=state["cases"],
            records=[row for row in state["results"] if row is not None],
            output_dir=state["output_dir"],
            graph_state_path=state["graph_state_path"],
            cases_path=state["cases_path"],
            config_path=args.config.resolve(),
            database_description=state["database_description"],
            workers=pool.max_workers,
        )


def _run_database(database_id: str, run_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    db_root = run_root / database_id
    db_root.mkdir(parents=True, exist_ok=True)
    stale_failure = db_root / "failure.json"
    if stale_failure.exists():
        stale_failure.unlink()
    print(
        json.dumps(
            {"database_id": database_id, "event": "database_start"},
            ensure_ascii=False,
        ),
        flush=True,
    )
    cases_path = _write_cases(database_id, args.dev_root, db_root)
    graph_state_path = _find_graph_state(args.workspace_root, database_id)
    guidance_path = db_root / "online_guidance/guidance_cases.json"
    if args.skip_guidance:
        guidance_summary = {"skipped": True}
    elif args.resume and _guidance_complete(guidance_path, cases_path):
        guidance_summary = _summarize_guidance(guidance_path)
    else:
        guidance_summary = asyncio.run(
            _write_online_guidance(
                database_id=database_id,
                graph_state_path=graph_state_path,
                cases_path=cases_path,
                output_dir=db_root / "online_guidance",
                config_path=args.config.resolve(),
                database_description=args.database_root
                / database_id
                / "database_description",
            )
        )

    baseline: dict[str, Any] = {}
    if not args.skip_baseline:
        baseline_config = _write_baseline_config(
            db_root / "settings/baseline_guidance.yaml",
            database_root=args.database_root.resolve(),
            test_set=cases_path,
            workspace_root=db_root / "final_results/baseline_guidance",
            guidance_path=guidance_path,
        )
        if args.resume and _baseline_complete(db_root / "final_results"):
            baseline = _load_existing_baseline(db_root / "final_results", baseline_config)
        else:
            baseline = _run_guided_baseline(
                baseline_root=args.baseline_root.resolve(),
                config_path=baseline_config,
                env_file=args.env_file.resolve(),
                final_root=db_root / "final_results",
            )
    summary = {
        "database_id": database_id,
        "cases": str(cases_path),
        "graph_state": str(graph_state_path),
        "online_guidance": guidance_summary | {"path": str(guidance_path)},
        "final_results": baseline,
    }
    _write_json(db_root / "summary.json", summary)
    return summary


async def _write_online_guidance(
    *,
    database_id: str,
    graph_state_path: Path,
    cases_path: Path,
    output_dir: Path,
    config_path: Path,
    database_description: Path,
) -> dict[str, Any]:
    graph_state = FileSystemGraphStateLoader().load(graph_state_path)
    column_context = (
        DatabaseDescriptionLoader().load(database_description)
        if database_description.is_dir()
        else ()
    )
    pool = build_online_context_evaluation_pool(config_path)
    cases = _read_jsonl(cases_path)
    results: list[dict[str, Any] | None] = [None] * len(cases)
    checkpoint = output_dir / "online_results.json"
    existing_by_source = _load_checkpoint_results(checkpoint)
    for index, case in enumerate(cases):
        if record := existing_by_source.get(str(case["source_id"])):
            results[index] = record
    lock = asyncio.Lock()

    async def run_one(index: int, case: dict[str, Any]) -> None:
        async with pool.lease() as worker:
            record = await _online_case(
                worker,
                graph_state=graph_state,
                column_context=column_context,
                case=case,
            )
        async with lock:
            results[index] = record
            _write_json(
                checkpoint,
                {"results": [item for item in results if item is not None]},
            )
        print(
            json.dumps(
                {
                    "database_id": database_id,
                    "source_id": record["source_id"],
                    "status": record.get("status"),
                    "guidance": bool(record.get("guidance_content")),
                    "error": record.get("error"),
                    "error_chain": record.get("error_chain"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    pending = [
        (index, case)
        for index, case in enumerate(cases)
        if results[index] is None
    ]
    if existing_by_source:
        print(
            json.dumps(
                {
                    "database_id": database_id,
                    "event": "checkpoint_resume",
                    "completed": len(cases) - len(pending),
                    "total": len(cases),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    await asyncio.gather(*(run_one(index, case) for index, case in pending))
    completed = [item for item in results if item is not None]
    summary = _write_guidance_artifacts(
        database_id=database_id,
        cases=cases,
        records=completed,
        output_dir=output_dir,
        graph_state_path=graph_state_path,
        cases_path=cases_path,
        config_path=config_path,
        database_description=database_description,
        workers=pool.max_workers,
    )
    return summary


def _write_guidance_artifacts(
    *,
    database_id: str,
    cases: list[dict[str, Any]],
    records: list[dict[str, Any]],
    output_dir: Path,
    graph_state_path: Path,
    cases_path: Path,
    config_path: Path,
    database_description: Path,
    workers: int,
) -> dict[str, Any]:
    guidance_cases = [
        _guidance_case(case, record)
        for case, record in zip(cases, records, strict=True)
    ]
    for item in guidance_cases:
        _write_json(output_dir / f"{item['source_id']}.json", item)
    guidance_path = output_dir / "guidance_cases.json"
    _write_json(guidance_path, {"cases": guidance_cases})
    status_counts = Counter(str(record.get("status") or "error") for record in records)
    summary = {
        "cases": len(guidance_cases),
        "nonempty_guidance": _count_nonempty_guidance(guidance_cases),
        "status_counts": dict(sorted(status_counts.items())),
        "workers": workers,
    }
    _write_json(output_dir / "settings.json", {
        "graph_state": str(graph_state_path),
        "cases": str(cases_path),
        "config": str(config_path),
        "database_description": str(database_description),
        "historical_examples": "per configs/cortex.yaml online_context.query_example_* settings",
    })
    _write_json(output_dir / "run_manifest.json", {
        "task": "BIRD dev online guidance for guided baseline",
        "database_id": database_id,
        **summary,
    })
    return summary


def _load_checkpoint_results(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    records = payload.get("results", [])
    if not isinstance(records, list):
        return {}
    loaded = {}
    for record in records:
        if isinstance(record, dict) and record.get("source_id"):
            loaded[str(record["source_id"])] = record
    return loaded


async def _online_case(
    worker: OnlineContextEvaluationWorker,
    *,
    graph_state,
    column_context,
    case: dict[str, Any],
    run_id: str | None = None,
) -> dict[str, Any]:
    question = _question_with_evidence(case)
    record: dict[str, Any] = {
        "source_id": case["source_id"],
        "database_id": case["database_id"],
        "question": case["question"],
        "simulated_user_decisions": [],
    }
    try:
        current = await worker.service.run(
            question=question,
            initial_sql=None,
            graph_state=graph_state,
            dialect="sqlite",
            column_context=column_context,
            **({"run_id": run_id} if run_id is not None else {}),
        )
        record["online_before_interaction"] = current.model_dump(mode="json")
        interaction_rounds = []
        prior_questions = []
        prior_answers = []
        while current.pending is not None:
            current_questions = tuple(current.interaction_questions)
            answers = list(
                await worker.simulator.answer_round(
                    question=question,
                    gold_sql=case["sql"],
                    interaction_questions=current_questions,
                    prior_questions=tuple(prior_questions),
                    prior_answers=tuple(prior_answers),
                    dialect="sqlite",
                )
            )
            round_decisions = []
            for decision in answers:
                serialized = decision.model_dump(mode="json")
                round_decisions.append(serialized)
                record["simulated_user_decisions"].append(serialized)
            prior_questions.extend(current_questions)
            prior_answers.extend(answers)
            interaction_rounds.append(
                {
                    "stage": current.pending.stage,
                    "questions": [
                        item.model_dump(mode="json") for item in current.interaction_questions
                    ],
                    "decisions": round_decisions,
                }
            )
            current = await worker.service.resume(
                graph_state=graph_state,
                pending=current.pending,
                answers=tuple(answers),
            )
        record["interaction_rounds"] = interaction_rounds
        record["online_after_interaction"] = current.model_dump(mode="json")
        record["status"] = current.status
        if current.downstream_guidance is not None:
            record["guidance_content"] = current.downstream_guidance.guidance_content
            record["downstream_guidance"] = current.downstream_guidance.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - one Case must not cancel the batch.
        detail = str(exc) if isinstance(exc, ValueError) else "online context Case failed"
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {detail}"
        record["error_chain"] = _sanitized_error_chain(exc)
    record["organizer_trace"] = list(worker.traces)
    return record


def _sanitized_error_chain(exc: BaseException) -> list[dict[str, str | int]]:
    """Keep useful retry diagnostics without provider payloads or credentials."""
    chain = []
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        entry: dict[str, str | int] = {"type": type(current).__name__}
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            entry["http_status"] = status
        message = str(current)
        if message.startswith(("LLM credential slot ", "Embedding credential failed ")):
            entry["sanitized_message"] = message
        chain.append(entry)
        current = current.__cause__
    return chain




def _guidance_case(case: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    guidance = str(record.get("guidance_content") or "").strip()
    handoff = dict(record.get("downstream_guidance") or {})
    handoff["guidance_content"] = guidance
    handoff["status"] = record.get("status")
    return {
        "source_id": case["source_id"],
        "database_id": case["database_id"],
        "question": case["question"],
        "status": record.get("status"),
        "guidance_content": guidance,
        "downstream_guidance": {"agent_handoff": handoff},
        "online_context": record,
    }


def _write_cases(database_id: str, dev_root: Path, output_root: Path) -> Path:
    source = dev_root / f"{database_id}.json"
    rows = json.loads(source.read_text(encoding="utf-8"))
    target = output_root / "inputs/dev.qa_sql.jsonl"
    output_rows = []
    for row in rows:
        source_id = str(row.get("question_id"))
        sql = row.get("SQL", row.get("query"))
        output_rows.append(
            {
                "source_id": source_id,
                "database_id": database_id,
                "question": row["question"],
                "evidence": row.get("evidence", ""),
                "sql": sql,
                "difficulty": row.get("difficulty", ""),
                "evaluation": {"row_order": "ignore"},
            }
        )
    _write_jsonl(target, output_rows)
    return target


def _write_baseline_config(
    path: Path,
    *,
    database_root: Path,
    test_set: Path,
    workspace_root: Path,
    guidance_path: Path,
) -> Path:
    payload = {
        "dataset": {"type": "bird"},
        "test_set_path": str(test_set.resolve()),
        "workspace_root": str(workspace_root.resolve()),
        "reference_time": "2026-08-21 00:00:00",
        "database": {"root": str(database_root.resolve())},
        "schema_linking": {
            "type": "simple",
            "table_top_k": 8,
            "column_top_k": 30,
            "value_top_k": 10,
            "values_per_column": 100,
        },
        "memory": {
            "type": "downstream_guidance",
            "source_path": str(guidance_path.resolve()),
        },
        "llm": {
            "model": "${CORTEX_LLM_MODEL}",
            "base_url": "${CORTEX_LLM_BASE_URL}",
            "api_key": "${CORTEX_LLM_API_KEY}",
            "temperature": 0,
            "timeout": 300,
        },
        "evaluation": {"top_k": 5, "sql_timeout": 30},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _find_graph_state(workspace_root: Path, database_id: str) -> Path:
    db_root = workspace_root / database_id
    candidates = [
        db_root / "merged_graph/graph_state.json",
        db_root / "merge/graph_state.json",
        db_root / "merge/merged_graph_state.json",
        db_root / "graph_state.json",
    ]
    candidates.extend(sorted(db_root.glob("v*/merge/graph_state.json"), reverse=True))
    candidates.extend(sorted(db_root.glob("merge/*/graph_state.json"), reverse=True))
    candidates.extend(sorted(db_root.glob("**/merged_graph*/graph_state.json"), reverse=True))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(f"{database_id}: graph_state.json not found under {db_root}")


def _run_guided_baseline(
    *, baseline_root: Path, config_path: Path, env_file: Path, final_root: Path
) -> dict[str, Any]:
    python = baseline_root / ".venv/bin/python"
    if not python.is_file():
        python = Path(os.environ.get("PYTHON", "python"))
    env = os.environ.copy()
    env["PYTHONPATH"] = str((baseline_root / "src").resolve())
    command_log_root = final_root / "command_logs"
    preflight = _run_json_command(
        [
            str(python),
            "-m",
            "nl2sql_baseline.cli",
            "preflight",
            "--config",
            str(config_path),
            "--env-file",
            str(env_file),
        ],
        cwd=baseline_root,
        env=env,
        log_path=command_log_root / "baseline_guidance_preflight.json",
    )
    report = _run_json_command(
        [
            str(python),
            "-m",
            "nl2sql_baseline.cli",
            "evaluate",
            "--config",
            str(config_path),
            "--env-file",
            str(env_file),
        ],
        cwd=baseline_root,
        env=env,
        log_path=command_log_root / "baseline_guidance_evaluate.json",
    )
    output = Path(report["output"])
    final_root.mkdir(parents=True, exist_ok=True)
    _copy_text(output / "predictions.jsonl", final_root / "predictions.jsonl")
    _copy_json(output / "report.json", final_root / "report.json")
    ea = {
        "database_id": config_path.parents[1].name,
        "predictions": str((final_root / "predictions.jsonl").resolve()),
        "report": str((final_root / "report.json").resolve()),
        "total": int(report.get("total", 0)),
        "correct": int(report.get("correct", 0)),
        "ea": report.get("ea"),
    }
    _write_json(final_root / "ea.json", ea)
    _write_json(final_root / "summary.json", {"preflight": preflight, "guided": report, "ea": ea})
    return {
        "config": str(config_path),
        "preflight": preflight,
        "guided": report,
        "ea": ea,
    }


def _run_json_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    _write_json(
        log_path,
        {
            "command": command,
            "cwd": str(cwd),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def _summarize_guidance(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases", [])
    return {
        "cases": len(cases),
        "nonempty_guidance": _count_nonempty_guidance(cases),
        "status_counts": dict(
            sorted(Counter(str(item.get("status") or "") for item in cases).items())
        ),
    }


def _count_nonempty_guidance(cases: list[dict[str, Any]]) -> int:
    return sum(
        bool(
            item.get("downstream_guidance", {})
            .get("agent_handoff", {})
            .get("guidance_content", "")
            .strip()
        )
        for item in cases
    )


def _aggregate(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0
    correct = 0
    rows = []
    for item in summaries:
        ea = item.get("final_results", {}).get("ea", {})
        db_total = int(ea.get("total", 0))
        db_correct = int(ea.get("correct", 0))
        total += db_total
        correct += db_correct
        rows.append(
            {
                "database_id": item["database_id"],
                "total": db_total,
                "correct": db_correct,
                "ea": ea.get("ea"),
            }
        )
    return {
        "databases": rows,
        "cases": total,
        "guided_correct": correct,
        "guided_ea": correct / total if total else None,
    }


def _guidance_complete(guidance_path: Path, cases_path: Path) -> bool:
    if not guidance_path.is_file():
        return False
    try:
        expected = {row["source_id"] for row in _read_jsonl(cases_path)}
        payload = json.loads(guidance_path.read_text(encoding="utf-8"))
        actual = {str(row.get("source_id")) for row in payload.get("cases", [])}
    except Exception:
        return False
    return expected == actual


def _baseline_complete(final_root: Path) -> bool:
    return (
        (final_root / "predictions.jsonl").is_file()
        and (final_root / "report.json").is_file()
        and (final_root / "ea.json").is_file()
    )


def _load_existing_baseline(final_root: Path, config_path: Path) -> dict[str, Any]:
    ea = json.loads((final_root / "ea.json").read_text(encoding="utf-8"))
    report = json.loads((final_root / "report.json").read_text(encoding="utf-8"))
    return {"config": str(config_path), "guided": report, "ea": ea, "resumed": True}


def _reserve_run_root(output_root: Path, run_name: str | None, *, resume: bool) -> Path:
    if run_name:
        path = output_root / run_name
        path.mkdir(parents=True, exist_ok=resume)
        return path
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    for suffix in range(1000):
        candidate = output_root / f"{stamp}-{suffix:03d}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"unable to reserve run root under {output_root}")


def _question_with_evidence(row: dict[str, Any]) -> str:
    question = str(row["question"]).strip()
    evidence = str(row.get("evidence") or "").strip()
    return f"{question}\nEvidence: {evidence}" if evidence else question


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _copy_text(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _copy_json(source: Path, target: Path) -> None:
    _write_json(target, json.loads(source.read_text(encoding="utf-8")))


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith(("#", ";")) or "=" not in text:
            continue
        if text.startswith("export "):
            text = text.removeprefix("export ").strip()
        key, value = text.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


if __name__ == "__main__":
    main()
