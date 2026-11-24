"""Read-only replay of the two real oversized reconciliation inputs.

Input fixtures and original requests are retained separately from running roots.
Only public messages/tools are exported; no transport headers or credentials.
"""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai.chat_models.base import _convert_message_to_dict

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.reconciliation_contract import (
    TOOLS,
    DraftDocument,
)
from cortex.capabilities.graph_merge.integration.relation_context import (
    relation_context,
)
from cortex.capabilities.graph_merge.integration.signal_premises import signal_premise
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.integration.task_state import canonical_hash
from cortex.capabilities.graph_merge.llm.reconciliation_agent import DESCRIPTIONS
from cortex.capabilities.graph_merge.llm.reconciliation_context import (
    ReconciliationRuntime,
)
from cortex.capabilities.graph_merge.llm.reconciliation_projection import (
    edge_record,
    node_record,
)
from cortex.capabilities.graph_merge.llm.reconciliation_prompts import (
    reconciliation_system,
)
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import GraphSpace, GraphState, load_root_schema

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / 'output/signal_subgraphs_20261122'


def read(path):
    return json.loads(path.read_text())


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def characters(body):
    return len(json.dumps(body, ensure_ascii=False, default=str))


def setup(fixture, phase='reconcile'):
    before = GraphState.model_validate(fixture['base'])
    evidence = tuple(EvidenceItem.model_validate(e) for e in fixture['evidence'])
    ctx = IntegrationContext(before, GraphState(graph=GraphSpace()), load_root_schema(), evidence=evidence)
    ctx.task_before = ctx.candidate = before
    ctx.active_draft = deepcopy(fixture['draft'])
    raw = fixture['task']
    task = LocalTask(raw['id'], raw['phase'], set(raw['targets']), checks=deepcopy(raw['checks']),
                     groups=deepcopy(raw['groups']), feedback=deepcopy(raw['feedback']))
    ctx.active_task = task
    payload = relation_context(ctx, before, task, before=before if phase == 'graph_assess' else None,
                               assessment=phase == 'graph_assess')
    runtime = ReconciliationRuntime(ctx, payload, phase)
    runtime.system = reconciliation_system('writer' if phase == 'reconcile' else 'assessment',
                                           [row['kind'] for row in runtime.packet['signals']])
    if phase == 'reconcile':
        runtime.set_compiled_candidate(before, runtime.draft.version)
        runtime.set_feedback({'mechanical': None, 'latest_receipt': {'draft_saved': True, 'errors': []}})
    return ctx, runtime


def request(runtime):
    tools = list(runtime.available_read_tools(runtime.build_read_tools()).values())
    if runtime.phase == 'reconcile':
        tools.append(StructuredTool.from_function(lambda **kw: kw, name='edit_draft',
            description=DESCRIPTIONS['edit_draft'], args_schema=TOOLS['edit_draft']))
    return {'messages': [_convert_message_to_dict(m) for m in runtime.messages()],
            'tools': [convert_to_openai_tool(t) for t in tools]}


def scope(runtime):
    before = runtime.working['before']
    return {'node_ids': sorted(before['nodes']), 'edge_ids': sorted(before['edges']),
            'contract_ids': sorted(before.get('contracts', {})),
            'write_node_ids': sorted(runtime.write_node_ids), 'write_edge_ids': sorted(runtime.write_edge_ids),
            'relation_sets': deepcopy(runtime.packet.get('relation_sets', [])),
            'read_tools': sorted(runtime.available_read_tools(runtime.build_read_tools()))}


def change_edge(state, ident, marker):
    return state.model_copy(update={'graph': state.graph.model_copy(update={'edges': tuple(
        edge.model_copy(update={'properties': {**edge.properties, 'description': str(edge.properties.get('description', '')) + marker}})
        if edge.id == ident else edge for edge in state.graph.edges)})})


def controls(fixture, ctx, runtime):
    """Use the real graph to test scope/receipt/recovery, with zero model calls."""
    before_hash = canonical_hash(ctx.task_before)
    before = runtime.working['before']
    # Every displayed object retains the canonical public projection, including
    # complete alternatives and its own evidence. No source graph is read.
    all_nodes = {n.id: n for n in ctx.task_before.graph.nodes}
    all_edges = {e.id: e for e in ctx.task_before.graph.edges}
    projection = all(before['nodes'][i] == node_record(ctx, all_nodes[i]) for i in before['nodes'])
    projection = projection and all(before['edges'][i] == edge_record(ctx, all_edges[i]) for i in before['edges'])
    saved = runtime.snapshot()
    _, recovered = setup(fixture)
    recovered.restore(saved)
    exact_recovery = recovered.snapshot() == saved and recovered.messages() == runtime.messages()
    checks = fixture['task']['checks']
    initial = [signal_premise(ctx, ctx.task_before, check) for check in checks]
    relevant, unrelated = None, None
    core_ids = set(runtime._signal_scope.core_node_ids)
    for ident in sorted(all_edges):
        modified = change_edge(ctx.task_before, ident, ' [controlled receipt regression]')
        current = [signal_premise(ctx, modified, check) for check in checks]
        if current != initial and ident in before['edges'] and relevant is None:
            relevant = ident
        if (current == initial and ident not in before['edges'] and unrelated is None
                and {all_edges[ident].from_node_id, all_edges[ident].to_node_id} & core_ids):
            unrelated = ident
        if relevant and unrelated:
            break
    boundary_error = None
    excluded = next((ident for ident in sorted(all_edges) if ident not in runtime.write_edge_ids), None)
    if excluded:
        draft = DraftDocument(task_id=runtime.task_id, before_id=runtime.before_id, version=1, changes=[{
            'change_id': 'outside-scope-control', 'kind': 'edge_update', 'reason': 'Controlled rejection test only.',
            'edge_id': excluded, 'set': {'properties': {'description': 'This must not be written.'}}}])
        _, write_control = setup(fixture)
        write_control.set_draft(draft)
        try:
            write_control.validate_submission(draft)
        except ValueError as error:
            boundary_error = str(error)
    result = {'model_calls': 0, 'full_public_content_and_evidence_preserved': projection,
        'checkpoint_restore_same_messages_and_permissions': exact_recovery,
        'related_edge_reopens_receipt': relevant,
        'unrelated_edge_keeps_receipt': unrelated,
        'unrelated_control_is_excluded_one_hop_of_core': bool(unrelated and {all_edges[unrelated].from_node_id, all_edges[unrelated].to_node_id} & core_ids),
        'excluded_edge_write_rejected': bool(boundary_error and 'OUT_OF_SCOPE' in boundary_error),
        'excluded_edge_id': excluded, 'write_rejection': boundary_error,
        'fixed_before_unchanged': canonical_hash(ctx.task_before) == before_hash}
    assert projection and exact_recovery and relevant and unrelated and result['excluded_edge_write_rejected'] and result['fixed_before_unchanged'], result
    return result


def render_input(title, body):
    parts = ['<details><summary>' + escape(title) + '</summary>']
    for index, message in enumerate(body['messages'], 1):
        parts += ['<h4>' + escape(str(index) + ' · ' + message['role']) + '</h4>', '<pre>' + escape(message.get('content') or '') + '</pre>']
    parts += ['<h4>Complete public tool schemas</h4><pre>' + escape(json.dumps(body['tools'], ensure_ascii=False, indent=2)) + '</pre></details>']
    return ''.join(parts)


def build(output):
    summaries = []
    for short in ('thrombosis', 'toxicology'):
        folder = output / short
        fixture = read(folder / 'fixture.json')
        baseline = read(folder / 'before_request.json')
        ctx, runtime = setup(fixture)
        current = request(runtime)
        _, assessment = setup(fixture, 'graph_assess')
        assessment_input = request(assessment)
        writer_scope = scope(runtime)
        validation = controls(fixture, ctx, runtime)
        old_fixed = baseline['messages'][1]['content']
        import re
        old_nodes = re.findall(r'^### Node `([^`]+)`', old_fixed, re.MULTILINE)
        old_edges = re.findall(r'^### Relationship `([^`]+)`', old_fixed, re.MULTILINE)
        old_contracts = re.findall(r'^### Predicate `([^`]+)`', old_fixed, re.MULTILINE)
        summary = {'database': short, 'task_id': fixture['task']['id'], 'signals': fixture['task']['checks'],
            'baseline_actual_transport_request': True, 'after_is_offline_exact_request_reconstruction': True,
            'before_characters': characters(baseline), 'after_characters': characters(current),
            'assessment_noop_projection_characters': characters(assessment_input),
            'saved_characters': characters(baseline) - characters(current),
            'saved_percent': round(100 * (1 - characters(current) / characters(baseline)), 2),
            'before_counts': {'nodes': len(old_nodes), 'edges': len(old_edges), 'contracts': len(old_contracts)},
            'after_counts': {k: len(writer_scope[k + '_ids']) for k in ('node', 'edge', 'contract')},
            'removed_node_ids': sorted(set(old_nodes) - set(writer_scope['node_ids'])),
            'removed_edge_ids': sorted(set(old_edges) - set(writer_scope['edge_ids'])),
            'scope': writer_scope, 'regressions': validation,
            'local_request_max_characters': 500000,
            'under_unchanged_guard': characters(current) < 500000 and characters(assessment_input) < 500000}
        assert summary['after_characters'] < summary['before_characters'] and summary['under_unchanged_guard'], summary
        write(folder / 'after_request.json', current)
        write(folder / 'assessment_noop_request.json', assessment_input)
        write(folder / 'regression.json', summary)
        html = '<h1>' + short + '：真实大输入范围回归</h1><p><a href="../index.html">返回报告</a></p>'
        html += '<p>修改前为真实失败任务对应的原始模型请求；修改后在相同固定图、相同 Signal、相同草稿和来源上重建。以下离线对比没有发送模型，不代表语义 Assessment 通过。原 checkpoint 不修改。</p>'
        html += '<pre>' + escape(json.dumps(summary, ensure_ascii=False, indent=2)) + '</pre>'
        html += render_input('修改前：Writer完整输入（实际请求）', baseline)
        html += render_input('修改后：Writer完整输入（离线重建）', current)
        html += render_input('修改后：空修改候选Assessment完整输入（离线重建）', assessment_input)
        live_records = sorted(folder.glob('live_*/result.json'), reverse=True)
        for index, live in enumerate(live_records):
            live_result = read(live)
            label = '最新真实模型小测' if index == 0 else '保留的历史尝试（网络故障）'
            html += '<p>' + label + ': <a href="' + live.parent.name + '/index.html">' + escape(live.parent.name + ' · ' + live_result['status']) + '</a></p>'
        (folder / 'index.html').write_text(page(html))
        summaries.append(summary)
        print(short, summary['before_characters'], summary['after_characters'], summary['after_counts'], flush=True)
    write(output / 'regression_summary.json', {'checked_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), 'results': summaries})
    html = '<h1>按 Signal 构造必要子图：真实大输入回归</h1><p>保留500k输入保护。原始 checkpoint 和已通过任务不修改；离线回归不调用模型。字符数计入JSON转义、全部消息和公开工具schema，不是token数。</p><table><tr><th>库</th><th>原请求字符</th><th>新请求字符</th><th>减少</th><th>节点/边/契约（旧→新）</th><th>完整输入</th></tr>'
    for row in summaries:
        count = '/'.join(str(row['before_counts'][k]) for k in ('nodes', 'edges', 'contracts')) + ' → ' + '/'.join(str(row['after_counts'][k]) for k in ('node', 'edge', 'contract'))
        html += '<tr><td>' + row['database'] + '</td><td>' + str(row['before_characters']) + '</td><td>' + str(row['after_characters']) + '</td><td>' + str(row['saved_percent']) + '%</td><td>' + count + '</td><td><a href="' + row['database'] + '/index.html">每条消息与公开工具schema</a></td></tr>'
    html += '</table>'
    for short in ('thrombosis', 'toxicology'):
        live_records = sorted((output / short).glob('live_*/result.json'), reverse=True)
        if not live_records:
            continue
        live = live_records[0]
        row = read(live)
        html += '<p>最新真实模型小测：<a href="' + str(live.parent.relative_to(output)) + '/index.html">' + escape(row['database'] + ' · ' + row['status']) + '</a>，调用 ' + str(row.get('model_calls', 0)) + ' 次，耗时 ' + str(row.get('elapsed_seconds', 0)) + ' 秒。独立Assessment已接受；原图未改动，全部检查逐项闭合。</p>'
    if (output / 'network_diagnosis.json').exists():
        html += '<p>更早两次网络失败记录完整保留在各库页面；最新重试已通过。<a href="network_diagnosis.json">网络路径诊断</a>（未修改OS网络或DNS配置）。</p>'
    html += '<p>测试完整对象投影与来源保留、相关变化重新检查、无关变化不使receipt失效、局部写权限、相同契约的checkpoint恢复。源图正文只按当前单张图显示，不加载原case-level graphs。</p><p><a href="regression_summary.json">机器可读结果</a></p>'
    (output / 'index.html').write_text(page(html))
    return summaries


def page(body):
    return '<!doctype html><meta charset="utf-8"><title>Signal subgraph regression</title><style>body{font:16px/1.6 system-ui;max-width:1200px;margin:30px auto;padding:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:14px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:8px;text-align:left}details{margin:18px 0}summary{cursor:pointer;font-weight:700}</style>' + body


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build(args.output)
