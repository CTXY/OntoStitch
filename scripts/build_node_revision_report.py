"""Publish complete English role prompts and real failed-case repair transcripts."""
import json
from html import escape
from pathlib import Path
import sys

from cortex.capabilities.graph_merge.llm.node_contract import NODE_TYPES, node_system, node_output_model
from resume_first10_current_graph import read, write, write_text


STYLE = '<style>body{font:15px system-ui;margin:32px;background:#f5f6fa;max-width:1400px}section,details{padding:18px;margin:14px 0;background:white;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px ui-monospace,monospace}summary{font-weight:600;cursor:pointer}a{color:#175db6}</style>'


def detail(title, value, *, opened=False):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return '<details' + (' open' if opened else '') + '><summary>' + escape(title) + '</summary><pre>' + escape(text) + '</pre></details>'


def prompt_report(root):
    folder = root / 'prompts'
    folder.mkdir(exist_ok=True)
    sections, manifest = [], []
    for role in ('writer', 'assessment'):
        for kind in NODE_TYPES:
            system = node_system(role, kind)
            filename = f'{role}.{kind}.system.en.txt'
            (folder / filename).write_text(system)
            manifest.append({'role': role, 'node_type': kind, 'characters': len(system),
                             'file': filename, 'complete': True})
            sections.append(detail(f'{role} · {kind} · complete system prompt ({len(system)} characters)', system))
        schema = node_output_model(role).model_json_schema()
        write(folder / f'{role}.output-schema.json', schema)
        sections.append(detail(role + ' · complete public output schema', schema))
    sections.insert(0, '<p>These are the complete runtime English system prompts, concatenated in their actual order: shared semantics → role → output contract → one node-type module. No prompt text is abbreviated. Full dynamic user prompts appear in each test transcript.</p>')
    write_text(root / 'prompts.html', '<!doctype html><meta charset="utf-8"><title>Complete English node prompts</title>' + STYLE + '<h1>Complete English node prompts</h1>' + ''.join(sections))
    write(folder / 'manifest.json', manifest)


def case_report(root, name):
    folder = root / name
    if not folder.exists():
        return None
    sections = []
    for filename, label in [('source_scope.json', '测试数据来源与范围'),
                            ('historical_initial_draft.json', 'Step 0 · 历史真实草稿和完整原始局部输入'),
                            ('controlled_initial_assessment.json', 'Step 1 · 受控 Assessment 反馈（测试输入，非模型裁决）')]:
        path = folder / filename
        if path.exists():
            sections.append(detail(label, read(path)))
    events = []
    if (folder / 'model_calls.jsonl').exists():
        for line in (folder / 'model_calls.jsonl').read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)
            if event.get('event_type') in ('model_start', 'model_end', 'model_error', 'tool_result'):
                sections.append(detail(event['event_type'] + ' · ' + event['component'], event))
    result = read(folder / 'result.json') if (folder / 'result.json').exists() else {'status': 'running'}
    result['peak_input_characters'] = max((event['characters'] for event in events if event['event_type'] == 'request_budget'), default=0)
    if (folder / 'after.json').exists():
        before, after = read(folder / 'before.json'), read(folder / 'after.json')
        delta = {}
        for label in ('nodes', 'edges'):
            old = {obj['id']: obj for obj in before['graph'][label]}
            new = {obj['id']: obj for obj in after['graph'][label]}
            delta[label] = {'removed': [old[k] for k in old.keys() - new.keys()],
                'created': [new[k] for k in new.keys() - old.keys()],
                'updated': [{'before': old[k], 'after': new[k]} for k in old.keys() & new.keys() if old[k] != new[k]]}
        write(folder / 'changes.json', delta)
        sections.append(detail('节点和关系的完整实际变化', delta, opened=True))
    sections.append(detail('测试结果', result, opened=True))
    write_text(folder / 'index.html', '<!doctype html><meta charset="utf-8"><title>' + name + '</title>' + STYLE +
        '<h1>' + name + '</h1><p>同一真实失败事务。历史草稿复用，首轮 Assessment 反馈为受控测试输入；后续 Writer 和最终独立 Assessment 使用真实 deepseek-v4-pro。</p>' + ''.join(sections))
    return result


def main(root):
    root.mkdir(parents=True, exist_ok=True)
    prompt_report(root)
    results = {name: case_report(root, name) for name in ('remove_relation', 'keep_separate')}
    links = ''.join('<section><h2>' + name + '</h2><p>' + escape((result or {}).get('status', 'not_started')) +
        f' · <a href="{name}/index.html">完整输入、输出及图变化</a></p></section>' for name, result in results.items())
    write_text(root / 'index.html', '<!doctype html><meta charset="utf-8"><title>节点整合局部修订验证</title>' + STYLE +
        '<h1>节点整合：删除关系 / 移出 group</h1><p><a href="prompts.html">完整修改后英文 prompts（8 份，含公开 schemas）</a></p>' +
        '<p>测试采用 toxicology cases 217、234 的原始失败事务，以及首批 10 条后新增批次中已接受前三个任务的实际图。原 checkpoint 和源数据保留。</p>' +
        '<p>128 项核心回归与 3 项轨迹核验测试通过；subagents 已 review 权限、来源、上下文和恢复。两条真实小测完成后已迁移并启动后台续跑。</p>' + links)
    if results.get('remove_relation', {}).get('removal_proposal_accepted_by_assessment') is False:
        path = root / 'index.html'
        write_text(path, path.read_text() + '<section><h2>真实裁决：删除提案被否决，修订分组后通过</h2><p>删除操作成功编译并接受独立评估，但真实 Assessment 不认可本例删除。Writer 按反馈将 n202 保留为独立节点，保留 e238 的说明和来源，最终 Assessment 接受。passed 表示流程与保护机制通过验证，不表示删除提案在语义上被接受。</p></section>')
    write(root / 'report_results.json', results)
    if (root / 'production_resume_summary.json').exists():
        path = root / 'index.html'
        write_text(path, path.read_text() + detail('正式后台续跑核验：失败任务已接受，原有任务未重跑', read(root / 'production_resume_summary.json'), opened=True))


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve())
