"""Audit the retained eleven-database guided results, without model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
NINE = OUT / "bird_unified_nine_20261120_1700"
NEW = {
    "thrombosis_prediction": OUT / "online_json_fix_20261123_195329",
    "toxicology": OUT / "toxicology_guided_recovery_20261124_001002",
}
USER_SOURCES = {"user_choice", "user_explanation"}


def read(p):
    return json.loads(p.read_text())


def rows(p):
    return [json.loads(s) for s in p.read_text().splitlines() if s.strip()]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def jsonl(p, values):
    p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in values))


def stats(db, cases):
    exact = [r["questions_actual"] for r in cases if r["questions_actual"] is not None]
    resolved = [r["questions_resolved"] for r in cases if r["questions_resolved"] is not None]
    correct = sum(r["guided_exec"] == 1 for r in cases)
    return {
        "database_id": db, "test_cases": len(cases), "guided_correct": correct,
        "guided_ea": correct / len(cases),
        "guided_missing": sum(not r["guided_prediction_available"] for r in cases),
        "guided_errors": sum(bool(r["guided_error"]) for r in cases),
        "actual_question_total": sum(exact), "actual_question_cases": len(exact),
        "actual_questions_per_observed_case": sum(exact) / len(exact) if exact else None,
        "actual_question_unknown_cases": len(cases) - len(exact),
        "resolved_question_total": sum(resolved), "resolved_question_cases": len(resolved),
        "resolved_questions_per_observed_case": sum(resolved) / len(resolved) if resolved else None,
        "resolved_question_unknown_cases": len(cases) - len(resolved),
        "full_set_actual_question_mean": sum(exact) / len(cases) if len(exact) == len(cases) else None,
        "retained_question_lower_bound_per_all_case": sum(resolved) / len(cases),
        "question_count_basis": dict(Counter(r["questions_basis"] for r in cases)),
    }


def compile_report(target):
    source_hashes = {}

    def watch(p):
        source_hashes[str(p.resolve())] = sha(p)
        return p

    target.mkdir(parents=True, exist_ok=False)
    old = rows(watch(NINE / "test_results_processed.jsonl"))
    old_summary = read(watch(NINE / "summary.json"))
    result = []
    for row in old:
        q = row["interaction"]
        pred = row["guided"]
        result.append({
            "database_id": row["database_id"], "source_id": row["source_id"],
            "guided_exec": (pred or {}).get("evaluation", {}).get("exec"),
            "guided_prediction_available": pred is not None, "guided_error": (pred or {}).get("error"),
            "questions_actual": q["asked_count"], "questions_resolved": q["resolved_user_questions"],
            "questions_basis": q["count_basis"], "online_source": q.get("log_source"),
            "result_source": str(NINE / "test_results_processed.jsonl"),
            "guided_prediction_source": row.get("guided_source"),
        })
    assert len(result) == old_summary["totals"]["test_cases"] == 612
    assert sum(r["guided_exec"] == 1 for r in result) == old_summary["totals"]["guided_processed_correct"] == 482
    assert sum(r["questions_resolved"] or 0 for r in result) == 711

    for db, root in NEW.items():
        source = read(watch(root / "source.json"))
        parent = Path(source["parent"])
        final = read(watch(root / "summary.json"))
        predictions = rows(watch(root / "predictions.jsonl"))
        assert final["status"] == "complete"
        assert len(predictions) == final["arms"]["guided"]["total"]
        expected = {str(c["source_id"]) for c in rows(watch(parent / "test_cases.jsonl"))}
        assert {str(p["source_id"]) for p in predictions} == expected
        for row in predictions:
            sid = str(row["source_id"])
            replacement = root / "cases" / sid / "guided_prediction.json"
            provenance = (row.get("result_sources") or {}).get("guided") or {}
            if replacement.exists():
                prediction_file = replacement
            elif provenance.get("path"):
                prediction_file = Path(provenance["path"])
            else:
                prediction_file = parent / "cases" / sid / "guided_prediction.json"
            pred = read(watch(prediction_file))
            assert pred == row["guided"], (db, sid, "prediction provenance mismatch")
            assert pred["database_id"] == db and str(pred["source_id"]) == sid
            online_file = prediction_file.parent / "online.json"
            online = read(watch(online_file))
            after = online.get("online_after_interaction") or {}
            down = online.get("downstream_guidance") or after.get("downstream_guidance") or {}
            down = down.get("agent_handoff", down)
            guidance = down.get("interaction_guidance", after.get("interaction_guidance", []))
            answered = [g for g in guidance if g.get("source") in USER_SOURCES]
            resolved = len(answered)
            assert online["status"] != "error", (db, sid)
            assert resolved == len({g["choice_point_id"] for g in answered}), (db, sid, "duplicate question IDs")
            rounds = online.get("interaction_rounds")
            round_count = sum(len(r.get("questions", [])) for r in rounds or [])
            cumulative = (after.get("diagnostics") or {}).get("presented_questions")
            if cumulative is not None:
                assert cumulative >= round_count
                assert cumulative == resolved, (db, sid, "cumulative question count differs from answered guidance")
                actual = cumulative
                basis = "completed_checkpoint_counter" if cumulative != round_count or rounds is None else "interaction_log_and_counter"
            elif isinstance(rounds, list) and round_count == resolved:
                actual, basis = round_count, "completed_interaction_log"
            else:
                actual, basis = None, "resolved_guidance_only"
            result.append({
                "database_id": db, "source_id": sid,
                "guided_exec": pred["evaluation"]["exec"], "guided_prediction_available": True,
                "guided_error": pred.get("error"), "questions_actual": actual,
                "questions_resolved": resolved, "questions_basis": basis,
                "recorded_round_questions": round_count if rounds is not None else None,
                "completed_checkpoint_question_count": cumulative,
                "online_source": str(online_file.resolve()),
                "guided_prediction_source": str(prediction_file.resolve()),
                "result_source": str(root / "predictions.jsonl"),
                "confirmed_questions": [{"question_id": a["choice_point_id"], "question": a["decision_question"],
                                         "answer_source": a["source"]} for a in answered],
            })
        group = [r for r in result if r["database_id"] == db]
        assert sum(r["guided_exec"] == 1 for r in group) == final["arms"]["guided"]["correct"]

    keys = {(r["database_id"], r["source_id"]) for r in result}
    assert len(keys) == len(result) == 765
    dbs = list(dict.fromkeys(r["database_id"] for r in result))
    per_db = [stats(db, [r for r in result if r["database_id"] == db]) for db in dbs]
    total = stats("ALL", result)
    assert total["guided_correct"] == 597
    assert total["resolved_question_cases"] == 749 and total["resolved_question_total"] == 886
    assert total["actual_question_cases"] == 362 and total["actual_question_total"] == 332
    summary = {
        "date": "2026-11-24", "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "scope": "Project BIRD held-out test split: 11 databases, 765 cases; not the full official 1534-case dev set.",
        "totals": total, "databases": per_db,
        "macro_database_ea": sum(d["guided_ea"] for d in per_db) / len(per_db),
        "metric_policy": {
            "guided_ea": "Sum of retained correct outcomes / all held-out test cases, including errors and missing predictions. Case-weighted, not an average of database percentages.",
            "result_selection": "Nine-database processed snapshot plus final thrombosis/toxicology continuations; includes approved historical failure-selected repeats and simulated-user interaction. Not a fresh uniform-runtime single-pass blind evaluation.",
            "question_unit": "One user-facing clarification question; exclude automatic conclusions, HTTP retries and SQL corrections. Use final retained-guidance session, counting reused answers once; do not add discarded experiment sessions.",
            "actual_mean": "Complete interaction logs or cumulative completed-checkpoint counters only; 403 historical cases lack original counters.",
            "resolved_mean": "Explicit answered user_choice/user_explanation items, with zero-question cases included when verifiable. Missing records are unknown, not zero. Counts recoverable for 749/765 cases.",
            "full_set_question_mean": "Unknown. 886/765 is only a lower bound from retained answered questions, not an exact full-test average.",
        },
        "source_runs": {"nine_database_snapshot": str(NINE), **{k: str(v) for k, v in NEW.items()}},
        "missing_resolved_question_cases": [f'{r["database_id"]}/{r["source_id"]}' for r in result if r["questions_resolved"] is None],
    }
    jsonl(target / "cases.jsonl", result)
    write(target / "summary.json", summary)
    assert all(sha(Path(p)) == h for p, h in source_hashes.items())
    write(target / "verification.json", {"status": "passed", "source_sha256": source_hashes,
          "unique_test_cases": len(keys), "guided_prediction_files_verified": 153,
          "new_online_question_counters_verified": 153, "source_files_unchanged": True,
          "no_model_calls": True, "no_baseline_reruns": True})
    render(target, summary)
    write(OUT / "bird_guided_statistics_latest.json", {"folder": str(target), "html": str(target / "index.html"),
          "summary": str(target / "summary.json"), "cases": str(target / "cases.jsonl")})
    print(json.dumps({"folder": str(target), "totals": total}, ensure_ascii=False))


def render(target, summary):
    total = summary["totals"]
    def average(v):
        return "未知" if v is None else f"{v:.3f}"
    h = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BIRD 11库 Guided EA 与澄清问题数</title><style>body{font:16px/1.7 system-ui;max-width:1350px;margin:30px auto;padding:0 20px;color:#17324a;background:#f8fafc}table{border-collapse:collapse;width:100%;background:white}td,th{border:1px solid #cfd9e3;padding:9px;text-align:left}aside{background:#fff0cf;padding:15px}pre{white-space:pre-wrap;overflow-wrap:anywhere}.scroll{overflow-x:auto}</style><body><h1>BIRD 11库：Guided EA 与澄清问题统计</h1>'''
    h += f'<p>2026-11-24 · 当前项目 held-out test 划分 · {total["test_cases"]} cases</p><h2>Guided EA：{total["guided_correct"]}/{total["test_cases"]}＝{total["guided_ea"]:.2%}</h2>'
    h += f'<p>按case加权汇总，包含缺失预测与最终错误；11库EA的算术平均为{summary["macro_database_ea"]:.2%}，主指标使用前者。</p>'
    h += '<aside>保留此前九库处理后结果，并加入thrombosis和toxicology最终恢复结果。包含已授权的历史失败集重跑和模拟用户交互；这不是新的统一runtime单次盲测，也不是官方完整1534条dev集的评测。</aside>'
    h += f'<h2>可恢复的已回答澄清问题：{total["resolved_question_total"]}/{total["resolved_question_cases"]}＝{total["resolved_questions_per_observed_case"]:.3f}题/case</h2><p>覆盖{total["resolved_question_cases"]}/{total["test_cases"]}条；还有{total["resolved_question_unknown_cases"]}条未知，不能算作0。全部765条的精确平均数暂不可得。按已保留问题计算的全量下界为{total["retained_question_lower_bound_per_all_case"]:.3f}题/case。</p>'
    h += f'<p>严格由原始交互日志或checkpoint累计计数核验的子集：{total["actual_question_total"]}题/{total["actual_question_cases"]} cases＝{total["actual_questions_per_observed_case"]:.3f}题/case。不能将这个子集均值视作全量均值。</p>'
    h += '<p>一个向用户提出的澄清问题计一次，一轮可含多题。自动判断、HTTP重试、SQL修复不计数。复用已确认回答只计一次；采用最终guidance会话的累计计数，不累计废弃实验会话。</p><div class="scroll"><table><tr><th>Database</th><th>Guided正确/总数</th><th>Guided EA</th><th>已回答问题数/可统计cases</th><th>平均已回答问题/case</th><th>原日志/累计计数覆盖</th></tr>'
    for d in summary["databases"]:
        h += f'<tr><td>{escape(d["database_id"])}</td><td>{d["guided_correct"]}/{d["test_cases"]}</td><td>{d["guided_ea"]:.2%}</td><td>{d["resolved_question_total"]}/{d["resolved_question_cases"]}</td><td>{average(d["resolved_questions_per_observed_case"])}</td><td>{d["actual_question_cases"]}/{d["test_cases"]}</td></tr>'
    h += f'<tr><th>合计</th><th>{total["guided_correct"]}/{total["test_cases"]}</th><th>{total["guided_ea"]:.2%}</th><th>{total["resolved_question_total"]}/{total["resolved_question_cases"]}</th><th>{total["resolved_questions_per_observed_case"]:.3f}</th><th>{total["actual_question_cases"]}/{total["test_cases"]}</th></tr></table></div>'
    h += '<p><a href="summary.json">汇总与统计规则</a> · <a href="cases.jsonl">逐case EA、问题数和来源</a> · <a href="verification.json">原始文件哈希核验</a></p><h2>无法恢复已回答问题数的case</h2><pre>'
    h += escape("\n".join(summary["missing_resolved_question_cases"])) + '</pre></body></html>'
    (target / "index.html").write_text(h)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    compile_report(args.output.resolve())
