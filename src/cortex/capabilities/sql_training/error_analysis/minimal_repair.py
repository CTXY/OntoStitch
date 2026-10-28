from __future__ import annotations

from itertools import combinations
from typing import Any

import sqlglot
from sqlglot import expressions

from cortex.capabilities.sql_training.error_analysis.agent import (
    ErrorAnalysisProvider,
    extract_error_analysis_minimal_repair_ablation,
    extract_error_analysis_minimal_repair_action,
    extract_error_analysis_minimal_repair_joint_ablation,
    extract_error_analysis_minimal_repair_review,
)
from cortex.capabilities.sql_training.error_analysis.models import (
    ErrorAnalysisReport,
    ErrorCandidate,
    RepairAblation,
    RepairAction,
    RepairJointAblation,
    RepairNecessity,
    RepairReview,
    RepairRound,
    RepairSession,
    RepairType,
)
from cortex.capabilities.sql_training.evaluation import QueryResult, evaluate
from cortex.infrastructure.database.execution import execute_query


def run_minimal_repair(
    provider: ErrorAnalysisProvider,
    *,
    question: str,
    sql_gt: str,
    sql_ic: str,
    final: dict[str, Any],
    database_url: str,
    timeout: float,
    max_repair_rounds: int,
    max_invalid_repair_rounds: int,
    max_joint_repair_checks: int,
    max_joint_repair_group_size: int,
    trace: Any,
    working_assets: list[dict[str, Any]] | None = None,
    schema_context: dict[str, Any] | None = None,
    evidence: str | None = None,
    gold_column_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace.write("minimal_repair_start", final_comparison=final)

    if not final.get("gold_executable") or not isinstance(final.get("gold_result"), dict):
        status = "oracle_unavailable"
        trace.write(
            "minimal_repair_complete",
            status=status,
            completeness_status=status,
            error_candidate_count=0,
        )
        return _report(
            status=status,
            summary="Gold SQL result is unavailable, so minimal repair has no oracle.",
            final=final,
            original_sql=sql_ic,
            final_sql=sql_ic,
            rounds=[],
            ablations=[],
            joint_ablations=[],
            review=None,
            candidates=[],
        )

    if final.get("result_equal") is True:
        status = "complete_equal_result"
        trace.write(
            "minimal_repair_complete",
            status=status,
            completeness_status=status,
            error_candidate_count=0,
        )
        return _report(
            status=status,
            summary="Original SQL already matches the gold SQL result.",
            final=final,
            original_sql=sql_ic,
            final_sql=sql_ic,
            rounds=[],
            ablations=[],
            joint_ablations=[],
            review=None,
            candidates=[],
        )

    gold_result = final["gold_result"]
    current_sql = sql_ic
    current_result = final.get("predicted_result")
    current_error = str(final.get("predicted_error") or "")
    rounds: list[RepairRound] = []
    consecutive_invalid = 0
    terminal_status = "budget_exhausted"
    final_result: QueryResult | None = current_result if isinstance(current_result, dict) else None
    final_error = current_error

    for round_index in range(1, max_repair_rounds + 1):
        payload = {
            "question": question,
            "evidence": evidence,
            "gold_sql": sql_gt,
            "gold_column_metadata": dict(gold_column_metadata or {}),
            "original_incorrect_sql": sql_ic,
            "current_sql": current_sql,
            "evaluation_contract": final.get("evaluation_contract") or {},
            "gold_result": gold_result,
            "current_result": current_result if isinstance(current_result, dict) else None,
            "current_error": current_error,
            "repair_rounds": [item.model_dump(mode="json") for item in rounds],
            "final_comparison": final,
        }
        action, action_error = _extract_repair_action_with_retries(
            provider,
            payload=payload,
            round_index=round_index,
            trace=trace,
        )
        if action is None:
            terminal_status = "analysis_incomplete"
            final_error = action_error
            break

        trace.write(
            "minimal_repair_action",
            round=round_index,
            output=action.model_dump(mode="json"),
        )
        if action.action == "finish":
            terminal_status = "analysis_incomplete"
            final_error = action.finish_reason
            break

        execution_payload = _execute_repair_action(
            action,
            round_index=round_index,
            input_sql=current_sql,
            sql_gt=sql_gt,
            gold_result=gold_result,
            database_url=database_url,
            timeout=timeout,
        )
        repair_round = execution_payload["round"]
        rounds.append(repair_round)
        trace.write(
            "minimal_repair_execution",
            round=round_index,
            execution=repair_round.model_dump(mode="json"),
        )

        current_sql = action.repaired_sql
        current_result = execution_payload["result"]
        current_error = repair_round.execution_error
        final_result = current_result if isinstance(current_result, dict) else None
        final_error = current_error

        if not repair_round.execution_success:
            consecutive_invalid += 1
            if consecutive_invalid >= max_invalid_repair_rounds:
                terminal_status = "repair_invalid"
                break
            continue

        consecutive_invalid = 0
        if repair_round.result_equal is True:
            terminal_status = "matched"
            break

    ablations = _run_backward_ablations(
        provider,
        question=question,
        sql_gt=sql_gt,
        final_sql=current_sql,
        gold_result=gold_result,
        rounds=rounds,
        database_url=database_url,
        timeout=timeout,
        trace=trace,
    ) if terminal_status == "matched" else []
    joint_ablations = (
        _run_joint_ablations(
            provider,
            question=question,
            sql_gt=sql_gt,
            final_sql=current_sql,
            gold_result=gold_result,
            rounds=rounds,
            ablations=ablations,
            database_url=database_url,
            timeout=timeout,
            max_checks=max_joint_repair_checks,
            max_group_size=max_joint_repair_group_size,
            trace=trace,
        )
        if terminal_status == "matched"
        else []
    )

    review, review_error = _review_session(
        provider,
        question=question,
        sql_gt=sql_gt,
        sql_ic=sql_ic,
        final_sql=current_sql,
        gold_result=gold_result,
        final_result=final_result,
        final_error=final_error,
        rounds=rounds,
        ablations=ablations,
        joint_ablations=joint_ablations,
        matched=terminal_status == "matched",
        terminal_status=terminal_status,
        working_assets=working_assets,
        schema_context=schema_context,
        evidence=evidence,
        gold_column_metadata=gold_column_metadata,
        evaluation_contract=final.get("evaluation_contract") or {},
        trace=trace,
    )
    if review_error:
        terminal_status = "analysis_incomplete"

    candidates = _error_candidates_from_review(review)
    completeness_status = _completeness_status(terminal_status, review_error=review_error)
    trace.write(
        "minimal_repair_complete",
        status=terminal_status,
        completeness_status=completeness_status,
        error_candidate_count=len(candidates),
    )
    return _report(
        status=terminal_status,
        summary=review.summary if review is not None else "",
        final=final,
        original_sql=sql_ic,
        final_sql=current_sql,
        rounds=rounds,
        ablations=ablations,
        joint_ablations=joint_ablations,
        review=review,
        candidates=candidates,
        completeness_status=completeness_status,
        completeness_reason=review_error or (
            final_error if terminal_status == "analysis_incomplete" else ""
        ),
    )


def _extract_repair_action_with_retries(
    provider: ErrorAnalysisProvider,
    *,
    payload: dict[str, Any],
    round_index: int,
    trace: Any,
    max_attempts: int = 3,
) -> tuple[RepairAction | None, str]:
    """Retry malformed action responses without spending a semantic repair round."""

    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        request = dict(payload)
        if errors:
            request["validation_feedback"] = {
                "attempt": attempt,
                "error": errors[-1],
                "instruction": (
                    "Return exactly the requested output Schema and correct the reported "
                    "validation error without changing evidenced facts. Do not preserve a "
                    "repair classification or decision that the validator rejected. A "
                    "representation repair may change only output shape over the same physical "
                    "inputs. If the attempted repair also changed physical columns, return only "
                    "the semantic binding repair while preserving the current output shape; a "
                    "later repair round will handle representation separately. Conversely, a "
                    "semantic binding repair must keep the current projection shape and number "
                    "of output expressions. Do not add another result wrapper."
                ),
            }
        try:
            action = extract_error_analysis_minimal_repair_action(provider, request)
            _validate_repair_action_changes_sql(
                action,
                input_sql=str(payload.get("current_sql") or ""),
            )
            _validate_representation_action(
                action,
                input_sql=str(payload.get("current_sql") or ""),
            )
            return action, ""
        except Exception as exc:  # noqa: BLE001 - retry typed LLM contract failures
            errors.append(str(exc))
            trace.write(
                "minimal_repair_action",
                round=round_index,
                attempt=attempt,
                error=str(exc),
            )
    return None, errors[-1]


def _validate_repair_action_changes_sql(action: RepairAction, *, input_sql: str) -> None:
    """Reject repair actions that do not actually edit the current query."""

    if action.action == "repair" and action.repaired_sql.strip() == input_sql.strip():
        raise ValueError("repair action is a no-op; repaired_sql must change the current SQL")


def _validate_representation_action(action: RepairAction, *, input_sql: str) -> None:
    """Keep result representation edits separate from physical binding edits."""

    before = _qualified_column_names(input_sql)
    after = _qualified_column_names(action.repaired_sql)
    if action.repair_type == RepairType.REPRESENTATION and before != after:
        raise ValueError(
            "representation repair changed underlying physical columns; repair the semantic "
            f"binding separately first: before={sorted(before)}, after={sorted(after)}"
        )
    if (
        action.repair_type == RepairType.SEMANTIC
        and before != after
        and _projection_arity(input_sql) != _projection_arity(action.repaired_sql)
    ):
        raise ValueError(
            "semantic binding repair also changed output representation; preserve the current "
            "projection shape and output-expression count, then repair representation in a "
            "later round"
        )


def _qualified_column_names(sql: str) -> set[tuple[str, str]]:
    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except Exception as exc:
        raise ValueError(f"could not inspect repair SQL columns: {exc}") from exc
    if tree is None:
        raise ValueError("could not inspect repair SQL columns")
    return {
        (column.table.casefold(), column.name.casefold())
        for column in tree.find_all(expressions.Column)
        if column.table and column.name
    }


def _projection_arity(sql: str) -> int | None:
    try:
        parsed = sqlglot.parse_one(sql)
    except Exception:  # noqa: BLE001 - validation remains conservative on unparsable SQL
        return None
    select = parsed.find(expressions.Select)
    return len(select.expressions) if select is not None else None


def _execute_repair_action(
    action: RepairAction,
    *,
    round_index: int,
    input_sql: str,
    sql_gt: str,
    gold_result: QueryResult,
    database_url: str,
    timeout: float,
) -> dict[str, Any]:
    executed = execute_query(action.repaired_sql, database_url=database_url, timeout=timeout)
    if not executed.success or executed.result is None:
        return {
            "round": RepairRound(
                round_index=round_index,
                edit_key=action.edit_key,
                edit_summary=action.edit_summary,
                changed_clause=action.changed_clause,
                repair_type=action.repair_type,
                predicted_logic_before=action.predicted_logic_before,
                gold_logic_after=action.gold_logic_after,
                input_sql=input_sql,
                repaired_sql=action.repaired_sql,
                execution_success=False,
                result_equal=None,
                evaluation_reason="",
                execution_error=str(executed.error or "candidate SQL failed"),
            ),
            "result": None,
        }

    evaluation = evaluate(sql_gt, executed.result, gold_result)
    return {
        "round": RepairRound(
            round_index=round_index,
            edit_key=action.edit_key,
            edit_summary=action.edit_summary,
            changed_clause=action.changed_clause,
            repair_type=action.repair_type,
            predicted_logic_before=action.predicted_logic_before,
            gold_logic_after=action.gold_logic_after,
            input_sql=input_sql,
            repaired_sql=action.repaired_sql,
            execution_success=True,
            result_equal=evaluation.get("exec") == 1,
            evaluation_reason=str(evaluation.get("reason") or ""),
            execution_error="",
        ),
        "result": executed.result,
    }


def _run_backward_ablations(
    provider: ErrorAnalysisProvider,
    *,
    question: str,
    sql_gt: str,
    final_sql: str,
    gold_result: QueryResult,
    rounds: list[RepairRound],
    database_url: str,
    timeout: float,
    trace: Any,
) -> list[RepairAblation]:
    ablations: list[RepairAblation] = []
    successful_rounds = [repair_round for repair_round in rounds if repair_round.execution_success]
    if len(successful_rounds) == 1:
        ablation = _single_round_necessary_ablation(successful_rounds[0])
        trace.write(
            "minimal_repair_ablation",
            round=ablation.round_index,
            ablation=ablation.model_dump(mode="json"),
        )
        return [ablation]

    for repair_round in successful_rounds:
        ablation = _run_backward_ablation(
            provider,
            question=question,
            sql_gt=sql_gt,
            final_sql=final_sql,
            gold_result=gold_result,
            repair_round=repair_round,
            database_url=database_url,
            timeout=timeout,
        )
        ablations.append(ablation)
        trace.write(
            "minimal_repair_ablation",
            round=repair_round.round_index,
            ablation=ablation.model_dump(mode="json"),
        )
    return ablations


def _run_backward_ablation(
    provider: ErrorAnalysisProvider,
    *,
    question: str,
    sql_gt: str,
    final_sql: str,
    gold_result: QueryResult,
    repair_round: RepairRound,
    database_url: str,
    timeout: float,
) -> RepairAblation:
    payload = {
        "question": question,
        "gold_sql": sql_gt,
        "final_sql": final_sql,
        "target_round": repair_round.model_dump(mode="json"),
    }
    last_invalid: RepairAblation | None = None
    for _attempt in range(2):
        if last_invalid is not None:
            payload["ablation_feedback"] = {
                "previous_rationale": last_invalid.rationale,
                "execution_error": last_invalid.execution_error,
                "instruction": (
                    "Retry in the final SQL's current structure. Reverse the target semantic "
                    "effect wherever it now appears; do not restore the target round's old query "
                    "structure."
                ),
            }
        try:
            proposal = extract_error_analysis_minimal_repair_ablation(provider, payload)
            ablation = _execute_ablation(
                proposal.ablated_sql,
                rationale=proposal.rationale,
                repair_round=repair_round,
                sql_gt=sql_gt,
                gold_result=gold_result,
                database_url=database_url,
                timeout=timeout,
            )
        except Exception as exc:
            ablation = RepairAblation(
                round_index=repair_round.round_index,
                repair_type=repair_round.repair_type,
                ablated_sql="",
                execution_success=False,
                result_equal=None,
                necessity=RepairNecessity.INVALID_ABLATION,
                rationale="",
                execution_error=str(exc),
                evaluation_reason="",
            )
        if ablation.execution_success:
            return ablation
        last_invalid = ablation
    assert last_invalid is not None
    return last_invalid


def _single_round_necessary_ablation(repair_round: RepairRound) -> RepairAblation:
    reason = (
        "single successful repair round; necessity is implied because final SQL matched only "
        "after this repair"
    )
    return RepairAblation(
        round_index=repair_round.round_index,
        repair_type=repair_round.repair_type,
        ablated_sql="",
        execution_success=True,
        result_equal=False,
        necessity=RepairNecessity.NECESSARY,
        rationale=reason,
        execution_error="",
        evaluation_reason=reason,
    )


def _execute_ablation(
    ablated_sql: str,
    *,
    rationale: str,
    repair_round: RepairRound,
    sql_gt: str,
    gold_result: QueryResult,
    database_url: str,
    timeout: float,
) -> RepairAblation:
    if not ablated_sql.strip():
        return RepairAblation(
            round_index=repair_round.round_index,
            repair_type=repair_round.repair_type,
            ablated_sql=ablated_sql,
            execution_success=False,
            result_equal=None,
            necessity=RepairNecessity.INVALID_ABLATION,
            rationale=rationale,
            execution_error="ablation SQL is empty",
            evaluation_reason="",
        )

    executed = execute_query(ablated_sql, database_url=database_url, timeout=timeout)
    if not executed.success or executed.result is None:
        return RepairAblation(
            round_index=repair_round.round_index,
            repair_type=repair_round.repair_type,
            ablated_sql=ablated_sql,
            execution_success=False,
            result_equal=None,
            necessity=RepairNecessity.INVALID_ABLATION,
            rationale=rationale,
            execution_error=str(executed.error or "ablation SQL failed"),
            evaluation_reason="",
        )

    evaluation = evaluate(sql_gt, executed.result, gold_result)
    result_equal = evaluation.get("exec") == 1
    return RepairAblation(
        round_index=repair_round.round_index,
        repair_type=repair_round.repair_type,
        ablated_sql=ablated_sql,
        execution_success=True,
        result_equal=result_equal,
        necessity=(
            RepairNecessity.NEED_REVIEW if result_equal else RepairNecessity.NECESSARY
        ),
        rationale=rationale,
        execution_error="",
        evaluation_reason=str(evaluation.get("reason") or ""),
    )


def _run_joint_ablations(
    provider: ErrorAnalysisProvider,
    *,
    question: str,
    sql_gt: str,
    final_sql: str,
    gold_result: QueryResult,
    rounds: list[RepairRound],
    ablations: list[RepairAblation],
    database_url: str,
    timeout: float,
    max_checks: int,
    max_group_size: int,
    trace: Any,
) -> list[RepairJointAblation]:
    successful_round_count = len([item for item in rounds if item.execution_success])
    if (
        successful_round_count < 2
        or len(ablations) != successful_round_count
        or any(not item.execution_success or item.result_equal is not True for item in ablations)
    ):
        return []
    unresolved_indexes = {
        item.round_index
        for item in ablations
        if item.execution_success and item.result_equal is True
    }
    unresolved_rounds = [
        item
        for item in rounds
        if item.execution_success and item.round_index in unresolved_indexes
    ]
    if len(unresolved_rounds) < 2:
        return []

    results: list[RepairJointAblation] = []
    group_limit = min(max_group_size, len(unresolved_rounds))
    groups = (
        group
        for size in range(2, group_limit + 1)
        for group in combinations(unresolved_rounds, size)
    )
    for group in groups:
        if len(results) >= max_checks:
            break
        round_indexes = [item.round_index for item in group]
        try:
            proposal = extract_error_analysis_minimal_repair_joint_ablation(
                provider,
                {
                    "question": question,
                    "gold_sql": sql_gt,
                    "final_sql": final_sql,
                    "target_rounds": [item.model_dump(mode="json") for item in group],
                },
            )
            result = _execute_joint_ablation(
                proposal.ablated_sql,
                rationale=proposal.rationale,
                round_indexes=round_indexes,
                sql_gt=sql_gt,
                gold_result=gold_result,
                database_url=database_url,
                timeout=timeout,
            )
        except Exception as exc:
            result = RepairJointAblation(
                round_indexes=round_indexes,
                execution_success=False,
                result_equal=None,
                necessity=RepairNecessity.INVALID_ABLATION,
                execution_error=str(exc),
            )
        results.append(result)
        trace.write(
            "minimal_repair_joint_ablation",
            rounds=round_indexes,
            ablation=result.model_dump(mode="json"),
        )
    return results


def _execute_joint_ablation(
    ablated_sql: str,
    *,
    rationale: str,
    round_indexes: list[int],
    sql_gt: str,
    gold_result: QueryResult,
    database_url: str,
    timeout: float,
) -> RepairJointAblation:
    if not ablated_sql.strip():
        return RepairJointAblation(
            round_indexes=round_indexes,
            ablated_sql=ablated_sql,
            execution_success=False,
            result_equal=None,
            necessity=RepairNecessity.INVALID_ABLATION,
            rationale=rationale,
            execution_error="joint ablation SQL is empty",
        )
    executed = execute_query(ablated_sql, database_url=database_url, timeout=timeout)
    if not executed.success or executed.result is None:
        return RepairJointAblation(
            round_indexes=round_indexes,
            ablated_sql=ablated_sql,
            execution_success=False,
            result_equal=None,
            necessity=RepairNecessity.INVALID_ABLATION,
            rationale=rationale,
            execution_error=str(executed.error or "joint ablation SQL failed"),
        )
    evaluation = evaluate(sql_gt, executed.result, gold_result)
    result_equal = evaluation.get("exec") == 1
    return RepairJointAblation(
        round_indexes=round_indexes,
        ablated_sql=ablated_sql,
        execution_success=True,
        result_equal=result_equal,
        necessity=(
            RepairNecessity.NEED_REVIEW if result_equal else RepairNecessity.NECESSARY
        ),
        rationale=rationale,
        evaluation_reason=str(evaluation.get("reason") or ""),
    )


def _review_session(
    provider: ErrorAnalysisProvider,
    *,
    question: str,
    sql_gt: str,
    sql_ic: str,
    final_sql: str,
    gold_result: QueryResult,
    final_result: QueryResult | None,
    final_error: str,
    rounds: list[RepairRound],
    ablations: list[RepairAblation],
    joint_ablations: list[RepairJointAblation],
    matched: bool,
    terminal_status: str,
    working_assets: list[dict[str, Any]] | None,
    schema_context: dict[str, Any] | None,
    evidence: str | None,
    gold_column_metadata: dict[str, Any] | None,
    evaluation_contract: dict[str, Any],
    trace: Any,
) -> tuple[RepairReview | None, str]:
    payload = {
        "question": question,
        "gold_sql": sql_gt,
        "original_incorrect_sql": sql_ic,
        "final_sql": final_sql,
        "evaluation_contract": evaluation_contract,
        "gold_result": gold_result,
        "final_result": final_result,
        "final_error": final_error,
        "repair_rounds": [item.model_dump(mode="json") for item in rounds],
        "ablations": [item.model_dump(mode="json") for item in ablations],
        "joint_ablations": [item.model_dump(mode="json") for item in joint_ablations],
        "matched": matched,
        "terminal_status": terminal_status,
        "accepted_working_assets": _canonical_working_assets(working_assets or []),
        "schema_context": _canonical_schema_context(schema_context or {}),
        "evidence": evidence,
        "gold_column_metadata": dict(gold_column_metadata or {}),
    }
    errors: list[str] = []
    review: RepairReview | None = None
    retry_required_types: set[RepairType] = set()
    for attempt in range(1, 4):
        request = dict(payload)
        if errors:
            request["validation_feedback"] = {
                "error": errors[-1],
                "instruction": (
                    "Correct the reported validation error without changing evidenced facts.\n"
                    "- Retain every supplied error point and partition its ID exactly once.\n"
                    "- Assign one owner from the complete term or independent request/result "
                    "operation, not from a separately executable SQL helper. An unnamed input "
                    "construction required only by a named Metric remains Metric-owned. Split "
                    "only when one supplied point crosses owner boundaries.\n"
                    "- Form groups after owners are frozen; merge only an indivisible shared "
                    "identity whose strict subsets are not complete obligations.\n"
                    "- Write the owner-scoped root cause and why the repair restores the first "
                    "owner-complete result before inspecting Working. Do not cite another group "
                    "as preserved behavior. Use Working only to decide exact same-identity "
                    "UPDATE versus new-asset creation."
                ),
            }
        try:
            review = extract_error_analysis_minimal_repair_review(provider, request)
            _validate_review_repair_round_coverage(
                review,
                ablations,
                joint_ablations,
            )
            _validate_review_retry_coverage(review, retry_required_types)
            _validate_review_working_bases(
                review,
                working_assets or [],
                question=question,
            )
            break
        except Exception as exc:  # noqa: BLE001 - retry typed LLM contract failures
            if review is not None and "leaks training evidence" in str(exc):
                retry_required_types.update(
                    item.repair_type for item in review.confirmed_errors
                )
            errors.append(str(exc))
            trace.write("minimal_repair_review", attempt=attempt, error=str(exc))
            review = None
    if review is None:
        return None, errors[-1]

    trace.write("minimal_repair_review", output=review.model_dump(mode="json"))
    return review, ""


def _validate_review_repair_round_coverage(
    review: RepairReview,
    ablations: list[RepairAblation],
    joint_ablations: list[RepairJointAblation] | None = None,
) -> None:
    required_rounds = {
        item.round_index
        for item in ablations
        if item.necessity == RepairNecessity.NECESSARY
    }
    required_rounds.update(
        round_index
        for item in joint_ablations or []
        if item.necessity == RepairNecessity.NECESSARY
        for round_index in item.round_indexes
    )
    covered_rounds = {
        round_index
        for error in review.confirmed_errors
        for round_index in error.source_rounds
    }
    missing = sorted(required_rounds - covered_rounds)
    if missing:
        raise ValueError(
            "review dropped repair rounds proved necessary by ablation evidence: "
            f"{missing}"
        )


def _validate_review_retry_coverage(
    review: RepairReview,
    required_types: set[RepairType],
) -> None:
    actual_types = {item.repair_type for item in review.confirmed_errors}
    if not required_types.issubset(actual_types):
        missing = sorted(item.value for item in required_types - actual_types)
        raise ValueError(
            "review dropped evidenced error types while correcting instruction wording: "
            f"{missing}"
        )


def _canonical_working_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the accepted catalog deterministic before prompt formatting."""

    normalized = [dict(asset) for asset in assets if isinstance(asset, dict)]
    return sorted(
        normalized,
        key=lambda asset: (str(asset.get("node_type") or ""), str(asset.get("id") or "")),
    )


def _canonical_schema_context(schema_context: dict[str, Any]) -> dict[str, Any]:
    return {str(key): schema_context[key] for key in sorted(schema_context)}


def _validate_review_working_bases(
    review: RepairReview,
    working_assets: list[dict[str, Any]],
    *,
    question: str = "",
) -> None:
    """Reject review output that cannot be compiled against the accepted Working ontology."""

    assets_by_id = {
        str(asset.get("id")): asset
        for asset in working_assets
        if isinstance(asset, dict) and str(asset.get("id") or "").strip()
    }
    errors_by_id = {error.id: error for error in review.confirmed_errors}
    del question
    for group in review.repair_groups:
        group_errors = [errors_by_id[error_id] for error_id in group.error_ids]
        if any(error.repair_type == RepairType.REPRESENTATION for error in group_errors):
            normalized_instruction = group.repair_instruction.casefold()
            leaked_sources = [
                marker
                for marker in ("evaluation contract", "optional evidence", "gold sql")
                if marker in normalized_instruction
            ]
            if leaked_sources:
                raise ValueError(
                    f"representation repair group {group.id} leaks training evidence into its "
                    f"instruction: {leaked_sources}"
                )
        if group.base_asset_id is None:
            continue
        asset = assets_by_id.get(group.base_asset_id)
        if asset is None:
            raise ValueError(
                f"repair group {group.id} base_asset_id is not an accepted Working asset: "
                f"{group.base_asset_id}"
            )
        expected_type = (
            "concept" if group.owner.value == "semantic_mapping" else group.owner.value
        )
        if str(asset.get("node_type") or "") != expected_type:
            raise ValueError(
                f"repair group {group.id} base_asset_id type does not match owner: "
                f"{group.base_asset_id}"
            )

def _error_candidates_from_review(
    review: RepairReview | None,
) -> list[ErrorCandidate]:
    if review is None:
        return []

    candidates: list[ErrorCandidate] = []
    for edit in review.confirmed_errors:
        try:
            candidates.append(
                ErrorCandidate.model_validate(
                    {
                        "id": edit.id,
                        "error_key": edit.error_key,
                        "difference_point": edit.difference_point,
                        "gold_logic": edit.gold_logic,
                        "predicted_logic": edit.predicted_logic,
                        "repair_type": edit.repair_type,
                        "necessity": RepairNecessity.NECESSARY,
                    }
                )
            )
        except Exception:
            continue
    return candidates


def _completeness_status(status: str, *, review_error: str = "") -> str:
    if review_error:
        return "analysis_incomplete"
    if status == "matched":
        return "complete"
    return status


def _report(
    *,
    status: str,
    summary: str,
    final: dict[str, Any],
    original_sql: str,
    final_sql: str,
    rounds: list[RepairRound],
    ablations: list[RepairAblation],
    joint_ablations: list[RepairJointAblation],
    review: RepairReview | None,
    candidates: list[ErrorCandidate],
    completeness_status: str | None = None,
    completeness_reason: str = "",
) -> dict[str, Any]:
    outward_status = completeness_status or _completeness_status(status)
    session = RepairSession(
        status=status,
        original_sql=original_sql,
        final_sql=final_sql,
        rounds=rounds,
        ablations=ablations,
        joint_ablations=joint_ablations,
        review=review,
    )
    return ErrorAnalysisReport(
        mode="minimal_repair",
        summary=summary,
        final_comparison=final,
        points=[],
        confirmed_errors=_error_candidates_from_review(review),
        error_candidates=candidates,
        repair_groups=(list(review.repair_groups) if review is not None else []),
        latent_risk_points=[
            candidate.model_dump(mode="json")
            for candidate in candidates
            if candidate.necessity == RepairNecessity.LATENT_RISK
        ],
        completeness={
            "status": outward_status,
            "reason": completeness_reason,
            "repair_round_count": len(rounds),
            "ablation_count": len(ablations),
            "joint_ablation_count": len(joint_ablations),
        },
        repair_session=session,
    ).model_dump(mode="json")
