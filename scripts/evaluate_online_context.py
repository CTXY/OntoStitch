from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from cortex.bootstrap.online_context import (
    OnlineContextEvaluationWorker,
    build_online_context_evaluation_pool,
)
from cortex.infrastructure.online_context import (
    DatabaseDescriptionLoader,
    FileSystemGraphStateLoader,
)


async def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    graph_state = FileSystemGraphStateLoader().load(args.graph_state)
    pool = build_online_context_evaluation_pool(args.config)
    column_context = (
        DatabaseDescriptionLoader().load(args.database_description)
        if args.database_description
        else ()
    )
    cases = _read_jsonl(args.cases)
    if args.source_id:
        requested = set(args.source_id)
        cases = [case for case in cases if str(case.get("source_id")) in requested]
    results: list[dict[str, Any] | None] = [None] * len(cases)
    checkpoint_lock = asyncio.Lock()

    async def run_one(index: int, case: dict[str, Any]) -> None:
        async with pool.lease() as worker:
            record = await _evaluate_case(
                worker=worker,
                case=case,
                graph_state=graph_state,
                column_context=column_context,
                dialect=args.dialect,
            )
        async with checkpoint_lock:
            results[index] = record
            _write_json(
                Path(args.output),
                {"results": [item for item in results if item is not None]},
            )
        print(
            json.dumps(
                {
                    "source_id": record["source_id"],
                    "status": record.get("online_after_interaction", {}).get("status"),
                    "questions": len(
                        record.get("online_before_interaction", {}).get(
                            "interaction_questions", []
                        )
                    ),
                    "error": record.get("error"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    await asyncio.gather(*(run_one(index, case) for index, case in enumerate(cases)))
    completed = [item for item in results if item is not None]
    return {
        "graph_state": str(Path(args.graph_state).resolve()),
        "cases": str(Path(args.cases).resolve()),
        "gold_boundary": (
            "case.sql was supplied only to SimulatedUserAgent after system questions existed"
        ),
        "workers": pool.max_workers,
        "results": completed,
    }


async def _evaluate_case(
    *,
    worker: OnlineContextEvaluationWorker,
    case: dict[str, Any],
    graph_state,
    column_context,
    dialect: str,
) -> dict[str, Any]:
    source_id = str(case.get("source_id") or case.get("id") or "")
    question = str(case["question"])
    system_question = _question_with_evidence(case)
    # The dataset SQL is Gold and is deliberately excluded from Online Context.
    initial_sql = case.get("initial_sql")
    record: dict[str, Any] = {
        "source_id": source_id,
        "question": question,
        "initial_sql": initial_sql,
        "simulated_user_decisions": [],
    }
    try:
        first = await worker.service.run(
            question=system_question,
            initial_sql=initial_sql,
            graph_state=graph_state,
            dialect=dialect,
            column_context=column_context,
        )
        record["online_before_interaction"] = first.model_dump(mode="json")
        simulator_decisions = []
        current = first
        interaction_rounds = []
        prior_questions = []
        prior_answers = []
        while current.pending is not None:
            current_questions = tuple(current.interaction_questions)
            answers = list(
                await worker.simulator.answer_round(
                    question=system_question,
                    gold_sql=str(case["sql"]),
                    interaction_questions=current_questions,
                    prior_questions=tuple(prior_questions),
                    prior_answers=tuple(prior_answers),
                    dialect=dialect,
                )
            )
            round_decisions = []
            for decision in answers:
                serialized = decision.model_dump(mode="json")
                simulator_decisions.append(serialized)
                round_decisions.append(serialized)
                record["simulated_user_decisions"] = simulator_decisions
            prior_questions.extend(current_questions)
            prior_answers.extend(answers)
            interaction_rounds.append(
                {
                    "stage": current.pending.stage,
                    "questions": [
                        item.model_dump(mode="json")
                        for item in current.interaction_questions
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
    except Exception as exc:  # keep remaining Cases observable without provider details
        detail = str(exc) if isinstance(exc, ValueError) else "online context Case failed"
        record["error"] = f"{type(exc).__name__}: {detail}"
    record["organizer_trace"] = list(worker.traces)
    return record


def _question_with_evidence(case: dict[str, Any]) -> str:
    question = str(case["question"]).strip()
    evidence = str(case.get("evidence") or "").strip()
    return f"{question}\nEvidence: {evidence}" if evidence else question


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]




def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate online context with a Gold-SQL user")
    parser.add_argument("--graph-state", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", default="configs/cortex.yaml")
    parser.add_argument("--dialect", default="sqlite")
    parser.add_argument("--database-description")
    parser.add_argument("--source-id", action="append", help="evaluate only this source ID")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = asyncio.run(evaluate(args))
    output = Path(args.output)
    _write_json(output, payload)
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
