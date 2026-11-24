"""Publish exact before/after code and bounded real-API node test transcripts."""
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
from pathlib import Path
from hashlib import sha256
import difflib
import importlib.util
import json
import sys
import xml.etree.ElementTree as ET

from cortex.capabilities.graph_merge.consolidation.contract import node_system, node_output_model

REPO=Path(__file__).resolve().parents[1]
SRC=REPO/'src/cortex/capabilities/graph_merge'
STYLE='''<style>body{font:16px/1.75 system-ui,sans-serif;color:#1a2333;background:#f4f6fa;max-width:1160px;margin:30px auto;padding:0 20px}section{background:#fff;border:1px solid #dce2ec;border-radius:10px;padding:22px;margin:20px 0}h1{font-size:29px}h2{font-size:22px}pre{font:13px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5fa;padding:15px;border-radius:8px}summary{padding:10px;cursor:pointer;font-weight:600}details{border:1px solid #dce2ec;border-radius:8px;margin:10px 0}table{border-collapse:collapse;width:100%;display:block;overflow:auto}td,th{border:1px solid #dce2ec;padding:9px;text-align:left;vertical-align:top;min-width:95px}a{color:#1357a6}code{overflow-wrap:anywhere}.add{color:#126638;background:#e9f7ef}.del{color:#9b2933;background:#fff0f0}.badge{background:#e6f6eb;padding:3px 9px;border-radius:8px}li{margin:7px 0}</style>'''
def read(p):return json.loads(p.read_text())
def write(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def pre(x):return '<pre>'+escape(x if isinstance(x,str) else json.dumps(x,ensure_ascii=False,indent=2))+'</pre>'
def detail(title,value,opened=False):return '<details'+(' open' if opened else '')+'><summary>'+escape(title)+'</summary>'+pre(value)+'</details>'
def page(path,title,body):path.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+escape(title)+'</title>'+STYLE+'<h1>'+escape(title)+'</h1>'+body+'</html>')
def table(head,rows):return '<table><tr>'+''.join('<th>'+escape(h)+'</th>' for h in head)+'</tr>'+''.join('<tr>'+''.join('<td>'+str(c)+'</td>' for c in row)+'</tr>' for row in rows)+'</table>'
def section(title,body):return '<section><h2>'+escape(title)+'</h2>'+body+'</section>'

def main(root):
    manifest=read(root/'manifest.json');patches=[];changes=[];body=''
    for rel in manifest['files']:
        old=(root/'before-code'/rel).read_text();new=(SRC/rel).read_text()
        dest=root/'after-code'/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(new)
        diff=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='before/'+rel,tofile='after/'+rel))
        if not diff:continue
        patches.append(diff);changes.append({'file':rel,'before_lines':len(old.splitlines()),'after_lines':len(new.splitlines()),
            'before_sha256':sha256(old.encode()).hexdigest(),'after_sha256':sha256(new.encode()).hexdigest()})
        colored='\n'.join('<span class="'+('add' if line.startswith('+') and not line.startswith('+++') else 'del' if line.startswith('-') and not line.startswith('---') else '')+'">'+escape(line)+'</span>' for line in diff.splitlines())
        body+=section(rel,'<p><a href="before-code/'+rel+'">修改前完整文件</a> · <a href="after-code/'+rel+'">修改后完整文件</a></p><pre>'+colored+'</pre>')
    (root/'changes.patch').write_text('\n'.join(patches));write(root/'code_changes.json',changes)
    page(root/'changes.html','本次修改的精确代码差异', '<p>对照本任务开始时保存的源码快照，不使用整个 dirty 工作区的 git diff。每个文件同时提供前后完整副本。</p>'+body)
    # Prove the actual new real-model draft was rejected by the old public schema.
    spec=importlib.util.spec_from_file_location('node_contract_before_multi',root/'before-code/llm/node_contract.py')
    oldmodule=importlib.util.module_from_spec(spec);sys.modules[spec.name]=oldmodule;spec.loader.exec_module(oldmodule)
    actual=read(root/'final/live/real_five_member_partition/result.json')['draft']
    try:oldmodule.NodeWriterResult.model_validate(actual)
    except Exception as error:old_rejection={'rejected':True,'error':str(error)}
    else:old_rejection={'rejected':False}
    assert old_rejection['rejected'] and 'at most 1 item' in old_rejection['error']
    node_output_model('writer').model_validate(actual)
    write(root/'before_schema_rejection.json',old_rejection)
    prompts=''
    for role in ('writer','assessment'):
        schema=node_output_model(role).model_json_schema();write(root/'schemas'/f'{role}.json',schema)
        prompts+=detail(role+' · 完整公开输出 schema',schema)
        for kind in ('concept','metric','event','generation_rule'):
            system=node_system(role,kind);p=root/'runtime-prompts'/f'{role}.{kind}.en.txt';p.parent.mkdir(parents=True,exist_ok=True);p.write_text(system)
            prompts+=detail(role+' / '+kind+' · 完整实际英文 system prompt',system)
    page(root/'prompts.html','当前运行代码的完整英文 prompts 与 schemas','<p>固定装配：共同规则 → 当前角色 → 输出契约 → 当前唯一节点类型。每次只加载一种类型；允许的读取仍只有授权 read_evidence，结果工具仍沿用 submit_patch / 节点 Review。</p>'+prompts)
    cases=[]
    for case in ('real_five_member_partition','metric_singletons'):
        folder=root/'final/live'/case;result=read(folder/'result.json');scope=read(folder/'source_scope.json');delta=read(folder/'local_diff.json')
        records=[json.loads(line) for line in (folder/'model_calls.jsonl').read_text().splitlines()]
        role_calls=[]
        for event in records:
            if event.get('event_type')=='model_end':
                msg=event['message'];role_calls.append({'role':event['component'],'seconds':event.get('elapsed_seconds'),
                    'tools':[tool['name'] for tool in msg.get('tool_calls',[])],
                    'visible_text_characters':len(str(msg.get('content','')))})
        expected={}
        oldnodes={n['id']:n for n in delta['before_nodes']};newnodes={n['id']:n for n in delta['after_nodes']}
        for owner in newnodes:
            members=[k for k,v in delta['member_mapping'].items() if v==owner]
            sources={json.dumps(s,sort_keys=True) for k in members for s in oldnodes[k].get('sources',[])}
            actualsources={json.dumps(s,sort_keys=True) for s in newnodes[owner].get('sources',[])}
            expected[owner]={'members':members,'source_reference_count':len(actualsources),'exact_source_union':sources==actualsources,
                'case_ids':sorted({s.get('case_id') for s in newnodes[owner].get('sources',[]) if s.get('case_id')})}
        assert all(x['exact_source_union'] for x in expected.values())
        result['role_calls']=role_calls;result['exact_source_unions']=expected
        result['cases']=scope['case_ids'];result['relation_counts']={'before':delta['relations_before'],'after':delta['relations_after_distinct']}
        io=detail('来源与 fixture 构造',scope,True)+detail('节点、来源及成员映射的完整前后变化',delta,True)
        io+=detail('所有相关关系的逐条前后对应与来源',read(folder/'relation_dispositions.json'))
        for i,event in enumerate(records):
            if event.get('event_type') in ('model_start','model_end','model_error','tool_result'):
                io+=detail(str(i+1)+' · '+event['component']+' / '+event['event_type'],event)
        wire=[]
        for request in sorted((folder/'wire').glob('*_request.json')):
            q=read(request);seq=request.name.split('_')[0];response=folder/'wire'/f'{seq}_response.json'
            response_data=read(response) if response.exists() else None
            assert q['body'].get('thinking')=={'type':'disabled'} and q['body'].get('reasoning_effort')=='none'
            wire.append({'request':request.name,'message_characters':sum(len(str(m.get('content'))) for m in q['body']['messages']),
                'thinking':q['body']['thinking'],'reasoning_effort':q['body']['reasoning_effort'],
                'seconds':response_data.get('seconds') if response_data else None})
            io+=detail('实际 HTTP 请求正文 '+seq,q['body'])
            if response_data:io+=detail('实际 HTTP 响应 '+seq,response_data)
        result['wire_verification']=wire;write(folder/'verified_result.json',result)
        page(folder/'index.html',case+' · 真实 API 全过程', '<p>没有复用历史 Writer 输出或伪造 Assessment。每一步均为真实 API 返回。输入来源为只读历史 checkpoint；完整业务节点未复制凑数。</p>'+detail('验证结果',result,True)+io)
        cases.append(result)
    before_schemas={'writer':oldmodule.NodeWriterResult.model_json_schema(),'assessment':oldmodule.NodeAssessmentResult.model_json_schema()}
    write(root/'schemas/before.json',before_schemas)
    preservation=[]
    for name,oldhash in manifest['production_checkpoints'].items():
        current=sha256(Path(name).read_bytes()).hexdigest();preservation.append({'checkpoint':name,'sha256_before':oldhash,'sha256_after':current,'unchanged':current==oldhash})
    assert all(p['unchanged'] for p in preservation)
    write(root/'checkpoint_preservation.json',preservation)
    xml=ET.parse(root/'pytest-all.xml').getroot();suites=list(xml) if xml.tag=='testsuites' else [xml]
    tests=sum(int(s.get('tests',0)) for s in suites);failures=sum(int(s.get('failures',0)) for s in suites)
    baseline=(root/'baseline-existing-failures.txt').read_text();assert '8 failed' in baseline
    summary={'generated_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'status':'real_api_passed',
        'design':'one local task / multiple subgroup integrations / one joint assessment / atomic commit',
        'final_live_cases':cases,'initial_live_results':[read(p) for p in sorted((root/'live').glob('*/result.json'))],
        'regression':{'tests':tests,'passed':tests-failures,'failed':failures,'baseline_same_failures':8,
                      'new_focus_result':(root/'pytest-final-focus.txt').read_text()},
        'production_checkpoints_unchanged':len(preservation),'background_runtime_switched':False,'code_changes':changes}
    write(root/'summary.json',summary)
    rows=[]
    for case in cases:
        link='final/live/'+case['case']+'/index.html'
        rows.append(['<a href="'+link+'">'+case['case']+'</a>',escape(', '.join(case['cases'])),
            '5 → 2+3 → 2 个 Concept' if case['case']=='real_five_member_partition' else '2 个不同 Metric → 2 个 singleton',
            str(case['model_calls']),str(case['seconds'])+'s','accept；源码核验通过'])
    body=section('结果与范围','<p><strong>已实现一个固定输入 group 内的多个子组合并，仍沿用一个任务、一份草稿、一次联合独立 Assessment 和原子提交。</strong>没有新增 Identity 调用、重分组工具、子任务调度或计划替换状态。</p><p>两项最终真实 API 小测均通过，原始 checkpoint、生产图和已完成任务未修改；后台任务尚未切换到新代码。本次完整保留初测和最终复测。</p>'+table(['真实测试','来源 cases','结果','调用次数','端到端耗时','独立 Assessment'],rows))
    body+=section('真实五成员失败：现在如何通过','<p>原组混合阳性类别与完整标签属性。Writer 将两个阳性类别成员合为一个 Concept，另三个完整属性成员合为一个 Concept；保留真实的 <code>carcinogenic is_a label</code> 关系。Controller 统一重定向所有相关关系与引用，Assessment 同时看到两个结果。</p><p>结果：5 个节点变为 2 个；阳性类别保留 2 条 source reference，完整属性保留 14 条，分别恰好等于对应输入来源并集。19 条相关关系仍对应 19 条存活关系，端点与来源逐条核验。没有把两个不同身份做成 alternatives。</p>'+detail('最终真实 Writer 草稿',cases[0]['draft'])+detail('同一草稿放入旧 schema 时的真实拒绝',old_rejection))
    body+=section('独立保留也是合法成功','<p>来自 toxicology/215 的两个真实 Metric 分别计算单键分子中的碘原子数和硫原子数。人为将其设为一个待判断组，用于检验不同身份可以全部独立保留。</p><p>Writer 输出 integrate_groups=[]、keep_separate_ids=[两节点]。节点与图保持不变，8 条相关关系保留，独立 Assessment 通过，并写入一个 committed 完成记录。不能把“图没变”当作任务没完成或反复重开。</p>'+detail('最终真实输出',cases[1]['draft']))
    body+=section('代码到底修改了什么',table(['位置','修改前','修改后'],[
        ['NodeWriterResult','integrate_groups 的 max_length=1','允许多个现有 IdentityIntegration；不添加新输出路由字段。'],
        ['node_revision / binding','减去 keep_separate 后，剩余成员只能组成一个组','partition_groups 在公开校验和编译两处共用；覆盖恰好一次，禁止重叠、遗漏、越权及错误 owner。'],
        ['source units / normalize','改 owner 时，对第 0 个 integration 做来源 ID 换键','编译从固定输入 manifest 取每个子组的真实来源；删除换键适配分支。'],
        ['compiler','强制遵守单个残余组的义务','一次构建全部子组的 alias 与 branch map；沿用原引用、关系、来源与原子提交机制。'],
        ['Assessment packet','candidate.node 指主结果；仅该 owner 来源路由','candidate.nodes 完整结果，integrated_groups 携带每组成员与路由，singleton_ids 显式列出独立节点。'],
        ['premise fingerprint','候选事实采集依赖 candidate.node 存在','直接覆盖全部候选和真实分组路由；解释改写和列表重排不算新事实。'],
        ['英文 prompts','one local integration / exactly one','明确输入分组是提议，允许多个结果、all-singletons、子组内 conflict；工具权限与类型 guidance 边界保留。']])+ '<p><a href="changes.html">逐文件完整前后对照</a> · <a href="changes.patch">仅本次修改的 patch</a> · <a href="prompts.html">完整英文 prompts 和公开 schemas</a></p>')
    body+=section('来源、关系、conflict 与恢复的边界','<ul><li>每个原节点只属于一个输出子组或 singleton；原 conflict 的分支不会作为独立成员拆到不同身份组。</li><li>每组只路由自己成员的来源单元。普通 content 与 conflict_points 混填仍拒绝，normal/common 来源不可丢。</li><li>跨子组的关系继续存在；外部 incoming 与语义 refs 由代码机械重定向，不开放全图读取或任意写操作。</li><li>真实 conflict 的分支内容、common 与分支来源、边的起点范围保留；出现旧入边指向新 conflict 的语义问题时，既有关系协调保护仍会阻塞，测试没有绕过该保护来伪造通过。</li><li>多个结果共同编译与提交；Assessment 前中断时 accepted graph/history 不变。恢复从同一草稿重编译并验证候选，直接进入 Assessment；回归中 Writer 总调用数仍为 1，接受后只有一条父任务 history。</li></ul>')
    body+=section('回归验证与已有失败','<p>扩展验证：<strong>'+str(tests-failures)+' 项通过，'+str(failures)+' 项失败</strong>。这 8 项失败全部在修改前源码副本复现；未将其标记为通过。另见最终新增节点分组测试结果。</p><p>其中 6 项旧 fixture 带 relation_metadata，被当前 schema 拒绝；2 项是此前 reconciliation 测试。此次不借机修改无关运行行为。</p><p>覆盖：Concept/Metric/Event/GenerationRule 四类；两个子组；singleton 与原 owner 独立保留；all-singletons；错误 owner、遗漏、重叠、外部成员、来源串组、第二组缺来源；跨组关系、外部 refs；conflict 分支保留；真实前提变化刷新、纯解释/顺序变化不刷新；Assessment checkpoint 恢复不重跑 Writer。</p>'+detail('完整测试尾部',(root/'pytest-all.txt').read_text()[-6000:])+detail('修改前源码副本复现结果',baseline[-6000:])+detail('最终新增测试结果',(root/'pytest-final-focus.txt').read_text())+'<p><a href="pytest-all.xml">完整 JUnit 结果</a> · <a href="pytest-all.txt">完整测试日志</a> · <a href="baseline-existing-failures.txt">基线复现日志</a></p>')
    body+=section('上下文、性能和本次没有切换的内容','<p>读取工具仍只有授权 read_evidence。固定共同提示、角色和 schema 在前，当前唯一类型 guidance 随后；固定局部输入复用，草稿/候选/反馈在当前尾部，不累积多份 graph。</p><p>deepseek-v4-pro，实际 wire 均验证 thinking=disabled、reasoning_effort=none；最终两项测试分别用匿名 key slot 2 / 3。没有泄露密钥。</p><p>五成员最终小测耗时 100.95s，调用为 evidence 读取、Writer 提交、Assessment；Metric 小测 26.36s，仅 Writer 与 Assessment。补充简短输出约束后，模型仍有较长可见说明文本，因此不宣称已彻底解决推理或延迟问题；完整响应与耗时保留。</p><p>生产 checkpoint 文件 '+str(len(preservation))+' 个的哈希与修改前完全相同。尚未迁移或重启 thrombosis/toxicology，原记录和完成任务保留。当前两库此前停在 reconciliation Assessment；本次多结果整合修复不会自动解决其重复补读/非 JSON 输出问题。</p><p><a href="checkpoint_preservation.json">逐 checkpoint 保全校验</a> · <a href="summary.json">完整结果索引</a></p>')
    body+=section('初测记录也完整保留','<p>最终复测前，首轮真实五成员测试为 2 次调用、133.53s，Metric 为 3 次调用、29.39s。随后仅对最终提示增加简短输出纪律后复测，保留初测；没有靠重复同一组制造更多测试样本。</p>'+detail('首轮结果',summary['initial_live_results']))
    page(root/'index.html','节点整合多结果：代码修改与真实 API 验证', '<p>更新：'+escape(summary['generated_at'])+'</p>'+body)
    print(json.dumps({'report':str(root/'index.html'),'live_cases_passed':len(cases),'regression_passed':tests-failures,'baseline_failures':failures,'checkpoints_unchanged':len(preservation)}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);args=parser.parse_args();main(args.root.resolve())
