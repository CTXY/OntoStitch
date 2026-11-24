"""Export a read-only reconciliation prompt/context review; never invoke a model."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime
from html import escape
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

try:
    from markdown_it import MarkdownIt
except ImportError:
    sys.path.append('/Library/Frameworks/Python.framework/Versions/3.7/lib/python3.7/site-packages')
    from markdown_it import MarkdownIt
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch
from cortex.capabilities.graph_merge.consolidation.review import Review
from cortex.capabilities.graph_merge.llm.task_tools import build_read_tools
from cortex.schema import load_root_schema

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = REPO / 'output/reconciliation_review_20261120'
SOURCE = REPO / 'src/cortex/capabilities/graph_merge'
PROMPTS = SOURCE / 'llm/prompts'
MODULES = {
    'writer-role': 'incremental_relation_write.md',
    'assessment-role': 'incremental_relation_assess.md',
    'shared': 'incremental_relation_shared.md',
    'conflict': 'conflict_contract.md',
    'protocol': 'incremental_protocol.md',
}
CONTEXT_SOURCES = ['llm/task_agent.py', 'llm/packets.py', 'llm/task_tools.py',
    'llm/recovery.py', 'llm/transport.py', 'llm/input_budget.py',
    'llm/relation_reads.py', 'integration/relation_context.py',
    'integration/task_runner.py', 'integration/node_relation_repair.py',
    'integration/relation_followups.py', 'integration/review.py',
    'integration/relation_edits.py', 'integration/patch.py']
ROLES = [('writer', 'Reconciliation Writer'), ('assessment', '独立 Reconciliation Assessment')]
ATTEMPTS = [('initial', REPO / 'output/node_consolidation_smoke_20261120_live/carcinogenic_molecule_count'),
            ('resume', REPO / 'output/node_consolidation_smoke_20261120_retry_live/carcinogenic_molecule_count')]
OUT.mkdir(parents=True, exist_ok=True)


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def original_system(role):
    names = [MODULES[role + '-role'], MODULES['shared'], MODULES['conflict'], MODULES['protocol']]
    return '\n\n'.join((PROMPTS / name).read_text() for name in names)


def translated_system(role):
    keys = [role + '-role', 'shared', 'conflict', 'protocol']
    return '\n\n'.join((HERE / 'prompts' / (key + '.zh.md')).read_text() for key in keys)


snapshots = {}
for name in list(MODULES.values()):
    data = (PROMPTS / name).read_bytes()
    target = OUT / 'source_snapshot/prompts' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    snapshots[str((PROMPTS / name).relative_to(REPO))] = sha(data)
for name in CONTEXT_SOURCES:
    data = (SOURCE / name).read_bytes()
    target = OUT / 'source_snapshot' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    snapshots[str((SOURCE / name).relative_to(REPO))] = sha(data)

ctx = SimpleNamespace(schema=load_root_schema(), evidence=(), log=lambda name, args, result: result)
registry = build_read_tools(ctx, {})
function = next(item for item in ast.walk(ast.parse((SOURCE / 'llm/task_agent.py').read_text()))
                if isinstance(item, ast.FunctionDef) and item.name == 'submit_patch')
result_tool = StructuredTool.from_function(lambda **kwargs: None, name='submit_patch',
    description=ast.get_docstring(function), args_schema=MergePatch)
tool_counts = {}
for role, final in [('writer', result_tool), ('assessment', Review)]:
    tools = [convert_to_openai_tool(item) for item in [*registry.values(), final]]
    save(OUT / (role + '.tools.actual.json'), tools)
    tool_counts[role] = {'names': [item['function']['name'] for item in tools],
                        'serialized_characters': len(json.dumps(tools, ensure_ascii=False))}
for name, model in [('MergePatch', MergePatch), ('Review', Review)]:
    save(OUT / (name + '.schema.actual.json'), model.model_json_schema())
for topic in ('concept', 'metric', 'event', 'generation_rule', 'relations', 'operations', 'conflicts'):
    save(OUT / 'guidance' / (topic + '.actual.json'), registry['read_guidance'].invoke({'topic': topic}))

# Preserve every real request from the selected two relationship episodes.
requests = []
for attempt, directory in ATTEMPTS:
    events = [json.loads(line) for line in (directory / 'model_calls.jsonl').read_text().splitlines() if line.strip()]
    counters, budgets, advertised = {}, {}, {}
    for line, event in enumerate(events, 1):
        episode = event.get('episode_id')
        if event.get('event_type') == 'request_budget':
            budgets[episode] = event
        if event.get('event_type') == 'request_contract':
            advertised[episode] = event
        if event.get('event_type') != 'model_start' or event.get('component') not in {'integration_reconcile', 'integration_graph_assess'}:
            continue
        role = 'writer' if event['component'] == 'integration_reconcile' else 'assessment'
        counters[role] = counters.get(role, 0) + 1
        index = counters[role]
        filename = f'actual_requests/{attempt}.{role}.round{index}.json'
        messages = event['messages']
        body = {'kind': 'actual_logged_request', 'source': str((directory / 'model_calls.jsonl').relative_to(REPO)),
                'source_event_line': line, 'attempt': attempt, 'role': role, 'episode_id': episode,
                'messages': messages, 'tool_names': event.get('tool_names'),
                'request_contract': advertised.get(episode), 'request_budget': budgets.get(episode)}
        save(OUT / filename, body)
        actual_system = messages[0]['content']
        row = {'attempt': attempt, 'role': role, 'round': index, 'file': filename,
               'messages': len(messages), 'message_characters': sum(len(str(item.get('content', ''))) for item in messages),
               'system_characters': len(actual_system), 'system_sha256': sha(actual_system),
               'matches_current_system': actual_system == original_system(role),
               'request_budget_characters': budgets.get(episode, {}).get('characters'),
               'tool_names': (advertised.get(episode) or {}).get('tool_names') or event.get('tool_names') or 'not_reported'}
        requests.append(row)
        if index == 1:
            # This is the actual User packet, not a guessed template.
            save(OUT / f'{attempt}.{role}.user.actual.json', json.loads(messages[1]['content']))
    state_path = directory / 'task_state.json'
    if state_path.exists():
        record = json.loads(state_path.read_text()).get('coupled_relation') or {}
        if record.get('patch'):
            MergePatch.model_validate(record['patch'])
            save(OUT / (attempt + '.writer.result.actual.json'), record['patch'])
        if record.get('review'):
            Review.model_validate(record['review'])
            save(OUT / (attempt + '.assessment.result.actual.json'), record['review'])
assert requests and all(item['matches_current_system'] for item in requests), 'Historical requests must not be presented as current System without disclosure'
save(OUT / 'requests_manifest.json', requests)

metrics = {}
for role, _ in ROLES:
    original, chinese = original_system(role), translated_system(role)
    (OUT / (role + '.system.original.md')).write_text(original)
    (OUT / (role + '.system.zh.md')).write_text(chinese)
    metrics[role] = {'original_characters': len(original), 'translated_characters': len(chinese),
                     'original_sha256': sha(original), 'translated_sha256': sha(chinese),
                     'module_order': [role + '-role', 'shared', 'conflict', 'protocol']}
for key in MODULES:
    destination = OUT / 'prompts' / (key + '.zh.md')
    destination.parent.mkdir(exist_ok=True)
    shutil.copy2(HERE / 'prompts' / (key + '.zh.md'), destination)
for path in (HERE / 'guidance').glob('*.zh.md'):
    shutil.copy2(path, OUT / 'guidance' / path.name)

md = MarkdownIt('commonmark').enable('table')
documents = [('overview', '总体与阅读入口', 'report.md'), ('contracts', '输入、输出与关系义务', 'contracts-review.md'),
             ('context', '每轮上下文与真实请求', 'context-review.md'), ('tools', '工具说明完整中文对照', 'tools.zh.md'),
             ('translation', '译文范围与原文疑点', 'translation-notes.md')]
link_map = {name: '#' + ident for ident, _, name in documents}


def render(name):
    text = (HERE / name).read_text()
    for target, anchor in link_map.items():
        text = text.replace('](' + target + ')', '](' + anchor + ')')
    # Code references remain plain paths with line numbers, not broken local URLs.
    return md.render(text)


style = '''body{font:16px/1.85 system-ui,"PingFang SC",sans-serif;background:#f4f7fa;color:#203047;margin:0}main{max-width:1180px;margin:auto;padding:24px}nav{position:sticky;top:0;z-index:2;background:#fff;padding:12px 24px;border-bottom:1px solid #d8e0ea;display:flex;gap:16px;flex-wrap:wrap}a{color:#1762b8}section{background:#fff;border:1px solid #d8e0ea;border-radius:10px;padding:24px;margin:22px 0;scroll-margin-top:100px}h1,h2,h3{line-height:1.5;scroll-margin-top:100px}h1{font-size:26px}h2{font-size:22px}h3{font-size:18px}table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #d8e0ea;padding:9px;text-align:left;vertical-align:top}th{background:#edf2f8}pre{background:#edf2f8;padding:16px;white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.7 ui-monospace,monospace}code{font-size:.9em}details{border:1px solid #d8e0ea;border-radius:7px;margin:14px 0;padding:14px}summary{cursor:pointer;font-weight:650}.status{background:#fff4d9;border:1px solid #d6bb76;border-radius:8px;padding:14px}.muted{color:#53667b}@media(max-width:700px){main{padding:12px}section{padding:14px}table{display:block;overflow:auto}}'''
page = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>Reconciliation 两个 Agent · 当前完整 Prompt 与上下文复阅</title><style>' + style + '</style></head><body><nav>']
for ident, title, _ in documents:
    page.append(f'<a href="#{ident}">{escape(title)}</a>')
page += ['<a href="#systems">两份完整中文 System</a><a href="#guidance">按需 Guidance</a><a href="#requests">逐轮真实请求</a></nav><main>',
         '<h1>Reconciliation Writer 与独立 Assessment：当前实现复阅</h1>',
         '<p class="status">只读整理：忠实保留当前规则，未精简或修改运行 Prompt、工具、checkpoint 或后台进程。本轮没有新模型调用。译文与建议分开，原文疑点不在译文中悄悄修正。</p>']
for ident, _, name in documents:
    page.append(f'<section id="{ident}">{render(name)}</section>')
page.append('<section id="systems"><h1>两份实际完整 System 的中文译文</h1><p>每份均按运行顺序完整拼接四个模块；下载文件不省略共享部分。工具定义、User 任务和按需 Guidance 不属于这份 System，分别列出。</p>')
for role, title in ROLES:
    value = metrics[role]
    page.append(f'<details id="system-{role}"><summary>{escape(title)} · 完整中文 System</summary><p>{value["original_characters"]} 原文字符 → {value["translated_characters"]} 译文字符；不是 token。<a href="{role}.system.zh.md">完整中文</a> · <a href="{role}.system.original.md">实际原文</a> · <a href="{role}.tools.actual.json">当前完整工具定义</a></p>')
    page.append(md.render(translated_system(role)) + '</details>')
page.append('<h2>五个源模块译文</h2>')
for key, source in MODULES.items():
    page.append(f'<details><summary>{escape(source)}</summary><p><a href="prompts/{key}.zh.md">单独译文</a> · <a href="source_snapshot/prompts/{source}">原文快照</a></p>{md.render((HERE / "prompts" / (key + ".zh.md")).read_text())}</details>')
page.append('</section><section id="guidance"><h1>按需读取后才进入上下文的 Guidance</h1><p>这些不是初始 System 的类型尾部。四类 guidance 还会带实际 fields schema；中文译文翻译自然语言规则，原始 JSON 完整保留 schema。conflicts 与 System 冲突模块同源，不另造一份不同译文。</p>')
for topic in ('concept', 'metric', 'event', 'generation_rule', 'relations', 'operations', 'conflicts'):
    page.append(f'<details><summary>read_guidance({topic})</summary><p><a href="guidance/{topic}.actual.json">实际返回 JSON</a></p>')
    path = HERE / 'guidance' / (topic + '.zh.md')
    if path.exists():
        page.append(f'<p><a href="guidance/{topic}.zh.md">完整中文指导</a></p>' + md.render(path.read_text()))
    elif topic == 'conflicts':
        page.append('<p>同上面的 conflict_contract 完整译文。</p>')
    else:
        raise ValueError('Missing guidance translation: ' + topic)
    page.append('</details>')
page.append('</section><section id="requests"><h1>真实请求逐轮目录</h1><p>来自先前 Metric 小样本的两轮记录；所有请求的 System 与当前源文件字节一致。第一次包含关系 Writer，恢复轮复用已保存 Patch，仅补独立 Assessment。真实请求全部原样导出，不把重建模板冒充日志。</p>')
page.append('<table><tr><th>记录</th><th>角色/轮次</th><th>消息数</th><th>消息正文字符</th><th>记录的完整请求预算字符</th><th>完整请求</th></tr>')
for item in requests:
    page.append('<tr>' + ''.join(f'<td>{escape(str(value))}</td>' for value in (item['attempt'], item['role'] + '/' + str(item['round']), item['messages'], item['message_characters'], item['request_budget_characters'])) + f'<td><a href="{item["file"]}">JSON</a></td></tr>')
page.append('</table><p><a href="requests_manifest.json">请求索引与 System SHA</a> · <a href="context_metrics.json">详细上下文测量</a></p><h2>首轮实际 User 与结果</h2>')
for attempt in ('initial', 'resume'):
    for role, _ in ROLES:
        for kind in ('user', 'result'):
            name = f'{attempt}.{role}.{kind}.actual.json'
            path = OUT / name
            if path.exists():
                page.append(f'<details><summary>{escape(name)}</summary><p><a href="{name}">原始 JSON</a></p><pre>{escape(path.read_text())}</pre></details>')
page.append('</section>')
for relative, digest in snapshots.items():
    assert sha((REPO / relative).read_bytes()) == digest, 'Runtime source changed during report generation: ' + relative
manifest = {'created_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
    'mode': 'read_only_review', 'runtime_modified': False, 'new_model_calls': 0,
    'system_metrics': metrics, 'tool_metrics': tool_counts, 'source_sha256': snapshots,
    'actual_request_count': len(requests), 'all_logged_systems_match_current': True,
    'translation_manifest': 'translation-manifest.json'}
save(OUT / 'manifest.json', manifest)
shutil.copy2(HERE / 'translation-manifest.json', OUT / 'translation-manifest.json')
page.append('<section id="validation"><h1>报告校验与边界</h1><p>已校验原文快照未改变、两份 System 按实际顺序拼接、日志 System 完全匹配、原始 Patch/Review 可通过当前公开模型 schema。未调用模型，未测试或修改运行行为。</p><pre>' + escape(json.dumps(manifest, ensure_ascii=False, indent=2)) + '</pre></section></main></body></html>')
(OUT / 'index.html').write_text(''.join(page))
print(json.dumps({'html': str(OUT / 'index.html'), 'actual_requests': len(requests), 'runtime_modified': False,
                  'system_characters': metrics, 'new_model_calls': 0}, ensure_ascii=False))
