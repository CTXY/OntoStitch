"""Build a review packet from completed extraction runs without altering their graphs."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from cortex.application.ontology_graph import GraphManager
from cortex.infrastructure.artifacts.graph_html import render_graph_html
from cortex.schema import GraphSpace, GraphState, load_root_schema

SEMANTIC_TYPES = {"concept", "event", "metric", "generation_rule"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def esc(value):
    return html.escape(str(value), quote=True)


def build(manifest_path, cases, target):
    manifest = read(manifest_path)
    originals = {int(row["question_id"]): row for row in manifest["cases"]}
    schema = load_root_schema()
    target.mkdir(parents=True, exist_ok=True)
    review_path = target / "independent_final_review.json"
    review = read(review_path) if review_path.exists() else {}
    reviews = {int(item["question_id"]): item for item in review.get("cases", [])}
    cards, rows = [], []
    for number, summary_path in sorted(cases.items()):
        summary_path = summary_path.resolve()
        summary = read(summary_path)
        workspace = Path(summary["workspace"])
        results = list(workspace.glob("train_runs/*/cases/*/result/result.json"))
        if len(results) != 1:
            raise ValueError(f"Expected one completed case in {workspace}")
        result_path = results[0]
        result = read(result_path)
        if not result["direct_completed"] or not result["correct"]:
            raise ValueError(f"Case {number} has not completed extraction and SQL validation")
        snapshot = read(result_path.parent.parent / "ontology" / f"{result['accepted_revision']}.json")
        state = GraphState.model_validate({"graph": snapshot["graph"]})
        GraphManager(schema).validate_state(state)
        semantic = [n for n in state.graph.nodes if n.node_type in SEMANTIC_TYPES]
        ids = {n.id for n in semantic}
        business = [e for e in state.graph.edges
                    if e.from_node_id in ids and e.to_node_id in ids
                    and e.reference_origin is None]
        output = target / f"case_{number}"
        output.mkdir(exist_ok=True)
        write(output / "source.json", originals[number])
        write(output / "result.json", result)
        write(output / "ontology.json", read(result_path.parent / "ontology.json"))
        write(output / "graph.json", state.graph.model_dump(mode="json"))
        (output / "graph.html").write_text(render_graph_html(
            state.graph, schema, title=f"BIRD {number} · case-level ontology"), encoding="utf-8")
        semantic_view = GraphSpace(
            nodes=tuple(semantic),
            edges=tuple(e for e in state.graph.edges if e.from_node_id in ids and e.to_node_id in ids),
        )
        (output / "semantic.html").write_text(render_graph_html(
            semantic_view, schema,
            title=f"BIRD {number} · 业务语义投影（物理节点省略）"), encoding="utf-8")
        names = {n.id: n.name for n in state.graph.nodes}
        node_rows = "".join(
            f"<tr><td>{esc(n.node_type)}</td><td>{esc(n.name)}<br><small>{esc(n.id)}</small></td>"
            f"<td>{esc(n.description or '')}<details><summary>完整定义与实现</summary>"
            f"<pre>{esc(json.dumps(n.properties, ensure_ascii=False, indent=2))}</pre></details></td></tr>"
            for n in semantic)
        edge_rows = "".join(
            f"<tr><td>{esc(names[e.from_node_id])}</td><td>{esc(e.edge_type)}</td>"
            f"<td>{esc(names[e.to_node_id])}</td><td><pre>{esc(json.dumps(e.properties, ensure_ascii=False, indent=2))}</pre></td></tr>"
            for e in business)
        source = originals[number]
        verdict = reviews.get(number)
        review_note = (f"独立审查：{verdict['total_score']}/14，{esc(verdict['review_status'])}。"
                       if verdict else "独立审查结果请见汇总报告。")
        cards.append(f"""<section id="case{number}"><h2>BIRD {number}</h2>
<p>{esc(source['question'])}</p><p><b>Evidence：</b>{esc(source.get('evidence', ''))}</p>
<p>{len(semantic)} 个语义节点，{len(business)} 条独立业务关系；连续正确 SQL 回放 {result.get('required_correct_replays')} 次。</p>
<p>{review_note}</p>
<p><small>来源轮次：{esc(summary_path.name)}。历史失败和修订记录保留在原运行目录。</small></p>
<p><a href="case_{number}/semantic.html">业务语义图</a> · <a href="case_{number}/graph.html">完整交互图</a> · <a href="case_{number}/graph.json">完整图 JSON</a> ·
<a href="case_{number}/ontology.json">语义节点 JSON</a> · <a href="case_{number}/result.json">运行结果与证据路径</a></p>
<details><summary>Gold 与生成 SQL</summary><h3>Gold</h3><pre>{esc(source['SQL'])}</pre>
<h3>Generated</h3><pre>{esc(result.get('query'))}</pre></details>
<h3>语义节点</h3><table><tr><th>类型</th><th>名称 / ID</th><th>定义 / 实现</th></tr>{node_rows}</table>
<h3>独立业务关系</h3><table><tr><th>起点</th><th>关系</th><th>终点</th><th>业务说明与条件</th></tr>{edge_rows}</table></section>""")
        rows.append({"question_id": number, "summary_path": str(summary_path),
                     "case_result_path": str(result_path), "semantic_nodes": len(semantic),
                     "business_relations": len(business), "semantic_types": sorted({n.node_type for n in semantic}),
                     "direct_completed": result["direct_completed"], "sql_correct": result["sql_correct"],
                     "required_correct_replays": result.get("required_correct_replays"),
                     "iterations": result.get("iterations"), "graph_path": str(output / "graph.json")})
    write(target / "cases.json", rows)
    write(target / "selected_cases.json", manifest)
    navigation = " · ".join(f'<a href="#case{r["question_id"]}">{r["question_id"]}</a>' for r in rows)
    (target / "index.html").write_text("""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Extraction training · BIRD case review</title><style>
body{font:16px/1.65 system-ui,sans-serif;max-width:1240px;margin:36px auto;padding:0 24px;color:#243044;background:#f7f9fc}
section{background:white;padding:28px;margin:28px 0;border:1px solid #dbe3ef;border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #dbe3ef;padding:10px;text-align:left;vertical-align:top}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace}small{color:#61718a}a{color:#185db6}summary{cursor:pointer}
</style><h1>Extraction training · BIRD case review</h1>
<p>原始案例 → Agent 抽取与修订 → SQL 回放与 Assessment → 接受图。此页面复制真实接受结果，不手工修改生成的节点或关系。</p>
<p>检索配置：词法召回＋LLM 选择。原始数据库说明参与上下文，额外生成的 metadata store 未使用。<a href="independent_final_review.json">独立语义审查</a> · <a href="assessment_rubric.json">评分标准</a> · <a href="review_packet_audit.json">原始输出一致性核验</a>。</p>
""" + f"<nav>{navigation}</nav>" + "".join(cards) + "</html>\n", encoding="utf-8")
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-run", action="append", required=True, help="BIRD_ID=run_summary.json")
    args = parser.parse_args()
    runs = {int(item.split("=", 1)[0]): Path(item.split("=", 1)[1]) for item in args.case_run}
    print(json.dumps(build(args.manifest, runs, args.output.resolve()), ensure_ascii=False, indent=2))
