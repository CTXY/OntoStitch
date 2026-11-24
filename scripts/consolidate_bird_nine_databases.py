"""Snapshot the nine evaluated BIRD databases without making model calls.

Keep published outcomes and formula's failure-selected repeat as separate views.
Count user-facing questions from matched interaction logs; expose missing logs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
LATEST = OUT / "latest_results"
GRAPHS = OUT / "latest_graphs"
MAIN = OUT / "bird_guided_only_20261120_015101"
REPEAT = OUT / "formula_guided_failure_repeat_20261120_140606"
PAIRED = OUT / "bird_online_paired_20261119_134017"
USER_SOURCES = {"user_choice", "user_explanation"}


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text_sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values))


def key(row):
    return (row.get("database_id") or row.get("db_id"), str(row.get("source_id", row.get("question_id"))))


def handoff(g):
    down = g.get("downstream_guidance") or {}
    return down.get("agent_handoff", down)


def content(g):
    return (g.get("guidance_content") or handoff(g).get("guidance_content") or "").strip()


def compact_online(record):
    result = {k: record[k] for k in (
        "source_id", "database_id", "question", "initial_sql", "status", "error",
        "error_code", "error_stage", "interaction_rounds", "simulated_user_decisions",
    ) if k in record}
    final = record.get("online_after_interaction") or {}
    result["completed_diagnostics"] = final.get("diagnostics")
    return result


class Bundle:
    def __init__(self, target):
        self.target = target
        self.assets = []
        self.issues = []
        self.guidances = defaultdict(list)
        self.context_by_content = defaultdict(list)
        self.manifest = read(LATEST / "manifest.json")
        self.databases = list(self.manifest["online_sources"])
        assert len(self.databases) == 9
        self.graph_manifest = read(GRAPHS / "manifest.json")
        self.main = {key(x): x for x in rows(MAIN / "full_test_results.jsonl")}
        self.repeat = {key(x): x for x in rows(REPEAT / "comparison_full_test_results.jsonl")}
        self.repeat_status = read(REPEAT / "status.json")
        self.repeat_sources = {tuple(x["case"].split("/")): x for x in read(REPEAT / "guidance_sources.json")}
        self.devs = {(x["db_id"], str(x["question_id"])): x for x in read(ROOT / "datasets/bird/dev/dev.json")}
        self.before = []
        self.after = []
        self.questions = []
        self.offline = []
        self.stats = []
        self.protected = {str(p): sha(p) for p in [
            MAIN / "full_test_results.jsonl", REPEAT / "comparison_full_test_results.jsonl",
            LATEST / "manifest.json", GRAPHS / "manifest.json",
        ]}

    def copy(self, source, relative, expected=None):
        source = Path(source)
        digest = sha(source)
        if expected is not None:
            assert digest == expected, source
        dest = self.target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        self.assets.append({"path": str(relative), "source": str(source.resolve()), "sha256": digest})
        return str(relative)

    def ingest_guidance(self, g, source, priority=0):
        if not isinstance(g, dict) or key(g)[0] not in self.databases:
            return
        # Traces and retrieved graphs are retained in their source records, not duplicated.
        small = {k: v for k, v in g.items() if k not in {"online_context", "organizer_trace"}}
        ctx = g.get("online_context")
        entry = {"guidance": small, "source": str(source.resolve()), "priority": priority,
                 "context": compact_online(ctx) if isinstance(ctx, dict) else None}
        self.guidances[key(g)].append(entry)
        if content(g) and isinstance(ctx, dict) and ctx.get("status") != "error":
            self.context_by_content[(key(g), text_sha(content(g)))].append(entry)

    def discover_guidances(self):
        for db in self.databases:
            for g in rows(LATEST / "online" / db / "guidance.jsonl"):
                self.ingest_guidance(g, LATEST / "online" / db / "guidance.jsonl", 1)
        for base, dirs, files in os.walk(OUT):
            dirs[:] = [d for d in dirs if not (
                d.startswith("runtime") or d.startswith("bird_unified_") or d in {
                    "wire", "recovery_archives", "credentials", ".credentials", "__pycache__",
                    "latest_graphs", "latest_results", "discovery_batches", "discovery_batches_native_fresh",
                    "retrieval", "retrieval_batched", "retrieval_native_fresh", "http_admission",
                }
            )]
            for name in files:
                p = Path(base) / name
                if name != "guidance_cases.json" and not (p.parent.name == "guidance" and p.suffix == ".json"):
                    continue
                data = read(p)
                entries = data.get("cases", []) if name == "guidance_cases.json" else [data]
                for g in entries:
                    self.ingest_guidance(g, p, 2)

    def select_guidance(self, case_key, row, source_file):
        candidates = self.guidances[case_key]
        sid = case_key[1]
        paths = []
        folder = row.get("guided_result_folder")
        if folder:
            paths.append(Path(folder) / "guidance" / f"{sid}.json")
        src = (row.get("result_sources") or {}).get("guided") or {}
        if src.get("path"):
            paths.append(Path(src["path"]).parent / "guidance" / f"{sid}.json")
        elif row.get("new_result_folder"):
            # This legacy field can survive later replacements. It is authoritative
            # only when there is no newer prediction-source reference.
            paths.append(Path(row["new_result_folder"]) / "guidance" / f"{sid}.json")
        for path in paths:
            for entry in candidates:
                if entry["source"] == str(path.resolve()):
                    return entry, "same_prediction_folder"
        # An explicitly stored repeat snapshot proves which guidance generated that repeat.
        if row.get("guided_repeat_provenance"):
            snapshot = Path(self.repeat_sources[case_key]["snapshot"])
            for entry in candidates:
                if entry["source"] == str(snapshot.resolve()):
                    return entry, "repeat_protocol_snapshot"
        exported = [e for e in candidates if e["priority"] == 1]
        if exported:
            original_rows = {key(x): x for x in rows(LATEST / "online" / case_key[0] / "results.jsonl")}
            old = original_rows.get(case_key, {}).get("guided")
            if old == row.get("guided"):
                e = exported[0]
                if e["guidance"].get("matched_to_recorded_guided_run"):
                    return e, "published_export_matched"
        # A same-case artifact is useful, but does not establish provenance for the final SQL.
        available = [e for e in candidates if content(e["guidance"])]
        if available:
            return sorted(available, key=lambda e: e["priority"])[0], "same_case_only_unverified"
        return None, "missing"

    def interaction(self, entry, match):
        unknown = {"asked_count": None, "round_count": None, "count_basis": "missing_matched_guidance",
                   "resolved_user_questions": None, "questions": [], "log_source": None}
        if entry is None or match in {"missing", "same_case_only_unverified"}:
            return unknown
        g = entry["guidance"]
        hg = handoff(g)
        resolved = [x for x in hg.get("interaction_guidance", []) if x.get("source") in USER_SOURCES]
        records = []
        same = self.context_by_content.get((key(g), text_sha(content(g))), [])
        if entry.get("context") is not None:
            same = [entry] + same
        for candidate in same:
            records = []
            ctx = candidate.get("context") or {}
            if ctx.get("status") == "error" or "interaction_rounds" not in ctx:
                continue
            rounds = ctx["interaction_rounds"]
            for i, rnd in enumerate(rounds):
                answers = rnd.get("decisions", [])
                answer_by_id = {x.get("question_id"): x for x in answers}
                for j, q in enumerate(rnd.get("questions", [])):
                    qid = q.get("question_id", q.get("id"))
                    answer = answer_by_id.get(qid)
                    if answer is None and j < len(answers):
                        answer = answers[j]
                    records.append({"round": i + 1, "stage": rnd.get("stage"), "question": q, "answer": answer})
            presented = (ctx.get("completed_diagnostics") or {}).get("presented_questions")
            if presented is not None and presented > len(records):
                # A completed workflow restored from checkpoint can have no new rounds.
                # Its final diagnostics persist the cumulative answered-question count.
                if presented == len(resolved):
                    restored = [{"round": None, "stage": None,
                                 "question": {"question_id": x.get("choice_point_id"), "prompt": x.get("decision_question")},
                                 "answer": {"source": x.get("source"), "confirmed_guidance": x.get("should_be")}}
                                for x in resolved]
                    return {"asked_count": presented, "round_count": None,
                            "count_basis": "matched_completed_checkpoint_counter",
                            "resolved_user_questions": len(resolved), "questions": restored,
                            "log_source": candidate["source"], "completed_diagnostics": ctx["completed_diagnostics"]}
                continue
            if len(records) < len(resolved):
                continue
            return {"asked_count": len(records), "round_count": len(rounds), "count_basis": "matched_interaction_log",
                    "resolved_user_questions": len(resolved), "questions": records, "log_source": candidate["source"]}
        # These entries are produced by the guidance synthesizer from answered choices.
        # They exclude automatic question_resolved/database_equivalent conclusions.
        if content(g) and "interaction_guidance" in hg:
            records = [{"round": None, "stage": None,
                        "question": {"question_id": x.get("choice_point_id"), "prompt": x.get("decision_question")},
                        "answer": {"source": x.get("source"), "confirmed_guidance": x.get("should_be")}}
                       for x in resolved]
            return {"asked_count": None, "round_count": None, "count_basis": "resolved_guidance_only",
                    "resolved_user_questions": len(resolved), "questions": records, "log_source": entry["source"]}
        return unknown

    def normalize(self, case_key, row, source_file):
        db, sid = case_key
        dev = self.devs[case_key]
        entry, match = self.select_guidance(case_key, row, source_file)
        g = entry["guidance"] if entry else None
        q = self.interaction(entry, match)
        pred = row.get("guided")
        plain = row.get("plain")
        # Preserve historical baselines as their own arm with explicit source provenance.
        plain_source = (row.get("result_sources") or {}).get("plain")
        if plain is None:
            path = PAIRED / db / "cases" / sid / "plain_prediction.json"
            if path.exists():
                plain = read(path)
                plain_source = {"path": str(path), "sha256": sha(path), "status": "historical_partial_baseline"}
        return {"database_id": db, "source_id": sid, "question": row.get("question") or dev["question"],
                "evidence": row.get("evidence", dev.get("evidence")), "gold_sql": dev["SQL"],
                "guided": pred, "baseline": plain,
                "status": "recorded" if pred is not None else "no_recorded_prediction",
                "source_result_file": str(source_file), "guided_source": (row.get("result_sources") or {}).get("guided"),
                "baseline_source": plain_source,
                "formula_repeat": row.get("guided_repeat_provenance"),
                "guidance": g, "guidance_source": entry["source"] if entry else None,
                "guidance_match": match, "interaction": q}

    def export_case(self, record, original):
        db, sid = key(record)
        relative = Path(db) / "test/cases" / sid
        folder = self.target / relative
        folder.mkdir(parents=True, exist_ok=True)
        write(folder / "record.json", record)
        write(folder / "before_formula_repeat.json", original)
        (folder / "gold.sql").write_text(record["gold_sql"].rstrip() + "\n")
        for arm, name in [("guided", "guided.sql"), ("baseline", "baseline.sql")]:
            pred = record[arm]
            if pred is not None and pred.get("predicted_sql"):
                (folder / name).write_text(pred["predicted_sql"].rstrip() + "\n")
        if record["guidance"] is not None:
            write(folder / "guidance.json", record["guidance"])
        write(folder / "questions.json", record["interaction"])

    def export_offline(self, db):
        gm = self.graph_manifest["databases"][db]
        write(self.target / db / "offline/provenance.json", gm)
        training = {str(r["question_id"]): r for r in rows(LATEST / "training" / db / "sql_records.jsonl")}
        for sid, meta in gm["cases"].items():
            rel = Path(db) / "offline/cases" / sid
            src = GRAPHS / db / "case_level_graphs" / sid / "state.json"
            graph_file = self.copy(src, rel / "graph.json", meta["sha256"])
            sql = training.get(sid)
            dev = self.devs[(db, sid)]
            rec = {"database_id": db, "source_id": sid, "question": dev["question"],
                   "graph": graph_file, "graph_sha256": meta["sha256"],
                   "historical_graph_source": meta["source"], "sql_record": sql,
                   "extraction_interaction_count": None,
                   "interaction_status": "original_extraction_logs_not_in_retained_graph_exports"}
            self.offline.append(rec)
            write(self.target / rel / "record.json", rec)
            if sql:
                assert sql["graph_sha256"] == meta["sha256"]
                for k, name in [("gold_sql", "gold.sql"), ("generated_sql", "generated.sql")]:
                    if sql.get(k):
                        (self.target / rel / name).write_text(sql[k].rstrip() + "\n")
        merged = gm["merged"]
        self.copy(GRAPHS / db / "merged_state.json", Path(db) / "merged/graph.json", merged["sha256"])
        write(self.target / db / "merged/provenance.json", merged)
        if merged["omitted_cases"] or merged["source_status"] == "partial":
            self.issues.append({"database_id": db, "type": "partial_merge_coverage", "detail": merged})
        if merged["merged_cases"] != len(gm["cases"]):
            self.issues.append({"database_id": db, "type": "merge_coverage_vs_retained_graph_count_differs",
                                "retained_graphs": len(gm["cases"]), "merge_reported_cases": merged["merged_cases"]})
        return gm

    def process_database(self, db):
        print(f"Consolidating {db}", flush=True)
        gm = self.export_offline(db)
        inputs = {}
        for path in sorted((PAIRED / db).glob("shards/*/cases.jsonl")):
            for inp in rows(path):
                assert key(inp)[0] == db
                sid = key(inp)[1]
                assert sid not in inputs
                inputs[sid] = inp
        published_file = LATEST / "online" / db / "results.jsonl"
        published = {key(x)[1]: x for x in rows(published_file)}
        assert set(published) <= set(inputs)
        assert not set(inputs).intersection(gm["cases"]), f"training/test overlap: {db}"
        jsonl(self.target / db / "test/inputs.jsonl", list(inputs.values()))
        before, after = [], []
        for sid in sorted(inputs, key=int):
            k = db, sid
            current = self.main.get(k, published.get(sid, inputs[sid]))
            processed = self.repeat.get(k, current)
            src = MAIN / "full_test_results.jsonl" if k in self.main else published_file
            a = self.normalize(k, current, src)
            b = self.normalize(k, processed, REPEAT / "comparison_full_test_results.jsonl" if k in self.repeat else src)
            assert a["baseline"] == b["baseline"]
            if (a["guided"] or {}).get("evaluation", {}).get("exec") == 1:
                assert b["guided"] == a["guided"]
            self.export_case(b, a)
            before.append(a)
            after.append(b)
            self.questions.append({"database_id": db, "source_id": sid,
                                   **{k: v for k, v in b["interaction"].items() if k != "questions"},
                                   "guidance_match": b["guidance_match"]})
        self.before.extend(before)
        self.after.extend(after)
        jsonl(self.target / db / "test/results_before_formula_repeat.jsonl", before)
        jsonl(self.target / db / "test/results_processed.jsonl", after)
        stats = database_statistics(db, before, after)
        stats.update(offline_graphs=len(gm["cases"]), merged_graph=gm["merged"], test_input_cases=len(inputs))
        self.stats.append(stats)
        write(self.target / db / "summary.json", stats)
        return before, after

    def run(self):
        self.target.mkdir(parents=True, exist_ok=False)
        self.discover_guidances()
        for db in self.databases:
            self.process_database(db)
        jsonl(self.target / "offline_cases.jsonl", self.offline)
        jsonl(self.target / "test_results_before_formula_repeat.jsonl", self.before)
        jsonl(self.target / "test_results_processed.jsonl", self.after)
        jsonl(self.target / "question_counts.jsonl", self.questions)
        summary = {"created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                   "database_count": len(self.databases), "databases": self.stats,
                   "totals": database_statistics("ALL", self.before, self.after),
                   "offline_case_graphs": len(self.offline), "merged_graphs": len(self.databases),
                   "formula_repeat_status": self.repeat_status["status"],
                   "formula_repeat_pending": self.repeat_status.get("deferred"),
                   "metric_policy": {
                       "ea_denominator": "All cases from retained test input shards, including missing predictions and SQL errors.",
                       "processed": "Published retained outcomes plus explicitly requested formula failure-selected repeat replacements; not a new independent single-pass benchmark.",
                       "baseline": "Historical available arms only; missing arms are not zero accuracy. Baseline was not rerun.",
                       "question_unit": "One user-facing clarification question, not a round, API call, SQL retry, or automatic decision.",
                       "exact_question_mean": "Only cases with content-matched completed interaction logs or cumulative completed-checkpoint question counters, including verified zero-question cases.",
                       "resolved_question_mean": "Explicit user_choice/user_explanation items in matched guidance; excludes automatic conclusions; not a claim that deleted logs are complete.",
                       "reruns": "Each case uses the session that produced its retained guidance. Reused guidance and network retries are not counted again.",
                       "offline_questions": "Unknown: original extraction interaction logs are missing from retained graph-only exports.",
                   }}
        write(self.target / "summary.json", summary)
        write(self.target / "issues.json", include_missing_records(self.issues, self.after))
        write(self.target / "asset_manifest.json", self.assets)
        assert all(sha(self.target / a["path"]) == a["sha256"] for a in self.assets)
        assert all(sha(Path(p)) == v for p, v in self.protected.items())
        write(self.target / "verification.json", {
            "status": "passed", "source_hashes_unchanged": self.protected,
            "copied_graph_assets_sha256_verified": len(self.assets),
            "unique_test_keys": len({key(r) for r in self.after}), "test_rows": len(self.after),
            "baseline_records_preserved": True, "previous_guided_passes_preserved": True,
            "no_model_calls": True,
        })
        render_report(self.target, summary, self.after)
        print(json.dumps({"folder": str(self.target), "totals": summary["totals"]}, ensure_ascii=False), flush=True)


def include_missing_records(issues, cases):
    result = list(issues)
    by_db = defaultdict(list)
    for row in cases:
        by_db[row["database_id"]].append(row)
    for db, records in by_db.items():
        for issue, predicate in [
            ("test_prediction_record_missing", lambda r: r["guided"] is None),
            ("test_final_error", lambda r: bool((r["guided"] or {}).get("error"))),
            ("guidance_provenance_unverified", lambda r: r["guidance_match"] in {"missing", "same_case_only_unverified"}),
            ("original_question_count_unavailable", lambda r: r["interaction"]["asked_count"] is None),
            ("resolved_question_count_unavailable", lambda r: r["interaction"]["resolved_user_questions"] is None),
            ("historical_baseline_record_unavailable", lambda r: r["baseline"] is None),
        ]:
            ids = [r["source_id"] for r in records if predicate(r)]
            if ids:
                result.append({"database_id": db, "type": issue, "case_count": len(ids), "source_ids": ids})
    return result


def database_statistics(db, before, after):
    def correct(r, arm):
        return (r[arm] or {}).get("evaluation", {}).get("exec") == 1

    n = len(after)
    exact = [r["interaction"]["asked_count"] for r in after if r["interaction"]["asked_count"] is not None]
    resolved = [r["interaction"]["resolved_user_questions"] for r in after
                if r["interaction"]["resolved_user_questions"] is not None]
    baseline = [r for r in after if r["baseline"] is not None]
    guided_correct = sum(correct(r, "guided") for r in after)
    before_correct = sum(correct(r, "guided") for r in before)
    return {"database_id": db, "test_cases": n,
            "guided_before_correct": before_correct, "guided_before_ea": before_correct / n,
            "guided_processed_correct": guided_correct, "guided_processed_ea": guided_correct / n,
            "guided_prediction_records": sum(r["guided"] is not None for r in after),
            "guided_sql_files": sum(bool((r["guided"] or {}).get("predicted_sql")) for r in after),
            "guided_final_errors": sum(bool((r["guided"] or {}).get("error")) for r in after),
            "guided_prediction_missing": sum(r["guided"] is None for r in after),
            "guidance_available": sum(bool(r["guidance"] and content(r["guidance"])) for r in after),
            "guidance_matches": dict(Counter(r["guidance_match"] for r in after)),
            "baseline_available": len(baseline), "baseline_missing": n - len(baseline),
            "baseline_correct_available": sum(correct(r, "baseline") for r in baseline),
            "baseline_ea_available_only": sum(correct(r, "baseline") for r in baseline) / len(baseline) if baseline else None,
            "baseline_full_set_ea": sum(correct(r, "baseline") for r in baseline) / n if len(baseline) == n else None,
            "exact_question_cases": len(exact), "exact_question_total": sum(exact),
            "exact_questions_per_case": sum(exact) / len(exact) if exact else None,
            "exact_zero_question_cases": exact.count(0),
            "exact_question_count_distribution": dict(Counter(exact)),
            "question_count_bases": dict(Counter(r["interaction"]["count_basis"] for r in after)),
            "exact_question_unknown_cases": n - len(exact),
            "resolved_question_cases": len(resolved), "resolved_question_total": sum(resolved),
            "resolved_questions_per_case": sum(resolved) / len(resolved) if resolved else None,
            "resolved_question_unknown_cases": n - len(resolved),
            "question_log_count_vs_resolved_count_mismatches": sum(
                r["interaction"]["asked_count"] is not None and r["interaction"]["resolved_user_questions"] is not None
                and r["interaction"]["asked_count"] != r["interaction"]["resolved_user_questions"] for r in after),
            "full_set_exact_questions_per_case": sum(exact) / n if len(exact) == n else None}


def render_report(target, summary, cases):
    e = escape
    stats = summary["databases"]
    total = summary["totals"]
    def avg(value):
        return "未知" if value is None else f"{value:.3f}"

    h = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BIRD 九库统一结果</title><style>
body{font:15px/1.65 system-ui;color:#183044;background:#f6f8fb;margin:28px auto;max-width:1500px;padding:0 20px}h1,h2{line-height:1.3}table{border-collapse:collapse;width:100%;background:white;margin:16px 0}td,th{padding:9px;border:1px solid #d7e0e8;text-align:left;vertical-align:top}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:500px;overflow:auto}aside{background:#fff2d9;padding:14px}a{color:#1265a8}input,select{padding:9px;margin-right:12px}small{color:#596c7c}.scroll{overflow-x:auto}.case{background:white;padding:12px;margin:8px 0;border:1px solid #d7e0e8}.metric{font-size:20px}</style><body>'''
    h += f'<h1>BIRD 九库统一结果</h1><p>{e(summary["created_at"])} · 现存产物快照 · 无新模型调用</p>'
    h += '<aside>Formula：原保留结果 56/87＝64.37%，失败集补充重跑后 66/87＝75.86%，增加10条、11.49个百分点。31条中完成30条；958的memory阶段HTTP 502重试耗尽，尚未完成，原失败结果仍保留。补充重跑结果不能称为新的独立单次EA。</aside>'
    h += f'<p class="metric">{summary["offline_case_graphs"]} 个 offline case-level graphs · 9 个 merged graphs · {total["test_cases"]} 条 test cases</p>'
    h += f'<p>九库现存发布结果：{total["guided_before_correct"]}/{total["test_cases"]}＝{total["guided_before_ea"]:.2%}；纳入Formula补充重跑：{total["guided_processed_correct"]}/{total["test_cases"]}＝{total["guided_processed_ea"]:.2%}。各库历史流程不同，此处是现存结果汇总。Baseline仅保存了{total["baseline_available"]}条，缺少{total["baseline_missing"]}条，无法给出完整九库baseline EA。</p>'
    h += '<p><a href="summary.json">汇总 JSON</a> · <a href="test_results_processed.jsonl">处理后 test 全记录</a> · <a href="test_results_before_formula_repeat.jsonl">Formula补充重跑前记录</a> · <a href="offline_cases.jsonl">Offline 台账</a> · <a href="question_counts.jsonl">逐case提问计数</a> · <a href="asset_manifest.json">图来源与SHA-256</a> · <a href="issues.json">覆盖与缺失问题</a> · <a href="verification.json">核验</a></p>'
    h += '<h2>产物和EA</h2><div class="scroll"><table><tr><th>Database</th><th>Offline graphs</th><th>Merged graph覆盖</th><th>Test总数</th><th>处理前 → 处理后 Guided EA</th><th>Guided SQL / guidance</th><th>历史baseline可用</th></tr>'
    for s in stats:
        db = s["database_id"]
        m = s["merged_graph"]
        h += f'<tr><td>{e(db)}</td><td><a href="{db}/offline/cases/">{s["offline_graphs"]}</a></td><td><a href="{db}/merged/graph.json">{m["merged_cases"]} cases</a> · {e(m["source_status"])}<br><small>omitted: {e(", ".join(m["omitted_cases"]) or "无")}</small></td><td>{s["test_cases"]}</td><td>{s["guided_before_correct"]}/{s["test_cases"]} ({s["guided_before_ea"]:.2%}) → {s["guided_processed_correct"]}/{s["test_cases"]} ({s["guided_processed_ea"]:.2%})</td><td>{s["guided_sql_files"]} / {s["guidance_available"]}<br><small>最终错误{s["guided_final_errors"]}；预测记录缺失{s["guided_prediction_missing"]}</small></td><td>{s["baseline_available"]}/{s["test_cases"]}</td></tr>'
    h += '</table></div><h2>每个case被提问多少次</h2><p>一个澄清问题算一次；一轮可包含多题。自动消歧不算提问，网络/SQL重试不算提问。同一case复用guidance只计算其对应交互会话一次。原始日志无法恢复时，不将缺失当成0。</p>'
    h += f'<p><b>原始交互日志或已完成checkpoint累计计数可核验：{total["exact_question_total"]}题 / {total["exact_question_cases"]} cases，平均{avg(total["exact_questions_per_case"])}题/case。</b>仍有{total["exact_question_unknown_cases"]}条缺少可匹配的原始计数，不能据此声称是九库全量平均。</p>'
    h += f'<p>Matched guidance中可恢复的已回答问题：{total["resolved_question_total"]}题 / {total["resolved_question_cases"]} cases，平均{avg(total["resolved_questions_per_case"])}题/case；{total["resolved_question_unknown_cases"]}条无法核验。此项只统计user_choice/user_explanation；属于保留的已确认澄清数，不能保证涵盖历史未回答或丢失的问题。Offline提问次数因原始extraction交互日志缺失而未知。</p>'
    h += '<table><tr><th>Database</th><th>日志/累计计数覆盖</th><th>实际问题总数 / 均值</th><th>已回答问题覆盖</th><th>已回答问题总数 / 均值</th><th>日志/已回答计数差异</th></tr>'
    for s in stats:
        h += f'<tr><td>{e(s["database_id"])}</td><td>{s["exact_question_cases"]}/{s["test_cases"]}</td><td>{s["exact_question_total"]} / {avg(s["exact_questions_per_case"])}</td><td>{s["resolved_question_cases"]}/{s["test_cases"]}</td><td>{s["resolved_question_total"]} / {avg(s["resolved_questions_per_case"])}</td><td>{s["question_log_count_vs_resolved_count_mismatches"]}</td></tr>'
    h += '</table><h2>逐case查询</h2><select id="database"><option value="">全部数据库</option>'
    h += ''.join(f'<option value="{e(s["database_id"])}">{e(s["database_id"])}</option>' for s in stats)
    h += '</select><input id="search" placeholder="搜索 case ID 或问题"><span id="visible"></span>'
    for r in cases:
        db, sid = key(r)
        pred = r["guided"] or {}
        state = '通过' if pred.get('evaluation', {}).get('exec') == 1 else '执行/生成错误' if pred.get('error') else 'EA不匹配' if pred else '无预测记录'
        q = r["interaction"]
        ref = f'{db}/test/cases/{sid}'
        h += f'<details class="case" data-db="{e(db)}" data-search="{e((db+" "+sid+" "+r["question"]).lower(), quote=True)}"><summary>{e(db)}/{sid} · {state} · 提问 {avg(q["asked_count"])} · {e(r["question"])}</summary>'
        h += f'<p><a href="{ref}/record.json">全记录</a> · <a href="{ref}/questions.json">问题/回答和计数来源</a> · guidance match: {e(r["guidance_match"])}</p>'
        for name, value in [('Gold SQL', r['gold_sql']), ('Guided SQL', pred.get('predicted_sql')), ('Baseline SQL', (r['baseline'] or {}).get('predicted_sql')), ('Guidance', content(r['guidance']) if r['guidance'] else None)]:
            h += f'<details><summary>{e(name)}</summary><pre>{e(value or "缺失")}</pre></details>'
        h += '</details>'
    h += '''<script>function filter(){let n=0;for(const e of document.querySelectorAll('.case')){const ok=(!database.value||e.dataset.db===database.value)&&e.dataset.search.includes(search.value.toLowerCase());e.hidden=!ok;n+=ok;}visible.textContent=n+' cases';}const database=document.getElementById('database'),search=document.getElementById('search'),visible=document.getElementById('visible');database.addEventListener('change',filter);search.addEventListener('input',filter);filter();</script></body></html>'''
    (target / "index.html").write_text(h)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    Bundle(args.output.resolve()).run()
