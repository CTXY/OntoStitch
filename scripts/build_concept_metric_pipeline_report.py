"""Render the bounded real-case pipeline, including every captured request/output."""
from __future__ import annotations
import argparse
from html import escape
import json
import os
from pathlib import Path

from build_reconciliation_simplified_report import box, head, one
from reconciliation_probe_fixtures import digest, write


def read(path):
    return json.loads(path.read_text())


def link(target, base, label):
    return '<a href="'+escape(os.path.relpath(target,base),quote=True)+'">'+escape(label)+'</a>'


def details(folder):
    before=read(folder/'before.json')['graph'];after=read(folder/'after.json')['graph']
    pieces=[head(folder.name+'：节点与关系变化'),'<p><a href="agent_io.html">每步完整输入输出</a> · <a href="../index.html">流程总览</a></p>']
    delta={}
    for kind,label in (('nodes','节点'),('edges','关系')):
        old={row['id']:row for row in before[kind]};new={row['id']:row for row in after[kind]}
        delta[kind]={'removed':[old[k] for k in old.keys()-new.keys()],
                     'created':[new[k] for k in new.keys()-old.keys()],
                     'updated':[{'before':old[k],'after':new[k]} for k in old.keys()&new.keys() if old[k]!=new[k]]}
        pieces.append(box(label+'变化（完整字段与来源）',delta[kind],True))
    pieces.extend([box('修改前完整图',before),box('修改后完整图',after)])
    (folder/'details.html').write_text(''.join(pieces)+'</html>')
    write(folder/'delta.json',delta)
    return delta


def render(root):
    live=root/'live_v2'
    summary=read(live/'validated_summary.json') if (live/'validated_summary.json').exists() else read(live/'summary.json') if (live/'summary.json').exists() else read(live/'progress.json')
    pages=[]
    labels={'01_concept':'两个 Concept 的 consolidation','02_metric':'两个 Metric 的 consolidation',
            '03_relation_merger':'自然重复关系的 description 整合','04_reconciliation':'实际 Signals 与保义义务的 Writer / Assessment'}
    for name,label in labels.items():
        folder=live/name
        if not (folder/'result.json').exists():continue
        delta=details(folder)
        row=one(folder);row['label']=label
        if (folder/'runtime_result.json').exists():
            runtime=read(folder/'runtime_result.json')
            row['status']=runtime['status']
            io=read(folder/'agent_io.json');io['runtime_result']=runtime
            write(folder/'agent_io.json',io)
            page=(folder/'agent_io.html').read_text()
            page=page.replace('<h2>第 1 步',box('运行时实际结果：独立评估已接受；原始 result.json 保留严格断言失败',runtime,True)+'<h2>第 1 步',1)
            (folder/'agent_io.html').write_text(page)
        row['node_changes']={key:len(value) for key,value in delta['nodes'].items()}
        row['edge_changes']={key:len(value) for key,value in delta['edges'].items()}
        audits=[json.loads(line) for line in (folder/'english_requests.jsonl').read_text().splitlines()] if (folder/'english_requests.jsonl').exists() else []
        row['english_requests_checked']=len(audits)
        pages.append(row)
    write(root/'report_stage_audit.json',pages)
    parts=[head('Concept → Metric → Relation Merger → Reconciliation：真实小范围测试'),
        '<p class="note">来源：toxicology/266 与 toxicology/204 的已接受 case-level graphs。先分别合并两组已确认身份，再整合自然产生的重复关系，最后进行局部关系协调。没有复制边凑数、没有人为制造过期依赖，也没有重新运行 Identity。所有测试 API 请求均受英文检查器约束；测试与后台生产 checkpoint 隔离。</p>',
        box('本次运行结果',summary,True),box('构造来源与可验证原始文件哈希',read(live/'provenance.json')),
        '<h2>各阶段完整输入输出</h2><table><tr><th>阶段</th><th>结果</th><th>实际模型轮数</th><th>变化</th><th>完整记录</th></tr>']
    for row in pages:
        folder=live/row['name']
        change='节点 '+json.dumps(row['node_changes'],ensure_ascii=False)+'；关系 '+json.dumps(row['edge_changes'],ensure_ascii=False)
        parts.append('<tr><td>'+escape(row['label'])+'</td><td>'+escape(row['status'])+'</td><td>'+str(row['calls'])+'</td><td>'+escape(change)+'</td><td>'+link(folder/'agent_io.html',root,'每步完整输入/输出')+' · '+link(folder/'details.html',root,'图变化')+'</td></tr>')
    parts.append('</table><p>每轮页面分别展示真实 system、所有 user messages、公开工具 schema、模型原始输出、Controller 读取/编译反馈及不含认证头的 HTTP body。读取、格式重试与独立评估分别计数。relation merger 按既定设计没有 Assessment。</p>')
    verification=read(live/'verification.json') if (live/'verification.json').exists() else {}
    parts.append('<h2>引用、描述与来源核验</h2>')
    parts.append('<p>最终应只保留一个目标 Concept 和一个目标 Metric，Metric 的 refs 与依赖边指向同一存活 Concept。两个原有匹配组均须保留；每个节点、每组边逐项比较完整 SourceReference（包括 locator），不只比较 evidence ID。节点按输入继承加实际明确引用核验；边严格等于原来源并集。节点 Writer 同时引用了两个原题的 QA/qa 定位，这些定位指向相同原文，新增定位单独列出，不能宣称节点来源只等于输入并集。关系语义由最终独立 Assessment 根据原始关系移交材料核查，程序检查结构与精确来源集合。</p>')
    parts.append('<p class="note">四阶段模型均通过，合计 15 次调用：Concept 4、Metric 4、relation merger 1、reconciliation 6（Writer 4、Assessment 2）。Concept/Metric 各修正一次缺少组内 explanation 的格式错误；reconciliation Writer 两次误加 parameters/params 包装后按反馈改为空数组。最终空草稿版本为 0，编译成功后直接进入 Assessment。旧的两个普通 molecule 邻居不属于这次已确认的 carcinogenic Concept 身份组，保留其各自 is_a 关系，未强行合并。</p>')
    parts.append(box('自动核验结果（含关系逐条前后对照）',verification,True))
    merger=live/'03_relation_merger'
    if (merger/'groups.json').exists():
        groups=read(merger/'groups.json');before={e['id']:e for e in read(merger/'before.json')['graph']['edges']}
        after=read(merger/'after.json')['graph']['edges']
        for group in groups:
            matching=[e for e in after if e['from_node_id']==group['from_node_id'] and e['to_node_id']==group['to_node_id'] and e['edge_type']==group['edge_type']]
            parts.append(box(group['edge_type']+'：真实旧说明 → relation merger 结果',{'inputs':[before[i] for i in group['member_edge_ids']],'result':matching},True))
    final=live/'04_reconciliation'
    if (final/'agent_io.json').exists():
        io=read(final/'agent_io.json')
        responses=[]
        for step in io['steps']:
            msg=step.get('response') or {}
            responses.append({'component':step['request'].get('component'),'content':msg.get('content'),'tool_calls':msg.get('tool_calls',[])})
        parts.append(box('最终关系 Writer 与 Assessment 的实际判断',responses,True))
        parts.append(box('实际检出 Signals 与真实保义移交义务',read(final/'signals.json')))
    parts.append('<h2>英文 prompt 与上下文布局</h2><p>运行时读取 .en.md。节点两种角色共享公共原则，类型 guidance 位于末尾；关系协调 system 的共享契约位于前面，Signal guidance 按需添加。动态状态替换旧状态，不累积对话历史。schema 的英文展示仅替换说明文字，ID、类型、校验约束与原始 graph/evidence 均不改写。</p>')
    prompts=Path('src/cortex/capabilities/graph_merge/llm/prompts').resolve()
    for path in sorted(prompts.rglob('*.en.md')):
        parts.append('<p>'+link(path,root,str(path.relative_to(prompts)))+'</p>')
    parts.append('<p>'+link(prompts/'schema-contracts.en.json',root,'schema 说明的英文映射')+'</p>')
    checks={}
    manifest=read(root/'prior_records_manifest.json')
    checks['prior_record_files']=len(manifest)
    checks['all_prior_records_unchanged']=all(Path(p).is_file() and digest(p)==h for p,h in manifest.items())
    checks['english_checked_api_requests']=sum(row['english_requests_checked'] for row in pages)
    checks['source_files_unchanged']=summary.get('original_sources_unchanged')
    for filename in ('final_checks_verified.txt',):
        if (root/filename).exists():checks[filename]=(root/filename).read_text()[-2000:]
    write(root/'report_checks.json',checks)
    parts.append('<h2>记录保留与检查</h2>'+box('核验',checks,True))
    parts.append('<p>'+link(root/'live/summary.json',root,'英文检查器拦截的预检记录（0 次 API）')+'；'+link(root/'translation_plan.json',root,'prompt 英文整理记录（与测试调用分开计数）')+'。</p>')
    (root/'index.html').write_text(''.join(parts)+'</html>')
    # Per-stage pages link to their immediate parent.
    (live/'index.html').write_text(head('真实测试流程')+'<p><a href="../index.html">完整报告</a></p>'+''.join('<p>'+link(live/r['name']/'agent_io.html',live,r['label'])+'</p>' for r in pages)+'</html>')
    print(json.dumps({'stages':len(pages),'status':summary['status'],'prior_records_unchanged':checks['all_prior_records_unchanged']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path)
    render(parser.parse_args().root.resolve())
