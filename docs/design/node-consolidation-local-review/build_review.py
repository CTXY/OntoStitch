"""Assemble type-specific review prompts; never updates production prompt loading."""

import hashlib
import json
import re
import sys
from datetime import datetime
from html import escape
from pathlib import Path

try:
    from markdown_it import MarkdownIt
except ImportError:
    sys.path.append('/Library/Frameworks/Python.framework/Versions/3.7/lib/python3.7/site-packages')
    from markdown_it import MarkdownIt

from cache_layout import assemble_system, check_layout, draft_tools, messages, modules
from validate_examples import make_input_example, validate_examples

from cortex.schema import load_root_schema

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = REPO / 'output/node_consolidation_local_review_20261120'
OUT.mkdir(parents=True, exist_ok=True)
renderer = MarkdownIt('commonmark').enable('table')
types = [('concept', 'Concept'), ('generation_rule', 'GenerationRule'), ('metric', 'Metric'), ('event', 'Event')]
roles = [('writer', '节点整合 Writer'), ('assessment', '独立节点 Assessment')]
documents = [
    ('cache', '四类型共用前缀、缓存与审查', 'cache-and-review.md'),
    ('agents', 'Sub-agent 审查记录', 'subagent-review.md'),
    ('changes', '本版说明、格式与代码差距', 'format-and-guidance-review.md'),
    ('overview', '局部输入、User Prompt 与上下文', 'review.md'),
]
source_sections = [
    ('writer-base', 'Writer 角色/输入模块', 'writer.system.zh.md'),
    ('assessment-base', 'Assessment 角色/输入模块', 'assessment.system.zh.md'),
    ('common', '通用字段与整合决策', 'common-fields.zh.md'),
    ('writer-format', 'Writer 输出填写契约', 'writer-output-contract.zh.md'),
    ('assessment-format', 'Assessment 输出填写契约', 'assessment-output-contract.zh.md'),
] + [('type-' + kind, title + ' guidance', 'types/' + kind + '.zh.md') for kind, title in types]
link_map = {filename: '#' + ident for ident, _, filename in documents + source_sections}


def render_source(filename):
    source = (HERE / filename).read_text()
    for target, local in link_map.items():
        source = source.replace(f']({target})', f']({local})')
    return renderer.render(source)


validation = validate_examples()
for ident, _, filename in documents:
    for block in re.findall(r'```json\n(.*?)\n```', (HERE / filename).read_text(), re.DOTALL):
        json.loads(block)
validation['proposed_input_templates_json_validated'] = 2
input_examples = make_input_example()
for filename, value in input_examples.items():
    (OUT / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2))
validation['concrete_input_output_files'] = len(input_examples)
cache_validation = check_layout(input_examples['writer.concept.user.example.json'], input_examples['assessment.concept.user.example.json'])
for role in ('writer', 'assessment'):
    (OUT / (role + '.tools.proposed.json')).write_text(json.dumps(draft_tools(role), ensure_ascii=False, indent=2))
    example_messages = messages(role, 'concept', input_examples[role + '.concept.user.example.json'])
    (OUT / (role + '.concept.messages.proposed.json')).write_text(json.dumps(example_messages, ensure_ascii=False, indent=2))
styles = '''
body{margin:0;color:#1c2b40;background:#f7f9fc;font:16px/1.85 system-ui,"PingFang SC",sans-serif}
main{max-width:1160px;margin:auto;padding:26px}nav{position:sticky;top:0;background:#fff;
padding:14px 24px;border-bottom:1px solid #d4dfeb;display:flex;flex-wrap:wrap;gap:18px;z-index:1}
a{color:#175cb5}h1,h2,h3{line-height:1.5;scroll-margin-top:130px}h1{font-size:27px}
h2{margin-top:32px;font-size:22px}h3{font-size:19px}li{margin:7px 0}
section{background:#fff;border:1px solid #d4dfeb;border-radius:10px;padding:26px;margin:24px 0;scroll-margin-top:130px}
table{width:100%;border-collapse:collapse}td,th{border:1px solid #d4dfeb;padding:10px;text-align:left}
th{background:#eef3f9}td{overflow-wrap:anywhere}pre{background:#eef3f9;padding:18px;
white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.75 ui-monospace,monospace}
code{font-size:.9em}blockquote{border-left:4px solid #91aed0;padding:3px 18px;margin-left:0;background:#f7f9fc}
.status{padding:12px 18px;background:#fff4d7;border:1px solid #ddc981;border-radius:8px}
details{border:1px solid #d4dfeb;border-radius:7px;padding:16px;margin:16px 0;scroll-margin-top:130px}
summary{cursor:pointer;font-weight:650}.metrics{color:#53637c;font-size:14px}
@media(max-width:650px){main{padding:12px}section{padding:16px}table{display:block;overflow:auto}}
@media print{nav{display:none}section{break-before:page;border:none;padding:0}body{background:#fff}}
'''
page = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>节点整合 Prompt 第三版 · 共用前缀与类型 Guidance</title>', f'<style>{styles}</style></head><body><nav>']
for ident, label, _ in documents:
    page.append(f'<a href="#{ident}">{escape(label)}</a>')
for kind, title in types:
    page.append(f'<a href="#assembled-{kind}">{title} 完整 Prompts</a>')
page.append('<a href="#modules">模块</a><a href="#outputs">输出示例</a><a href="#validation">验证</a></nav><main>')
page.append('<h1>节点整合：四类型共用前缀、类型尾部与独立审查</h1>')
page.append('<p class="status">第三版待 Review 草案。生产 Prompt、工具、checkpoint 与后台 runtime 未切换。每个 group 只发送当前角色与唯一类型的一份 System；缓存检查仅验证前缀布局，不是供应商实际命中率。</p>')
page.append('<p>完整填写的教学示例（使用真实生成的来源句柄，原始 evidence 正文未注入）：')
for filename in input_examples:
    page.append(f'<a href="{filename}">{escape(filename)}</a> · ')
page.append('输入 envelope 仍是拟议局部接口，底层节点/关系与整合结果经过现有代码验证。</p>')
for ident, _, filename in documents:
    page.append(f'<section id="{ident}">{render_source(filename)}</section>')

schema = load_root_schema()
metrics = {}
for kind, title in types:
    page.append(f'<section id="assembled-{kind}"><h1>{title} group：完整 System</h1>')
    node_schema = {'node_type': kind, 'fields': {key: value.model_dump(mode='json')
                   for key, value in schema.effective_node_fields(kind).items()}}
    schema_name = kind + '.node_schema.json'
    (OUT / schema_name).write_text(json.dumps(node_schema, ensure_ascii=False, indent=2))
    page.append(f'<p><a href="{schema_name}">本类别当前实际 node_schema 字段</a>。该 schema 应随 User 材料发送；示例与未来 packet 字段不代表已接入 runtime。</p>')
    for role, label in roles:
        names = modules(role, kind)
        text = assemble_system(role, kind)
        filename = role + '.' + kind + '.system.zh.md'
        (OUT / filename).write_text(text)
        metrics[role + ':' + kind] = {'files': names, 'characters': len(text), 'sha256': hashlib.sha256(text.encode()).hexdigest()}
        page.append(f'<details id="{role}-{kind}"' + (' open' if role == 'writer' and kind == 'concept' else '') + f'><summary>{label} · {title} · 完整文本</summary>')
        page.append(f'<p class="metrics">{len(text)} 字符；仅字符计数，不等同于 token 或提速验证。<a href="{filename}">下载完整 System</a></p>')
        page.append(renderer.render(text) + '</details>')
    page.append('</section>')

page.append('<section id="modules"><h1>按职责拆开的源模块</h1><p>用于定位规则；实际发送的是上面的按类型完整 System，模块不重复注入。</p>')
for ident, title, filename in source_sections:
    page.append(f'<details id="{ident}"><summary>{escape(title)}</summary>{render_source(filename)}</details>')
page.append('</section>')
examples = json.loads((HERE / 'output_examples.json').read_text())
page.append('<section id="outputs"><h1>公开输出示例</h1><p>均为教学数据；ID 与 source unit 是占位，不代表真实任务记录。各示例独立；验证器对局部编译样例替换为实际生成的来源句柄。</p>')
for name, value in examples.items():
    if name == 'note':
        continue
    page.append(f'<details><summary>{escape(name)}</summary><pre>{escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre></details>')
page.append('</section><section id="validation"><h1>验证结果与边界</h1>')
page.append('<p>使用现有真实验证函数，不调用模型、不读取 API key、不迁移 checkpoint。不是完整 task runner 回归或真实模型效果验证。</p>')
page.append('<pre>' + escape(json.dumps(validation, ensure_ascii=False, indent=2)) + '</pre>')
page.append('<h2>缓存布局检查</h2><pre>' + escape(json.dumps(cache_validation, ensure_ascii=False, indent=2)) + '</pre></section></main></body></html>')
destination = OUT / 'index.html'
if destination.exists() and not (OUT / 'v1_index.html').exists():
    (OUT / 'v1_index.html').write_text(destination.read_text())
if destination.exists() and not (OUT / 'v2_index.html').exists():
    (OUT / 'v2_index.html').write_text(destination.read_text())
destination.write_text(''.join(page))
(OUT / 'validation.json').write_text(json.dumps(validation, ensure_ascii=False, indent=2))
(OUT / 'cache_validation.json').write_text(json.dumps(cache_validation, ensure_ascii=False, indent=2))
(OUT / 'manifest.json').write_text(json.dumps({'status': 'review_draft_not_deployed',
    'created_at': datetime.now().astimezone().isoformat(timespec='seconds'), 'roles_by_type': metrics,
    'cache_layout': cache_validation}, ensure_ascii=False, indent=2))
print(json.dumps({'html': str(destination), 'assembled_systems': len(metrics), 'validation': validation,
                  'cache_layout_status': cache_validation['status']}, ensure_ascii=False))
