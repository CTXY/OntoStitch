"""Render preserved real requests, exact agent outputs and controller receipts."""
from pathlib import Path
import argparse
import hashlib
from html import escape
import json

REPO=Path(__file__).resolve().parents[1]

def read(p): return json.loads(p.read_text())
def write(p,v): p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def box(title,value,opened=False):
    return '<details'+(' open' if opened else '')+'><summary>'+escape(title)+'</summary><pre>'+escape(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2))+'</pre></details>'
STYLE='body{font:16px/1.75 system-ui;max-width:1200px;margin:32px auto;padding:0 22px;color:#1e293b}.prompt{padding:0 16px}.prompt h1{font-size:22px}.prompt h2{font-size:19px;margin-top:24px}.prompt h3{font-size:16px}code{font:13px ui-monospace,monospace}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f9;padding:16px;font-size:13px;max-height:75vh;overflow:auto}details{margin:14px 0;border:1px solid #cbd5e1;padding:10px}summary{cursor:pointer;font-weight:600}table{border-collapse:collapse;width:100%}td,th{border:1px solid #cbd5e1;padding:9px;text-align:left}a{color:#0369a1}h2{margin-top:40px}.note{background:#e0f2fe;padding:15px}'
def head(title): return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+escape(title)+'</title><style>'+STYLE+'</style><h1>'+escape(title)+'</h1>'

def markdown_prompt(value):
    """Render our prompt Markdown safely; the exact input stays available below."""
    import re
    out=[];paragraph=[];code=[];fenced=False;items=[]
    def inline(text):
        return ''.join('<code>'+escape(part)+'</code>' if index%2 else escape(part)
                       for index,part in enumerate(text.split('`')))
    def flush():
        if paragraph: out.append('<p>'+inline(' '.join(paragraph))+'</p>');paragraph.clear()
        if items: out.append('<ul>'+''.join('<li>'+inline(item)+'</li>' for item in items)+'</ul>');items.clear()
    for line in value.splitlines():
        if line.startswith('```'):
            if fenced: out.append('<pre><code>'+escape('\n'.join(code))+'</code></pre>');code=[]
            else: flush()
            fenced=not fenced
        elif fenced: code.append(line)
        elif not line.strip(): flush()
        elif re.match(r'^#{1,6} ',line):
            flush();level=len(line)-len(line.lstrip('#'));out.append('<h'+str(level)+'>'+inline(line[level:].strip())+'</h'+str(level)+'>')
        elif line.startswith('- '):
            if paragraph: out.append('<p>'+inline(' '.join(paragraph))+'</p>');paragraph.clear()
            items.append(line[2:])
        else: paragraph.append(line)
    if fenced: out.append('<pre><code>'+escape('\n'.join(code))+'</code></pre>')
    flush()
    return ''.join(out)


def prompt_box(title,value):
    return '<details open><summary>'+escape(title)+'</summary><div class="prompt">'+markdown_prompt(value)+        '</div>'+box('逐字原始文本（实际发给模型）',value)+'</details>'


def one(folder):
    result=read(folder/'result.json')
    events=[json.loads(x) for x in (folder/'model_calls.jsonl').read_text().splitlines()] if (folder/'model_calls.jsonl').exists() else []
    wires=[(p,read(p)) for p in sorted((folder/'wire').glob('*request.json'))]
    steps=[]
    seed=read(folder/'seed_submission.json') if (folder/'seed_submission.json').exists() else None
    if seed:
        steps.append({'request':{'component':'integration_reconcile','messages':seed['controller_messages']},
            'response':seed['seed_response'],'controller_events':[]})
    for event in events:
        if event.get('event_type')=='model_start':
            steps.append({'request':event,'controller_events':[]})
        elif steps:
            steps[-1]['controller_events'].append(event)
    used=set()
    for step in steps:
        wanted=[m.get('content') for m in step['request']['messages']]
        for p,w in wires:
            if p.name not in used and wanted==[m.get('content') for m in w['body']['messages']]:
                step['wire_file']=str(p.relative_to(folder));step['wire_body']=w['body'];used.add(p.name);break
        endings=[x for x in step['controller_events'] if x.get('event_type')=='model_end']
        step['response']=endings[0]['message'] if endings else step.get('response')
    for step in steps:
        step['test_harness_action']=any(call.get('id')=='controlled-empty-seed' for call in (step.get('response') or {}).get('tool_calls',[]))
    data={'result':result,'steps':steps,'wire_attempts':[str(p.relative_to(folder)) for p,_ in wires],
        'note':'These are actual model calls. Complete system/user messages and public schemas come from captured HTTP request bodies; controller receipts are recorded after each response. Test workspace only.'}
    write(folder/'agent_io.json',data)
    parts=[head(result['name']+'：真实 Agent 输入与输出'),'<p><a href="../index.html">该轮总览</a> · <a href="agent_io.json">完整结构化调用</a> · <a href="details.html">节点/关系前后差异</a> · <a href="before.json">输入图</a> · <a href="after.json">接受图</a></p>',
        '<p class="note">每一步按真实调用顺序展示。展开“完整输入”可分别查看 system prompt、所有 user messages 和实际公开的工具 schema。格式失败、读取与编译错误均保留。此页面不把离线代理输出当作真实模型证据。</p>',box('测试来源与受控构造',result.get('provenance',{}),True)]
    resumed=result.get('resume_from')
    if resumed:
        import os
        original=Path(resumed['case_directory'])
        relative=os.path.relpath(original/'agent_io.html',folder)
        parts.append('<p class="note">本页从已提交、待独立评估的 checkpoint 继续；已有 Writer 草稿和编译结果保留。<a href="'+relative+'">恢复前的完整 Writer / Assessment 输入输出及接口失败记录</a>。本页中的模型调用仅属于恢复阶段。</p>')
        parts.append(box('检查点恢复信息',resumed))
    if (folder/'controlled_stop.json').exists(): parts.append(box('受控中断：以下为 Controller 已准备、尚未发给 API 的 Assessment 输入',read(folder/'controlled_stop.json'),True))
    if (folder/'fixture_evidence.json').exists(): parts.append(box('原始 Question / Gold SQL / evidence（测试材料，不代表每轮都注入）',read(folder/'fixture_evidence.json')))
    if (folder/'seed_submission.json').exists(): parts.append(box('受控空草稿起点（由测试器提供，不是模型生成）',read(folder/'seed_submission.json'),True))
    for i,step in enumerate(steps,1):
        role=step['request'].get('component','').removeprefix('integration_')
        role={'reconcile':'关系协调 Writer','graph_assess':'独立 Assessment'}.get(role,role)
        if step.get('test_harness_action'): role+=' · 测试器受控空编辑（非模型输出，无 API 请求）'
        parts.append('<h2>第 '+str(i)+' 步 · '+escape(role)+'</h2>')
        request=step.get('wire_body')
        messages=request['messages'] if request else step['request']['messages']
        parts.append('<details open><summary>完整输入：'+str(len(messages))+' 条消息（1 条 system + '+str(len(messages)-1)+' 条 user，同一次请求）</summary>')
        for n,m in enumerate(messages,1):
            label=m.get('role',m.get('type','message'))
            if len(messages)==3: label+=('：共用契约与角色职责' if n==1 else '：固定任务、原始局部图与阅读说明' if n==2 else '：当前草稿、实际候选、补读材料及有效反馈')
            parts.append(prompt_box(str(n)+' · '+label,m.get('content','')))
        if request:
            parts.append(box('公开工具 schemas',request.get('tools',[])))
            parts.append(box('输出约束与模型参数',{k:v for k,v in request.items() if k not in {'messages','tools'}}))
            parts.append('<p><a href="'+step['wire_file']+'">原始 HTTP body（不含认证头）</a></p>')
        parts.append('</details>')
        parts.append(box('Agent 原始输出',step['response'],True))
        receipts=[e for e in step['controller_events'] if e.get('event_type') in {'tool_result','assessment_result','protocol_rejected','model_error'}]
        parts.append(box('Controller 编译 / 读取 / 结果校验反馈',receipts,True))
        budgets=[e for e in step['controller_events'] if e.get('event_type')=='request_budget']
        if budgets: parts.append(box('下一轮上下文计量',budgets))
    parts.append(box('全部实际传输尝试（包括失败重试）',data['wire_attempts']))
    (folder/'agent_io.html').write_text(''.join(parts)+'</html>')
    return {'name':result['name'],'status':result['status'],'calls':sum(not step.get('test_harness_action') for step in steps),'harness_actions':sum(bool(step.get('test_harness_action')) for step in steps),
        'responses':sum(step.get('response') is not None and not step.get('test_harness_action') for step in steps),
        'model':read(folder/'model_configuration.json').get('model') if (folder/'model_configuration.json').exists() else None,
        'assessment_calls':sum(s['request'].get('component')=='integration_graph_assess' for s in steps),
        'format_errors':sum(e.get('event_type')=='assessment_result' and bool(e.get('result',{}).get('error')) for e in events),
        'source_files_unchanged':result.get('source_files_unchanged'),'result':str(folder/'result.json')}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);args=parser.parse_args();root=args.root
    runs=[]
    for index in sorted(root.glob('live*/summary.json')):
        run=index.parent;rows=[]
        for row in read(index)['results']:
            folder=run/row['name']
            if (folder/'result.json').exists(): rows.append(one(folder))
        runs.append({'directory':run.name,'rows':rows,'budget':read(index)['budget']})
    policy=read(root/'policy_before_final_live.json') if (root/'policy_before_final_live.json').exists() else {}
    changes=[p for p,d in policy.items() if not Path(p).exists() or hashlib.sha256(Path(p).read_bytes()).hexdigest()!=d]
    documentation_changes=[p for p in changes if Path(p).name=='README.md']
    changes=[p for p in changes if p not in documentation_changes]
    validation=read(root/'validation.json') if (root/'validation.json').exists() else {}
    summary={'runs':runs,'validation':validation,'policy_changes_since_final_live':changes,'documentation_changes_after_live':documentation_changes,'background_runtime_switched':False}
    write(root/'review_summary.json',summary)
    parts=[head('Reconciliation 可读 User Prompts：真实小范围验证'),'<p class="note">后台 runtime 未切换。旧 checkpoint、真实失败记录和已完成任务均保留；本轮只操作隔离测试 workspace。下列真实模型记录与离线回归明确区分。</p>',
        '<p>可读 user prompts：每次请求为 1 条 system + 2 条 user。第一条 user 用中文说明任务并提供固定原始局部图；第二条只更新当前草稿、实际候选、补读材料和有效反馈。不回放历史对话，不展示常规状态中的 coverage/hash/空回执，真实错误完整保留。Writer 编辑图草稿，代码自动编译；编译成功即自动进入 Assessment，无独立确认调用；Assessment 直接返回 explanation / feedback，两方面都通过才接受，有反馈仍回同一 Writer。空草稿不需要 keep 决策，合法 conflict 保留。</p>',
        '<p><a href="walkthrough.html">逐例测试数据、改动与 Assessment 依据</a> · <a href="protocol.html">完整中文 prompts 与输入/输出契约</a> · <a href="validation.json">代码回归验证</a> · <a href="before_manifest.json">修改前快照清单</a> · <a href="review_summary.json">结果明细</a></p>']
    if (root/'run_notes.json').exists():
        parts.append('<p><a href="code_changes.diff">修改前后的代码与 Prompt 差异</a> · <a href="implementation.html">流程、边界与恢复缺陷修复说明</a></p>')
    if (root/'live_feedback_verified'/'value_concepts'/'result.json').exists():
        parts.append('<h2>本轮测试发现与修复</h2><ul>'
            '<li>任务表达：保留具体任务原文，明确区分检查机会与必须结果。受控 value_concepts 原先标为疑点，却要求必须补建业务维度，已统一为明确义务；普通 Signal 不因此被强制修图。'
            '<a href="live_feedback_verified/value_concepts/agent_io.html">最初未满足测试预期的 Assessment 原文</a>。</li>'
            '<li>输出反馈：当前轮末尾明确读取、编辑、提交或 JSON 输出动作。真实的字段缺失、格式错误和所有修改步骤都保留在输入输出记录中。</li>'
            '<li>接口与模型：Pro 尝试出现 400/502/慢响应；隔离测试配置省略 thinking 参数，完整常规重跑采用 deepseek-v4-flash。未修改全局 API 配置。</li>'
            '<li>恢复：反馈场景保留 Pro 已提交草稿，在新 workspace 复制 checkpoint 后用 Flash 独立 Assessment 续跑；未重跑 Writer。</li></ul>')
    parts.append('<p><a href="context_audit.json">实际请求的消息数量、固定前缀与正文去重审计</a> · <a href="post_run_checks.json">原始图、来源并集及 checkpoint 断言</a></p>')
    preferred={'live_final_verified':-2,'live_checkpoint_resumed_fixed':-1,'live_flash_verified':0,'live_feedback_resumed':1,'live_reviewed':2,'live_feedback_reviewed':3,'live_verified':2,'live_feedback_verified':3,'live_final_protocol':2,'live_feedback_recovered':3,'live_feedback_complete':4,'live':5}
    runs.sort(key=lambda run:(preferred.get(run['directory'],3),run['directory']))
    for run in runs:
        diagnostic=((root/'live_flash_verified'/'summary.json').exists() and run['directory'] not in {'live_flash_verified','live_feedback_resumed'}) or (run['directory'] in {'live_feedback','live_feedback_retry','live_feedback_final'} or
            (run['directory'].startswith('live_feedback') and run['rows'] and all(row['status']!='passed' for row in run['rows'])))
        if diagnostic:
            parts.append('<details><summary>保留的接口失败与测试诊断：'+escape(run['directory'])+'</summary><p>原始请求、接口错误和未完成状态完整保留；重试结果单独列出，不把这些请求标为通过。</p>')
        notes=read(root/'run_notes.json') if (root/'run_notes.json').exists() else {}
        if notes.get(run['directory']): parts.append('<p class="note">'+escape(notes[run['directory']])+'</p>')
        parts.append('<h2>'+escape(run['directory'])+'</h2><table><tr><th>样本</th><th>结果</th><th>实际请求模型</th><th>请求尝试 / 响应 / Assessment</th><th>Review 入口</th></tr>')
        for row in run['rows']:
            path=run['directory']+'/'+row['name']
            parts.append('<tr><td>'+escape(row['name'])+'</td><td>'+escape(row['status'])+'</td><td>'+escape(row.get('model') or '未记录')+'</td><td>'+str(row['calls'])+' / '+str(row['responses'])+' / '+str(row['assessment_calls'])+'</td><td><a href="'+path+'/agent_io.html">每步完整输入输出</a> · <a href="'+path+'/details.html">前后图差异</a></td></tr>')
        parts.append('</table>')
        if diagnostic:parts.append('</details>')
    parts.append(box('回归与保留记录验证',validation,True))
    (root/'index.html').write_text(''.join(parts)+'</html>')
    from cortex.capabilities.graph_merge.reconciliation.policy import reconciliation_system
    from cortex.capabilities.graph_merge.reconciliation.contract import SIGNALS
    from cortex.capabilities.graph_merge.reconciliation.contract import TOOLS,AssessmentResult
    prompts=[head('完整中文 prompts 与公开 schema'),'<p><a href="index.html">返回真实测试报告</a></p><p>稳定共用前缀 + 角色契约 + 仅当前 Signal 的 guidance。实际每轮 system/user 内容以各测试的 HTTP 请求记录为准。</p>']
    from cortex.capabilities.graph_merge.reconciliation.presentation import user_assets
    for name,text in user_assets().items(): prompts.append(prompt_box(name,text))
    for role in ('writer','assessment'):
        prompts.append(box(role+' 共用与角色 prompt',reconciliation_system(role,()),True))
        for kind in SIGNALS:
            text=reconciliation_system(role,[kind]);base=reconciliation_system(role,()).rstrip()
            prompts.append(box(role+' / '+kind,text[len(base):].strip()))
    prompts.append(box('Writer 与读取工具 schema',{k:v.model_json_schema() for k,v in TOOLS.items()}))
    prompts.append(box('Assessment 直接输出 schema',AssessmentResult.model_json_schema(),True))
    (root/'protocol.html').write_text(''.join(prompts)+'</html>')
    from build_reconciliation_simplified_walkthrough import build
    build(root)
    print(root/'index.html')

if __name__=='__main__': main()
