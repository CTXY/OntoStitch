"""Preview revised prompts on saved real inputs, preserving original requests."""
import argparse
import hashlib
from pathlib import Path
import json
import re
from types import SimpleNamespace

from build_reconciliation_simplified_report import head, box, prompt_box, one
from run_relation_reconciliation_v2_smoke import fixtures
from run_node_consolidation_smoke import resolve_input_root
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.integration.relation_context import relation_context
from cortex.capabilities.graph_merge.llm.reconciliation_context import ReconciliationRuntime
from cortex.capabilities.graph_merge.llm.reconciliation_prompts import reconciliation_system
from cortex.schema import GraphSpace,GraphState,load_root_schema
from langchain_openai.chat_models.base import _convert_message_to_dict

REPO=Path(__file__).resolve().parents[1]

def read(path): return json.loads(path.read_text())
def write(path,value): path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def build(root):
    previous=REPO/'output/reconciliation_readable_prompts_20261121/live_flash_verified'
    args=SimpleNamespace(graph_root=REPO/'output/latest_graphs',source_input_root=resolve_input_root(None),
        metric_record=REPO/'tests/fixtures/node_local_metric_retirement.json',discovery_limit=32)
    selected=fixtures(args);summaries=[]
    previews=root/'previews';previews.mkdir(exist_ok=True)
    for name,fixture in selected.items():
        old=previous/fixture.name
        if not (old/'after.json').exists(): continue
        before=GraphState.model_validate(read(old/'before.json'));candidate=GraphState.model_validate(read(old/'after.json'))
        assert before==fixture.before
        records=[]
        for phase in ('reconcile','graph_assess'):
            context=IntegrationContext(before,GraphState(graph=GraphSpace()),load_root_schema(),evidence=fixture.evidence)
            context.task_before=before;context.candidate=candidate if phase=='graph_assess' else before
            task=LocalTask('preview:'+fixture.name,phase,fixture.targets,checks=[fixture.signal]);context.active_task=task
            payload=relation_context(context,context.candidate,task,before=before if phase=='graph_assess' else None,assessment=phase=='graph_assess')
            runtime=ReconciliationRuntime(context,payload,phase)
            runtime.system=reconciliation_system('writer' if phase=='reconcile' else 'assessment',
                [row['kind'] for row in runtime.packet['signals']])
            if phase=='reconcile': runtime.set_compiled_candidate(before,runtime.draft.version)
            messages=[_convert_message_to_dict(m) for m in runtime.messages()]
            tools=list(runtime.available_read_tools(runtime.build_read_tools()))
            records.append({'role':'Writer' if phase=='reconcile' else 'Assessment','messages':messages,
                'predicate_contracts':sorted(runtime.working['before'].get('contracts',{})),
                'candidate_contracts':sorted(runtime.working['candidate'].get('contracts',{})),
                'offered_read_tools':tools,'preview_not_sent_to_model':True})
        original=read(old/'agent_io.json')
        old_writer=next(row for row in original['steps'] if row['request']['component']=='integration_reconcile')
        old_names=re.findall(r'^### 谓词 `([^`]+)`',old_writer['request']['messages'][1]['content'],re.M)
        current=records[0]['predicate_contracts']
        row={'case':fixture.name,'case_ids':fixture.provenance['case_ids'],
            'original_writer_contracts':old_names,'new_writer_contracts':current,
            'removed_contracts':sorted(set(old_names)-set(current)),
            'unchanged_graph_input_sha256':hashlib.sha256((old/'before.json').read_bytes()).hexdigest()}
        summaries.append(row)
        path=previews/fixture.name;path.mkdir(exist_ok=True)
        write(path/'prompts.json',{'preview_not_sent_to_model':True,'comparison':row,'roles':records})
        parts=[head(fixture.name+'：修改后 Prompt 预览'),'<p><a href="../../index.html">返回本轮说明</a></p>',
            '<p class="note">以下使用原有真实测试图重新构造输入，仅为新版 prompt 预览，没有重写旧请求，也不代表新的模型评估。Assessment 使用上轮真实接受的 candidate。</p>',box('谓词范围变化',row,True)]
        for record in records:
            parts.append('<h2>'+record['role']+'</h2>')
            parts.append(box('本轮开放的只读工具',record['offered_read_tools']))
            for index,message in enumerate(record['messages'],1):parts.append(prompt_box(str(index)+' · '+message['role'],message['content']))
        (path/'index.html').write_text(''.join(parts)+'</html>')
    write(root/'preview_summary.json',summaries)
    parts=[head('Signal 记录、谓词范围与 System Prompt 调整'),
        '<p>“命中节点／关系”和“检测／任务记录”是本轮具体输入，完整内容在局部图展开。通用 guidance 与示例位于 system 对应 Signal 章节；示例明确标为示意，不能当作来源证据。</p>',
        '<p>谓词采用可验证的保守类型筛选：已有关系契约完整保留，额外候选只考虑当前局部真实业务起点类型与真实终点类型。没有按名称封禁 examines，也没有让模型做语义相关性过滤。新类型关系可由 Writer 通过 read_predicates 只读查询契约；Assessment 自动得到实际候选使用的契约。</p>',
        '<p>现有 3 消息布局和缓存前缀保留；目录读取只补入动态消息，不扩大图读取范围或写权限。未切换后台 runtime，旧图、原始请求和 checkpoint 保留。</p>',
        '<table><tr><th>真实样本</th><th>旧 Writer 契约数</th><th>新契约数</th><th>本轮排除</th><th>新版完整预览</th></tr>']
    from html import escape
    for row in summaries:
        parts.append('<tr><td>'+escape(row['case'])+'</td><td>'+str(len(row['original_writer_contracts']))+'</td><td>'+str(len(row['new_writer_contracts']))+'</td><td>'+escape(', '.join(row['removed_contracts']))+'</td><td><a href="previews/'+row['case']+'/index.html">System + 两条 User</a></td></tr>')
    parts.append('</table>')
    if (root/'validation.json').exists(): parts.append(box('本地验证',read(root/'validation.json'),True))
    for run in sorted(root.glob('live*/summary.json')):
        for row in read(run)['results']:
            folder=run.parent/row['name']
            if not (folder/'result.json').exists():continue
            result=one(folder)
            parts.append('<h2>新的真实模型小测</h2><p>'+escape(result['name'])+'：'+escape(result['status'])+'。<a href="'+str(folder.relative_to(root))+'/agent_io.html">每步完整输入输出</a>。</p>')
    parts.append('<p><a href="preview_summary.json">结构化范围对比</a> · <a href="before_manifest.json">修改前快照</a></p>')
    (root/'index.html').write_text(''.join(parts)+'</html>')
    print(root/'index.html')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);build(parser.parse_args().root)
