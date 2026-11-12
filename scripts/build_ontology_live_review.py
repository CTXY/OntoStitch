"""Build a review packet from actual, unmodified extraction results."""

from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter
from pathlib import Path

from cortex.infrastructure.artifacts.graph_html import render_graph_html
from cortex.schema import GraphSpace, load_root_schema, validate_graph_space

SEMANTIC = {"concept", "metric", "event", "generation_rule"}
RUN_NAME = re.compile(r"case(?P<case>\d+|financial\d+)_r(?P<round>\d+)$")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def esc(value):
    return html.escape(str(value))


def collect(root):
    attempts = []
    for directory in sorted(root.iterdir()):
        match = RUN_NAME.fullmatch(directory.name)
        if not directory.is_dir() or not match:
            continue
        terminal = root / f"{directory.name}_result.json"
        attempt = {
            "run": directory.name,
            "case": match["case"],
            "round": int(match["round"]),
            "status": "pending",
        }
        if terminal.exists():
            outer = read(terminal)
            attempt.update(status=outer["status"], terminal_result=str(terminal.resolve()))
            workspace = Path(outer["workspace"])
            archive_path = root / f"{directory.name}_archive.json"
            if archive_path.exists():
                workspace = Path(read(archive_path)["archive_workspace"])
            results = list(workspace.glob("train_runs/*/cases/*/result/result.json"))
            if outer.get("case_result_path"):
                result_path = Path(outer["case_result_path"])
                result = read(result_path)
                case_root = Path(outer["case_root"])
            elif len(results) == 1:
                result_path = results[0]
                result = read(result_path)
                case_root = result_path.parent.parent
            else:
                run_results = list(workspace.glob("train_runs/*/result.json"))
                case_roots = list(workspace.glob("train_runs/*/cases/*/case.json"))
                if len(run_results) != 1 or len(case_roots) != 1:
                    attempts.append(attempt)
                    continue
                result_path = run_results[0]
                cases = read(result_path).get("cases", [])
                if len(cases) != 1 or not cases[0].get("accepted_revision"):
                    attempts.append(attempt)
                    continue
                result = cases[0]
                case_root = case_roots[0].parent
            if result:
                snapshot = read(case_root / "ontology" / f"{result['accepted_revision']}.json")
                graph = snapshot["graph"]
                assessments = []
                episodes = []
                first_observed_stage = {}
                for evidence_path in sorted((case_root / "evidence").glob("*.json"), key=lambda p: p.stat().st_mtime):
                    with evidence_path.open(encoding="utf-8") as handle:
                        header = handle.read(512)
                    if re.search(r'"kind"\s*:\s*"assessment"', header):
                        assessments.append(read(evidence_path)["payload"])
                    elif re.search(r'"kind"\s*:\s*"episode_result"', header):
                        episodes.append(read(evidence_path)["payload"])
                    elif re.search(r'"kind"\s*:\s*"node_result"', header):
                        node_result = read(evidence_path)["payload"]
                        stage = node_result.get("node")
                        revision = node_result.get("updates", {}).get("working_revision")
                        stage_path = case_root / "ontology" / f"{revision}.json"
                        if stage in {"direct", "repair"} and revision and stage_path.exists():
                            for node in read(stage_path)["graph"]["nodes"]:
                                if node["node_type"] == "generation_rule":
                                    first_observed_stage.setdefault(node["id"], stage)
                graph_role = "accepted"
                display_revision = result["accepted_revision"]
                if outer.get("proposal_revision"):
                    display_revision = outer["proposal_revision"]
                    graph = read(case_root / "ontology" / f"{display_revision}.json")["graph"]
                    graph_role = outer.get("graph_role", "unaccepted_proposal")
                elif not any(n["node_type"] in SEMANTIC for n in graph["nodes"]) and episodes:
                    proposal_revision = episodes[-1]["delivered_revision"]
                    proposal_path = case_root / "ontology" / f"{proposal_revision}.json"
                    if proposal_path.exists():
                        proposal = read(proposal_path)["graph"]
                        if any(n["node_type"] in SEMANTIC for n in proposal["nodes"]):
                            graph = proposal
                            display_revision = proposal_revision
                            graph_role = "unaccepted_proposal"
                semantic = [n for n in graph["nodes"] if n["node_type"] in SEMANTIC]
                ids = {n["id"] for n in semantic}
                matching_assessments = [
                    a for a in assessments if a.get("trial_revision") == display_revision
                ]
                quality_acceptable = None
                if matching_assessments:
                    units = matching_assessments[-1]["feedback"].get("unit_results", [])
                    if units:
                        quality_acceptable = all(u["quality"] == "valid" for u in units)
                attempt.update(
                    source=read(case_root / "case.json")["source"],
                    result=result,
                    result_path=str(result_path.resolve()),
                    graph=graph,
                    semantic_nodes=semantic,
                    semantic_edges=[e for e in graph["edges"] if e["from_node_id"] in ids],
                    semantic_type_counts=dict(Counter(n["node_type"] for n in semantic)),
                    accepted_revision=result["accepted_revision"],
                    correct=result.get("correct", False),
                    sql_correct=result.get("sql_correct", False),
                    assessments=assessments,
                    episodes=episodes,
                    graph_role=graph_role,
                    displayed_revision=display_revision,
                    rule_first_observed_stage=first_observed_stage,
                    quality_acceptable=quality_acceptable,
                )
        attempts.append(attempt)
    return attempts


def build(root):
    attempts = collect(root)
    selected = {}
    for attempt in attempts:
        key = attempt["case"]
        if key not in selected or selected[key]["round"] < attempt["round"]:
            selected[key] = attempt
    review = root / "review"
    review.mkdir(exist_ok=True)
    independent_path = root / "independent_semantic_review.json"
    review_path = independent_path if independent_path.exists() else root / "semantic_review.json"
    independent = read(review_path) if review_path.exists() else {}
    reviews = {item["run"]: item for item in independent.get("current_reviews", [])}
    sections = []
    manifest = []
    schema = load_root_schema()
    for key, attempt in sorted(selected.items()):
        run = attempt["run"]
        manifest.append({k: v for k, v in attempt.items() if k not in {"graph", "source", "result", "semantic_nodes", "semantic_edges"}})
        folder = review / run
        folder.mkdir(exist_ok=True)
        if "graph" not in attempt:
            sections.append(f"<section><h2>{esc(run)}</h2><p>状态：{esc(attempt['status'])}。尚无最终接受图，不能视为质量通过。</p></section>")
            continue
        graph = attempt["graph"]
        hard_issues = []
        try:
            validate_graph_space(GraphSpace.model_validate(graph), schema)
        except ValueError as exc:
            hard_issues.append(str(exc))
        audit = {
            "run": run,
            "revision": attempt["displayed_revision"],
            "graph_role": attempt["graph_role"],
            "schema_validation_passed": not hard_issues,
            "issues": hard_issues,
            "semantic_nodes": len(attempt["semantic_nodes"]),
            "semantic_outgoing_edges": len(attempt["semantic_edges"]),
            "edges_with_description": sum(bool(e.get("properties", {}).get("description", "").strip()) for e in attempt["semantic_edges"]),
            "note": "Deterministic validation does not establish business semantics; independent review is separate.",
        }
        manifest[-1]["hard_contract_audit"] = audit
        write(folder / "hard_contract_audit.json", audit)
        source, result = attempt["source"], attempt["result"]
        write(folder / "graph.json", {"graph": graph})
        write(folder / "semantic.json", {"nodes": attempt["semantic_nodes"], "edges": attempt["semantic_edges"]})
        write(folder / "case.json", source)
        write(folder / "result.json", result)
        write(folder / "assessment.json", attempt.get("assessments", []))
        try:
            rendered = render_graph_html(GraphSpace.model_validate(graph), schema, title=run)
        except ValueError as exc:
            graph_link = f"<p>当前 schema 的交互图校验未通过：{esc(exc)}。原始 JSON 保留。</p>"
        else:
            (folder / "graph.html").write_text(rendered, encoding="utf-8")
            graph_link = f'<a href="{esc(run)}/graph.html">完整交互图</a> · '
            if (review / run / "business_graph.html").is_file():
                graph_link += f'<a href="{esc(run)}/business_graph.html">业务语义视图</a> · '
        nodes = []
        current_assessment = next((a for a in reversed(attempt.get("assessments", [])) if a.get("trial_revision") == attempt["displayed_revision"]), {})
        checks_by_id = {u["unit_id"]: u.get("quality_checks", {}) for u in current_assessment.get("feedback", {}).get("unit_results", [])}

        def check_text(unit_id, checks=checks_by_id):
            return " · ".join(f"{key}: {value}" for key, value in checks.get(unit_id, {}).items())

        for node in attempt["semantic_nodes"]:
            props = json.dumps(node["properties"], ensure_ascii=False, indent=2)
            origin = attempt.get("rule_first_observed_stage", {}).get(node["id"])
            origin_label = f"<br><small>首次出现阶段：{esc(origin)}；Repair 不自动等于 SQL error-driven</small>" if origin else ""
            nodes.append(f"<tr><td>{esc(node['node_type'])}{origin_label}<br><small>{esc(check_text(node['id']))}</small></td><td>{esc(node['name'])}<br><small>{esc(node['id'])}</small></td><td>{esc(node.get('description') or '')}</td><td><pre>{esc(props)}</pre></td></tr>")
        by_id = {node["id"]: node["name"] for node in graph["nodes"]}
        edges = []
        for edge in attempt["semantic_edges"]:
            edges.append(f"<tr><td>{esc(by_id.get(edge['from_node_id'], edge['from_node_id']))}</td><td>{esc(edge['edge_type'])}</td><td>{esc(by_id.get(edge['to_node_id'], edge['to_node_id']))}</td><td>{esc(edge.get('properties', {}).get('description', ''))}<br><small>{esc(edge['id'])}<br>{esc(check_text(edge['id']))}</small></td></tr>")
        independent_review = reviews.get(run, {"verdict": "not_yet_independently_reviewed"})
        sections.append(f"""<section><h2>{esc(run)}</h2>
<p>{esc(source['question'])}</p>
<p>运行状态：{esc(attempt['status'])}；训练完成原因：{esc(result.get('completion_reason', ''))}；SQL 正确：{esc(attempt['sql_correct'])}；质量门禁：{esc(attempt['quality_acceptable'])}；硬规则复核：{esc(not hard_issues)}</p>
<p>独立语义复核记录：{esc(independent_review.get('verdict'))}（与训练自动验收独立，不是人工批准门禁）</p>
<p><strong>展示身份：{esc(attempt['graph_role'])}；accepted 为正式训练接受版本；proposal 为实验候选，不代表已完成正式验收。人工 review 不是训练完成条件。</strong></p>
<p>接受版本：{esc(attempt['accepted_revision'])}；展示版本：{esc(attempt['displayed_revision'])}；语义节点：{len(nodes)}；语义出边：{len(edges)}</p>
<p>停止/失败原因：{esc(result.get('reason') or result.get('completion_reason') or '')}</p>
<p>{graph_link}<a href="{esc(run)}/graph.json">图 JSON</a> · <a href="{esc(run)}/semantic.json">语义节点与边</a> · <a href="{esc(run)}/result.json">原始运行结果</a></p>
<details><summary>原始 SQL 与最终生成 SQL</summary><pre>{esc(source['sql'])}</pre><pre>{esc(result.get('query'))}</pre></details>
<details open><summary>语义评估与未解决问题</summary><pre>{esc(json.dumps(independent_review, ensure_ascii=False, indent=2))}</pre></details>
<details><summary>逐轮 Assessment：节点与关系的 quality_checks</summary><pre>{esc(json.dumps(attempt.get('assessments', []), ensure_ascii=False, indent=2))}</pre></details>
<h3>语义节点</h3><table><tr><th>类型</th><th>名称与 ID</th><th>定义</th><th>匹配或计算</th></tr>{''.join(nodes)}</table>
<h3>显式关系及说明</h3><table><tr><th>起点</th><th>关系</th><th>终点</th><th>具体语义与限制</th></tr>{''.join(edges)}</table></section>""")
    attempt_rows = []
    for attempt in attempts:
        if "graph" in attempt:
            folder = review / attempt["run"]
            write(folder / "graph.json", {"graph": attempt["graph"]})
            write(folder / "semantic.json", {"nodes": attempt["semantic_nodes"], "edges": attempt["semantic_edges"]})
            write(folder / "result.json", attempt["result"])
    for attempt in sorted(attempts, key=lambda item: (item["case"], item["round"])):
        run_label = esc(attempt["run"])
        if "graph" in attempt:
            run_label = f'<a href="{run_label}/semantic.json">{run_label}</a>'
        verdict = reviews.get(attempt["run"], {}).get("verdict", "not_independently_reviewed")
        attempt_rows.append(f"<tr><td>{run_label}</td><td>{esc(attempt['status'])}</td><td>{esc(attempt.get('correct', ''))}</td><td>{len(attempt.get('semantic_nodes', []))}</td><td>{esc(verdict)}</td></tr>")
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Ontology 实跑与评估</title>
<style>body{font:15px/1.6 system-ui;margin:30px auto;padding:0 22px;max-width:1500px;color:#243044;background:#f5f7fa}section{background:white;padding:24px;margin:24px 0;border:1px solid #dde3ec;border-radius:10px}table{border-collapse:collapse;width:100%;table-layout:fixed}td,th{border:1px solid #dce2eb;padding:9px;text-align:left;vertical-align:top;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}small{color:#59677a}a{color:#1459a6}</style>
<h1>Ontology 实跑与评估</h1><p>内容直接来自真实模型保存的快照，未人工修改图。优先显示接受图；没有语义接受图时明确标注未接受候选。每例显示最新一轮，未完成或失败不视为通过。SQL 正确与语义评估分别记录；旧轮问题和失败均保留。</p>
""" + "".join(sections)
    page += f"<section><h2>全部运行历史</h2><table><tr><th>运行</th><th>状态</th><th>Pipeline通过</th><th>接受语义节点数</th><th>语义复核</th></tr>{''.join(attempt_rows)}</table></section></html>"
    (review / "index.html").write_text(page, encoding="utf-8")
    write(review / "summary.json", {"selected": manifest, "semantic_review": independent,
        "attempts": [{k: v for k, v in a.items() if k not in {"graph", "semantic_nodes", "semantic_edges", "source", "result"}} for a in attempts]})
    print(json.dumps({"review": str((review / 'index.html').resolve()), "cases": len(selected), "attempts": len(attempts)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    build(parser.parse_args().root)
