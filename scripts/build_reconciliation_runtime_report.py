"""Refresh the local implementation/small-probe report without invoking models."""
import argparse
from datetime import datetime
from html import escape
import json
from pathlib import Path
from zoneinfo import ZoneInfo

NAMES = ('healthy_parallel', 'abstract_predicate', 'metric_retirement',
         'metadata_alias', 'compatible_alternatives')


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def live_label(result):
    if result.get('status') == 'passed':
        return '通过独立 Assessment'
    errors = ' '.join(str(row.get('type', '')) + ' ' + str(row.get('message', ''))
                      for row in result.get('errors', []))
    if 'ModelBudgetExceeded' in errors:
        return '未完成：达到小测调用上限；已保存检查点'
    if 'Timeout' in errors or 'timeout' in errors:
        return '未完成：模型请求超时；已保存检查点'
    return result.get('status', '未执行')


def build(root):
    validation = read(root / 'implementation_validation.json')
    attempts = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not (directory / 'summary.json').exists():
            continue
        summary = read(directory / 'summary.json')
        if summary.get('mode') not in {'live', 'deterministic'}:
            continue
        attempts.append((directory, summary))
    attempts.sort(key=lambda item: item[1].get('created_at', ''))
    def results(name, mode):
        found = []
        for directory, summary in attempts:
            if summary.get('mode') != mode:
                continue
            item = read(directory / name / 'result.json')
            if item:
                found.append((directory, item))
        return found
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>Reconciliation 实现与小范围验证</title>',
        '<style>body{font:16px/1.8 system-ui;margin:28px auto;max-width:1120px;padding:0 20px;color:#213047}',
        'table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd6e2;padding:10px;text-align:left}',
        'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f8;padding:16px}a{color:#175daf}</style>',
        '<h1>Reconciliation 实现与小范围验证</h1>', f'<p>更新时间：{escape(stamp)}</p>',
        '<p>已接入局部工具、版本草稿、独立评估、来源/分支编译与检查点恢复。样例自动取自既有 case-level graphs；',
        '受控修改逐项记录，不将注入故障归因为原数据错误。后台数据库 merge runtime 尚未切换。</p>',
        '<p><a href="../../docs/design/reconciliation-runtime.md">实现说明</a> · ',
        '<a href="../../src/cortex/capabilities/graph_merge/llm/prompts/reconciliation/README.md">实际 Prompt 修改入口</a> · ',
        '<a href="implementation_validation.json">完整验证记录</a></p>',
        '<h2>代码与恢复验证</h2>',
        f'<p>专项测试：{validation.get("targeted_tests", {}).get("passed", "待记录")} 项通过。覆盖八类操作、',
        '本地权限、原子读取、分支来源、候选差量、完整事务及中断恢复；未运行全仓库或全数据库测试。</p>',
        '<h2>局部样例</h2><table><tr><th>样例</th><th>离线生产事务</th><th>真实调用</th><th>记录</th></tr>']
    overview = []
    for name in NAMES:
        offline, live = results(name, 'deterministic'), results(name, 'live')
        offline_status = offline[-1][1]['status'] if offline else '未执行'
        live_status = live_label(live[-1][1]) if live else '本轮未选'
        links = []
        for mode, selected in (('离线', offline[-1:] if offline else []), ('真实', live[-1:] if live else [])):
            for directory, result in selected:
                links.append(f'<a href="{directory.name}/index.html">{mode}报告</a>')
                if (directory / name / 'result.json').exists():
                    links.append(f'<a href="{directory.name}/{name}/result.json">{mode}结果</a>')
        parts.append('<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in (name, offline_status, live_status))
                     + '<td>' + ' · '.join(links) + '</td></tr>')
        overview.append({'name': name, 'deterministic': offline_status, 'live': live_status})
    parts += ['</table><h2>各轮记录与调用预算</h2><ul>']
    for directory, summary in attempts:
        parts.append(f'<li><a href="{directory.name}/index.html">{escape(directory.name)}</a> · '
                     + escape(summary['mode']) + ' · <code>'
                     + escape(json.dumps(summary.get('budgets', {}), ensure_ascii=False)) + '</code></li>')
    parts += ['</ul><h2>本轮结论与限制</h2><ul>']
    for finding in validation.get('findings', []):
        parts.append('<li>' + escape(finding) + '</li>')
    parts += ['</ul><p>离线代理使用生产工具和编译路径验证结构/来源，不作为真实模型语义能力的证明。',
        '真实调用通过独立 Assessment 后仍检查结构与来源不变量；需要人工判断的等义结构不会伪报为通过。</p>',
        '<h2>保留记录与能力边界</h2><p>原始 case graphs、原生产 checkpoint 与失败轮次均保留。',
        '测试检查点在独立目录写入；恢复已有候选只续未完成评估。',
        '终点分支选择及仅关系不同的同正文 alternatives 仍明确报告表示能力缺口。</p>',
        '<details><summary>验证范围与源文件指纹</summary><pre>'
        + escape(json.dumps(validation, ensure_ascii=False, indent=2)) + '</pre></details></html>']
    (root / 'index.html').write_text(''.join(parts))
    (root / 'report_summary.json').write_text(json.dumps({'updated_at': stamp, 'cases': overview,
        'attempts': [directory.name for directory, _ in attempts]}, ensure_ascii=False, indent=2) + '\n')
    return root / 'index.html'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
        default=Path(__file__).resolve().parents[1] / 'output/reconciliation_runtime_20261120')
    print(build(parser.parse_args().output.resolve()))
