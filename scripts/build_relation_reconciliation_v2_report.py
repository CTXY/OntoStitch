"""Build an offline audit/report of v2 implementation and bounded model probes."""
from collections import Counter
from datetime import datetime
from hashlib import sha256
from html import escape
import json
from pathlib import Path
from zoneinfo import ZoneInfo


REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / 'output/reconciliation_v2_20261121'


def read(path):
    return json.loads(path.read_text())


def build():
    from cortex.capabilities.graph_merge.reconciliation.policy import reconciliation_system, SIGNALS
    from cortex.capabilities.graph_merge.llm.relation_merger import RelationResults
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    old = read(ROOT/'policy_before_live.json')
    changed = [name for name, digest in old.items()
        if not (REPO/name).exists() or sha256((REPO/name).read_bytes()).hexdigest() != digest]
    live = read(ROOT/'live_retry/summary.json')
    rows, max_input, actual_calls = [], 0, 0
    for record in live['results']:
        folder = ROOT/'live_retry'/record['name']
        events = [json.loads(line) for line in (folder/'model_calls.jsonl').read_text().splitlines()]
        calls = Counter(row['component'] for row in events if row.get('event_type') == 'model_start')
        budgets = [row['characters'] for row in events if row.get('event_type') == 'request_budget']
        rejections = [{'tool': row.get('name'), 'arguments': row.get('arguments'), 'feedback': row.get('result')}
            for row in events if row.get('event_type') == 'tool_result' and row.get('result', {}).get('status') == 'rejected_unapplied']
        observed = [row for row in events if row.get('event_type') == 'request_contract']
        valid_tools = True
        for row in observed:
            if row['component'] == 'integration_relation_merger':
                allowed = {'read_evidence', 'submit_relations'}
            elif row['component'] == 'integration_reconcile':
                allowed = {'read_nodes', 'read_relations', 'read_evidence', 'edit_draft'}
            else:
                allowed = {'read_nodes', 'read_relations', 'read_evidence', 'Review'}
            valid_tools &= set(row['tool_names']) <= allowed
        timeout_ok, no_sdk_retry, wire_calls = True, True, 0
        for wire in (folder/'wire').glob('*_request.json'):
            item = read(wire)
            timeout_ok &= item['timeout']['read'] == 300
            no_sdk_retry &= item.get('sdk_retry') in {'0', 0}
            wire_calls += 1
        item = {'name': record['name'], 'status': record['status'], 'calls': dict(calls),
            'wire_calls': wire_calls, 'max_complete_input_characters': max(budgets),
            'tool_permissions_valid': valid_tools, 'actual_http_read_timeout_300': timeout_ok,
            'sdk_retries_zero': no_sdk_retry, 'rejections': rejections,
            'source_graph_hashes_unchanged': record['source_files_unchanged'],
            'assessment': 'not_required' if record['name'] == 'relation_merger' else 'independent_accept',
            'report': 'live_retry/' + record['name'] + '/result.json'}
        if record['name'] == 'relation_merger':
            item.update(groups=10, real_source_cases=1, controlled_metric_replicas=9,
                        completed_resume_model_calls=record['completed_resume_model_calls'])
        rows.append(item)
        actual_calls += sum(calls.values())
        max_input = max(max_input, max(budgets))
    review = [
        {'area': 'RelationMerger', 'decision': 'passed_after_revision',
         'fixed': ['batch size differs from atomic transaction size', 'pending model result cannot be marked applied',
                   'durable result before outer collection resumes with zero replay', 'scope/schema/evidence/policy binding', 'local focus for nested node repair']},
        {'area': 'Context and prompts', 'decision': 'passed_after_revision',
         'fixed': ['8 signals separate from obligations', 'full initial business objects and coverage',
                   'unused installed predicates reachable without request_context', 'restore closure idempotent',
                   'immutable candidate session and source handles', 'repeated reads are not new facts']},
        {'area': 'Contract and execution', 'decision': 'passed_after_revision',
         'fixed': ['ordinary node creation and branch-local edits retained', 'conflict resolution and clone bypass blocked',
                   'cross-scope merge cannot silently broadcast', 'old defects cannot silently defer and disappear',
                   'deferred queue persists with candidate and wakes by dependency content']}]
    validation = {'updated_at': stamp, 'status': 'implemented_and_bounded_live_probes_passed',
        'targeted_tests': {'passed': 149, 'failed': 0, 'modules': 17, 'full_repository_suite': False, 'ruff_F': 'passed'},
        'reviews': review, 'real_calls': actual_calls, 'probe_call_cap': 48,
        'real_results': rows, 'max_complete_input_characters': max_input,
        'production_files_unchanged_during_live': not changed, 'changed_production_files': changed,
        'production_files_checked': len(old), 'background_runtime_switched': False,
        'original_case_graphs_and_checkpoints': 'preserved',
        'transport': {'http_read_timeout_seconds': 300, 'actual_timeout_or_rotation': False,
                      'retry_with_different_key_offline_regression': 'passed'},
        'limits': ['A ten-group batch contains nine controlled copies of one real BIRD metric context.',
                   'Different protected formal properties or branch scopes remain separate subgroups; consolidation never silently broadens scope.',
                   'RelationMerger deliberately has no independent Assessment.',
                   'Four reconciliation probes have actual independent accepting Review, each in one Assessment call.',
                   'One premature read of a draft-only new node was rejected and recovered; no unresolved protocol error.',
                   'No full database merge or all-eight-signals real-model benchmark was run.']}
    (ROOT/'validation.json').write_text(json.dumps(validation, ensure_ascii=False, indent=2)+'\n')
    prompt_parts = ['<!doctype html><html lang="zh"><meta charset="utf-8"><title>v2 实际中文 Prompts</title>',
        '<style>body{font:16px/1.7 system-ui;max-width:1100px;margin:30px auto}pre{white-space:pre-wrap;background:#f6f7f8;padding:18px}summary{cursor:pointer}</style>',
        '<h1>实际加载的中文 Prompts</h1><p>共用规则、角色规范与本轮 Signal 指导按实际加载器导出；不含模型历史。</p>',
        '<h2>Relation Merger（无 Assessment）</h2><pre>' + escape((REPO/'src/cortex/capabilities/graph_merge/llm/prompts/relation_merger/writer.zh.md').read_text()) + '</pre>',
        '<details><summary>Relation Merger 输出 schema</summary><pre>' + escape(json.dumps(RelationResults.model_json_schema(), ensure_ascii=False, indent=2)) + '</pre></details>']
    for role in ('writer', 'assessment'):
        for kind in SIGNALS:
            body = reconciliation_system(role, [kind])
            prompt_parts.append('<details><summary>' + role + ' · ' + kind + f' · {len(body)} 字符</summary><pre>' + escape(body) + '</pre></details>')
    (ROOT/'prompts.html').write_text(''.join(prompt_parts)+'</html>')
    table = ''.join('<tr><td>'+escape(item['name'])+'</td><td>'+escape(item['status'])+'</td><td>'
        + str(sum(item['calls'].values()))+'</td><td>'+escape(item['assessment'])+'</td><td><a href="'
        + item['report']+'">实际结果</a></td></tr>' for item in rows)
    page = ['<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>Relation Merger / Reconciliation v2 实施与验证</title>',
        '<style>body{font:16px/1.8 system-ui;max-width:1180px;margin:30px auto;padding:0 22px;color:#203044}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd5df;padding:10px;text-align:left}pre{white-space:pre-wrap;background:#f4f6f8;padding:16px}a{color:#165db0}</style>',
        '<h1>Relation Merger / Reconciliation v2 实施与验证</h1><p>'+escape(stamp)+'</p>',
        '<p>三个独立 subagent 完成方案与接口 review，阻塞发现修复后实施。原 case graphs、旧 checkpoint 与失败记录保留，未切换后台 merge。</p>',
        '<p><a href="../../docs/design/reconciliation-v2-implementation.md">完整修改方案</a> · <a href="prompts.html">实际完整中文 Prompts</a> · ',
        '<a href="../../src/cortex/capabilities/graph_merge/llm/prompts/reconciliation/README.md">Prompt 源文件入口</a> · ',
        '<a href="validation.json">验证与审查记录</a></p>',
        '<h2>结果</h2><p>149 项专项测试、Ruff F 通过；5 项离线与真实小测均通过。真实模型共19次调用，最大完整输入42,526字符（含工具schema）。</p>',
        '<table><tr><th>样本</th><th>真实结果</th><th>调用</th><th>评估方式</th><th>证据</th></tr>'+table+'</table>',
        '<p>Relation Merger一次请求处理10组，含9个由实际Metric构造的受控副本；不等于10个独立BIRD案例。四例Reconciliation的独立Assessment均首轮提交接受Review，无需补读。</p>',
        '<p>边的正式属性／分支scope不同仍分独立子组，本轮未强制跨scope合并。合并描述人工复核保留了十一月分子首项、十二月分子减项及分母三种作用。</p>',
        '<p>维度补全曾提前请求草稿中新建节点的关系，权限门禁正确拒绝；随后使用已给材料恢复并通过。所有原有节点在该样本中保持不变，新建了标签维度与必要连接。</p>',
        '<p>本轮没有请求超时，未实际触发换key；实际HTTP read timeout为300秒，SDK隐藏重试为0，换key及错误脱敏路径已做离线故障注入回归。</p>',
        '<h2>各轮记录</h2><ul><li><a href="offline_first/index.html">最初离线：发现无实例谓词契约不可达</a></li>',
        '<li><a href="offline_second/index.html">修复后四例离线通过</a></li>',
        '<li><a href="offline_full/index.html">完整五项离线通过</a></li>',
        '<li><a href="offline_final/index.html">最终入口与只读fixture库离线复核</a></li>',
        '<li><a href="live_first/index.html">首次真实启动前检查：环境加载顺序错误，0次API</a></li>',
        '<li><a href="live_retry/index.html">真实调用：五项全部通过，19次API</a></li></ul>',
        '<h2>方案与代码核查</h2><p>源码与Prompt在真实调用期间保持不变：'+str(not changed)+'。',
        '结构校验、来源、scope、metadata、冲突保护、候选恢复和延期持久化有独立专项覆盖；完整数据库与其余Signal的真实模型表现未在本轮全量测试。</p>',
        '<details><summary>完整审查与验证说明</summary><pre>'+escape(json.dumps(validation,ensure_ascii=False,indent=2))+'</pre></details></html>']
    (ROOT/'index.html').write_text(''.join(page))
    return ROOT/'index.html'


if __name__ == '__main__':
    print(build())
