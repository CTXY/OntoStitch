"""Render completed integration artifacts and measured agent usage without editing the graph."""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path


def esc(value):
    return html.escape(str(value), quote=True)


def details(label, value):
    return f"<details><summary>{esc(label)}</summary><pre>{esc(json.dumps(value, ensure_ascii=False, indent=2))}</pre></details>"


def build(root):
    result = json.loads((root / "merge_report.json").read_text())
    workflow = json.loads((root / "workflow.json").read_text())
    traces = [
        json.loads(line)
        for line in (root / "agent_traces.jsonl").read_text().splitlines()
    ]
    evidence = json.loads((root / "source_evidence.json").read_text())
    graph = result["state"]["graph"]
    from cortex.infrastructure.artifacts.graph_html import render_graph_html
    from cortex.schema import GraphState, SchemaDefinition

    state = GraphState.model_validate(result["state"])
    schema = SchemaDefinition.model_validate(workflow["request"]["schema"])
    (root / "graph.html").write_text(
        render_graph_html(
            state.graph, schema, title=f"Ontology merge · {result['run_id']}"
        )
    )
    nodes = {n["id"]: n for n in graph["nodes"]}
    semantic = {
        key: n
        for key, n in nodes.items()
        if n["node_type"] not in {"table", "column", "dimension_value"}
    }
    authored = [
        e
        for e in graph["edges"]
        if e["from_node_id"] in semantic or e["to_node_id"] in semantic
    ]
    business = [
        e
        for e in authored
        if e["from_node_id"] in semantic and e["to_node_id"] in semantic
    ]
    phases = []
    for event in traces:
        usages = [
            m["usage_metadata"]
            for m in event.get("messages", [])
            if m.get("usage_metadata")
        ]
        phases.append(
            {
                "phase": event["component"],
                "execution_origin": event.get("execution_origin", "live_model"),
                "model_calls": event.get("model_calls", 0),
                "payload_characters": event.get("payload_characters", 0),
                "input_tokens": sum(u.get("input_tokens", 0) for u in usages),
                "output_tokens": sum(u.get("output_tokens", 0) for u in usages),
                "largest_input_tokens": max(
                    (u.get("input_tokens", 0) for u in usages), default=0
                ),
                "tool_calls": dict(Counter(t["tool"] for t in event.get("tools", []))),
                "error": event.get("error"),
            }
        )
    audit = {
        "run_id": result["run_id"],
        "assessment": workflow.get("review"),
        "semantic_types": dict(Counter(n["node_type"] for n in semantic.values())),
        "semantic_nodes": len(semantic),
        "business_edges": len(business),
        "authored_adjacent_edges": len(authored),
        "all_nodes": len(nodes),
        "all_edges": len(graph["edges"]),
        "rewrites": len(result.get("rewrites", [])),
        "proposal_attempts": workflow.get("attempts"),
        "identity_attempts": workflow.get("identity_attempts"),
        "phases": phases,
        "model_calls": sum(p["model_calls"] for p in phases),
        "input_tokens": sum(p["input_tokens"] for p in phases),
        "output_tokens": sum(p["output_tokens"] for p in phases),
    }
    (root / "review_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2)
    )
    names = {key: n["name"] for key, n in nodes.items()}
    source_nodes = {
        n["id"]: n for n in workflow["request"]["incoming"]["graph"]["nodes"]
    }
    source_edges = {
        e["id"]: e for e in workflow["request"]["incoming"]["graph"]["edges"]
    }
    final_edges = {e["id"]: e for e in graph["edges"]}

    def ref_label(ref):
        source = ref["view"] != "result"
        found = (
            (source_nodes if source else nodes)
            if ref["kind"] == "node"
            else (source_edges if source else final_edges)
        )
        item = found.get(ref["id"], {})
        text = item.get("name") or item.get("edge_type") or ref["id"]
        return f"{ref['view']} · {ref['kind']} · {text}" + (
            f" / {ref['alternative_id']}" if ref.get("alternative_id") else ""
        )

    node_rows = "".join(
        f"<tr><td>{esc(n['node_type'])}</td><td><b>{esc(n['name'])}</b></td><td>{esc(n.get('description') or '')}{details('完整节点', n)}</td></tr>"
        for n in semantic.values()
    )
    edge_rows = "".join(
        f"<tr><td>{esc(names[e['from_node_id']])}</td><td>{esc(e['edge_type'])}</td><td>{esc(names[e['to_node_id']])}</td><td>{esc(e['properties'].get('description', ''))}{details('完整关系', e)}</td></tr>"
        for e in authored
    )
    change_rows = "".join(
        f"<tr><td>{'<br>'.join(esc(ref_label(r)) for r in c['before'])}</td><td>{'<br>'.join(esc(ref_label(r)) for r in c['after']) or '有据删除'}</td><td>{esc(c['explanation'])}{details('来源、目标与 revision', c)}</td></tr>"
        for c in result.get("rewrites", [])
    )
    source_cards = "".join(
        f"<article><b>{esc(key)}</b><p>{esc(row['question'])}</p><p>{esc(row.get('evidence', ''))}</p>{details('Gold SQL', row['sql'])}</article>"
        for key, row in evidence.items()
    )
    phase_rows = "".join(
        f"<tr><td>{esc(p['phase'])}<br><small>{esc(p['execution_origin'])}</small></td><td>{p['model_calls']}</td><td>{p['input_tokens']:,}</td><td>{p['output_tokens']:,}</td><td>{p['largest_input_tokens']:,}</td><td>{esc(p['tool_calls'])}</td></tr>"
        for p in phases
    )
    review_file = root / "semantic_review.json"
    review_card = (
        details("额外语义复查", json.loads(review_file.read_text()))
        if review_file.exists()
        else ""
    )
    seed_notice = (
        '<p class="card">这是基于已有真实候选图的增量验证：reviewer_seed_not_model 阶段来自已完成结果及复查反馈，不是新的模型调用。live_model 阶段为真实增量修订和 Assessment。</p>'
        if any(p["execution_origin"] != "live_model" for p in phases)
        else ""
    )
    two_stage = any(
        p["phase"] in {"integration_consolidate", "integration_reconcile"}
        for p in phases
    )
    assessment_notice = (
        "节点内容经过独立 Node Assessment；图级关系和组织由 Graph Reconciliation 自检并修复，代码执行最终严格校验。图级自检不等于独立语义证明。"
        if two_stage
        else "结果经过代码校验和独立模型 Assessment。"
    )
    page = f"""<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ontology merge review</title>
<style>body{{font:15px/1.65 system-ui;margin:32px auto;padding:0 24px;max-width:1450px;color:#203047;background:#f6f8fb}}h1,h2{{color:#12243e}}nav{{display:flex;gap:20px;flex-wrap:wrap}}a{{color:#1763a6}}article,.card{{background:white;border:1px solid #dce3ec;padding:18px;margin:16px 0;border-radius:10px}}table{{width:100%;border-collapse:collapse;background:white;margin:16px 0}}td,th{{text-align:left;padding:12px;border:1px solid #e1e6ee;vertical-align:top}}pre{{white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace;max-height:450px;overflow:auto}}summary{{cursor:pointer;color:#1763a6}}small{{color:#59677a}}</style>
<h1>Ontology graph 合并审查</h1><p>{esc(result["run_id"])} · {len(evidence)} 个真实 BIRD cases · {len(semantic)} 个语义节点 · {len(business)} 条业务关系</p>
<nav><a href="graph.html">完整交互图</a><a href="#sources">来源</a><a href="#nodes">节点</a><a href="#edges">关系与绑定</a><a href="#changes">结构对应</a><a href="#audit">评估与上下文</a><a href="merge_report.json">完整 JSON</a></nav>
<p class="card">{assessment_notice} 结果仍供人工 review；本次未重跑 NL2SQL 生成或数据库执行，不能据此宣称 SQL 准确率提升。</p>
{seed_notice}{review_card}
<h2 id="sources">来源 cases</h2>{source_cards}
<h2 id="nodes">语义节点</h2><table><tr><th>类型</th><th>名称</th><th>定义与属性</th></tr>{node_rows}</table>
<h2 id="edges">关系与绑定</h2><table><tr><th>起点</th><th>谓词</th><th>终点</th><th>业务语义与限制</th></tr>{edge_rows}</table>
<h2 id="changes">实质修改与结构对应</h2><p>这张表记录知识的处理去向，不代表每个 before 与每个 after 逐项同义。普通安全 ID 映射另见完整 JSON。</p><table><tr><th>Before</th><th>After</th><th>处理解释</th></tr>{change_rows}</table>
<h2 id="audit">独立评估与实际上下文</h2>{details("最终 Assessment", workflow.get("review"))}{details("Identity 判断", workflow.get("identity"))}
<table><tr><th>阶段</th><th>模型调用</th><th>累计输入 token</th><th>累计输出 token</th><th>最大单次输入</th><th>工具调用</th></tr>{phase_rows}</table>
<p><a href="review_audit.json">测量报告</a> · <a href="agent_traces.jsonl">完整模型交互</a> · <a href="workflow.json">阶段记录与提案</a></p></html>"""
    (root / "index.html").write_text(page, encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in audit.items() if k not in {"phases", "assessment"}},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    build(parser.parse_args().run_directory)
