"""Measure actual first Writer requests after the local-budget checkpoint resume."""
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/signal_receipts_rollout_20261122/input_size_analysis'
OUT.mkdir(exist_ok=True)
result={'checked_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'units':'Unicode characters; JSON-serialized guard is not token count','databases':{}}

def escaped_size(value):
    return len(json.dumps(value,ensure_ascii=False))-2

for short in ('thrombosis','toxicology'):
    root=ROOT/'output'/f'{short}_remaining_merge_pro_signal_receipts_20261122'
    folder=root/'merge/batch-0003'
    cutoff=datetime(2026,11,22,16,32,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    request=None
    for path in sorted((folder/'wire').glob('*_request.json')):
        row=json.loads(path.read_text());body=row.get('body',{})
        if row['time']>=cutoff and any(t.get('function',{}).get('name')=='edit_draft' for t in body.get('tools',[])):
            request=(path,row);break
    if request is None:raise ValueError('No resumed real Writer request')
    path,wire=request;body=wire['body'];text=body['messages'][1]['content']
    sections=[];matches=list(re.finditer(r'^# ([^\n]+)',text,re.M))
    for i,m in enumerate(matches):
        piece=text[m.start():matches[i+1].start() if i+1<len(matches) else len(text)]
        sections.append({'title':m.group(1),'raw_characters':len(piece),'escaped_characters':escaped_size(piece)})
    nodes=[];edges=[];contracts=[];leaf_strings=defaultdict(list)
    for m in re.finditer(r'^### (Node|Relationship|Predicate) `([^`]+)`[^\n]*\n([\s\S]*?)(?=^### |^# |\Z)',text,re.M):
        kind,ident,content=m.groups();chunks=[json.loads(x) for x in re.findall(r'```json\n([\s\S]*?)\n```',content)]
        if not chunks:continue
        obj={'id':ident,'raw_characters':len(m.group()),'escaped_characters':escaped_size(m.group())}
        if kind=='Node':
            value=chunks[0];obj.update(node_type=value['node_type'],name=value.get('name'),evidence_ids=len(value.get('evidence_ids',[])),properties_character_counts={k:len(json.dumps(v,ensure_ascii=False)) for k,v in value.get('properties',{}).items()})
            obj['description_equals_definition']=bool(value.get('description')) and value.get('description')==value.get('properties',{}).get('definition')
            obj['description_equals_definition_characters']=len(value['description']) if obj['description_equals_definition'] else 0
            obj['record']=value;nodes.append(obj)
            def walk(v,p):
                if isinstance(v,str) and len(v)>=100:leaf_strings[v].append(ident+':'+p)
                elif isinstance(v,dict):
                    for k,x in v.items():walk(x,p+'.'+k)
                elif isinstance(v,list):
                    for i,x in enumerate(v):walk(x,p+'.'+str(i))
            walk(value,'node')
        elif kind=='Relationship':
            obj.update(from_node_id=chunks[0][0],edge_type=chunks[0][1],to_node_id=chunks[0][2],record={'id':ident,'from_node_id':chunks[0][0],'edge_type':chunks[0][1],'to_node_id':chunks[0][2],'properties':chunks[1],'evidence_ids':chunks[2]});edges.append(obj)
        else:obj['record']=chunks[0];contracts.append(obj)
    node_totals=defaultdict(lambda:{'count':0,'raw_characters':0,'escaped_characters':0})
    for n in nodes:
        x=node_totals[n['node_type']];x['count']+=1;x['raw_characters']+=n['raw_characters'];x['escaped_characters']+=n['escaped_characters']
    node_ids=[n['id'] for n in nodes];edge_ids=[e['id'] for e in edges]
    backups=sorted((root/'recoveries').glob('local_budget_*/workspace.before.sqlite'))
    with sqlite3.connect(backups[-1]) as connection:cp=json.loads(connection.execute('select payload from merge_workspaces').fetchone()[0])
    task=next(t for t in cp['open_tasks'] if t['id']==cp['task_state']['task_id'])
    actual={n['id']:n for n in cp['current']['graph']['nodes']};targets=set(task['targets']);core=targets & actual.keys()
    degree=Counter()
    for e in cp['current']['graph']['edges']:degree[e['from_node_id']]+=1;degree[e['to_node_id']]+=1
    compact={'nodes':[n['record'] for n in nodes],'edges':[e['record'] for e in edges],'contracts':[c['record'] for c in contracts]}
    # Compare equal public graph facts, excluding user task, tools and role rules.
    graph_rendered=sum(o['raw_characters'] for o in [*nodes,*edges,*contracts])
    graph_compact=len(json.dumps(compact,ensure_ascii=False,separators=(',',':')))
    repeat=[{'occurrences':len(locations),'characters_each':len(value),'duplicated_characters':(len(locations)-1)*len(value),'locations':locations,'preview':value[:100]} for value,locations in leaf_strings.items() if len(locations)>1]
    sample={'request_file':str(path.relative_to(ROOT)),'task_id':task['id'],'checks':len(task['checks']),
        'message_count':len(body['messages']),'raw_message_characters':[len(m.get('content') or '') for m in body['messages']],
        'guard_serialized_characters':len(json.dumps({'messages':body['messages'],'tools':body.get('tools',[])},ensure_ascii=False,default=str)),
        'tool_schema_characters':len(json.dumps(body.get('tools',[]),ensure_ascii=False)),
        'fixed_task_sections':sections,'node_count':len(nodes),'edge_count':len(edges),'predicate_count':len(contracts),
        'duplicate_node_ids':len(node_ids)-len(set(node_ids)),'duplicate_edge_ids':len(edge_ids)-len(set(edge_ids)),
        'node_types':dict(node_totals),'largest_nodes':[{k:v for k,v in n.items() if k!='record'} for n in sorted(nodes,key=lambda n:n['raw_characters'],reverse=True)[:10]],
        'core_nodes':len(core),'mixed_target_ids':len(targets),'core_node_degrees':[{'id':i,'name':actual[i]['name'],'type':actual[i]['node_type'],'degree':degree[i]} for i in sorted(core,key=lambda i:degree[i],reverse=True)],
        'authoritative_business_nodes':sum(n['node_type'] not in {'table','column','dimension_value'} for n in actual.values()),
        'shown_business_nodes':sum(n['node_type'] not in {'table','column','dimension_value'} for n in nodes),
        'rendered_graph_characters':graph_rendered,'same_graph_compact_json_characters':graph_compact,
        'identical_description_definition_nodes':sum(n['description_equals_definition'] for n in nodes),
        'identical_description_definition_extra_characters':sum(n['description_equals_definition_characters'] for n in nodes),
        'exact_repeated_long_strings':sorted(repeat,key=lambda x:x['duplicated_characters'],reverse=True)[:10]}
    result['databases'][short]=sample
(OUT/'analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
body='<h1>500k阈值与真实输入体量分析</h1><p>分析时间：'+escape(result['checked_at'])+'。使用16:32恢复后的实际Writer请求；与原失败任务相同的固定输入。这里的字符不是token，guard还计入JSON转义和工具schema。</p><p>两库local_request_max_characters已按用户要求设为500000，Identity仍600000，节点分组预算仍110000。保留所有原始checkpoint、历史与receipt。</p>'
for name,s in result['databases'].items():
    body+='<h2>'+name+'</h2><p>完整请求guard计数 '+str(s['guard_serialized_characters'])+' 字符；'+str(s['message_count'])+' 条消息；'+str(s['checks'])+' 项Signal；核心节点 '+str(s['core_nodes'])+' 个，实际展示 '+str(s['node_count'])+' 个节点、'+str(s['edge_count'])+' 条边、'+str(s['predicate_count'])+' 个谓词契约。'+str(s['shown_business_nodes'])+'/'+str(s['authoritative_business_nodes'])+' 个业务节点被展示。</p><table><tr><th>固定User部分</th><th>原文字数</th><th>JSON转义后字符</th></tr>'
    for row in s['fixed_task_sections']:body+='<tr><td>'+escape(row['title'])+'</td><td>'+str(row['raw_characters'])+'</td><td>'+str(row['escaped_characters'])+'</td></tr>'
    body+='</table><p>节点ID重复 '+str(s['duplicate_node_ids'])+'，边ID重复 '+str(s['duplicate_edge_ids'])+'。完整graph没有多份重发；主要是单份局部范围及对象正文很大。</p><h3>按节点类型</h3><pre>'+escape(json.dumps(s['node_types'],ensure_ascii=False,indent=2))+'</pre><h3>核心节点及连接度</h3><pre>'+escape(json.dumps(s['core_node_degrees'],ensure_ascii=False,indent=2))+'</pre><h3>最大的节点</h3><pre>'+escape(json.dumps(s['largest_nodes'][:5],ensure_ascii=False,indent=2))+'</pre><p>同一组公开图事实：现渲染 '+str(s['rendered_graph_characters'])+' 字符，紧凑JSON '+str(s['same_graph_compact_json_characters'])+' 字符（仅比较图事实；不是声称可直接替换全部prompt）。description与definition完全相同的节点 '+str(s['identical_description_definition_nodes'])+' 个，重复正文 '+str(s['identical_description_definition_extra_characters'])+' 字符。</p>'
body+='<h2>原因与优化方向</h2><ol><li>多项Signal共享patient / atom / molecule等高连接核心。完整一跳关系、所有边界节点完整正文、引用物理节点及grounding owners共同扩大了单份局部输入。</li><li>当前review_batches仅用focus节点的JSON大小估算分组，不计算展开后的完整Writer消息、关系、契约和工具schema，因此约14k的分组估算会对应约200k的实际请求。</li><li>边界节点没有递归升级成核心，但完整正文仍包含长GenerationRule条件、action、理由和triggers；即使只为核对一条关系也会发送这些字段。</li><li>每条边重复带英文标签、三个JSON代码块和长evidence IDs；谓词契约、四类节点创建字段说明、工具schema另有固定开销。</li><li>部分节点description与definition完全重复，属于对象内冗余，和多轮重复发送整个graph是两件不同的事。</li></ol><p>建议后续按最终展开后的序列化请求大小分组；对无修改边界节点采用任务相关完整性契约；精简边的渲染与完全相同的description/definition。500k目前只提高保护阈值，未删内容、未改变权限或prompts。</p><p><a href="analysis.json">完整统计与最长对象</a> · <a href="../index.html">最新后台进度</a></p>'
style='body{font:16px/1.7 system-ui;max-width:1100px;margin:30px auto;padding:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#175db6}'
(OUT/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Merge input size analysis</title><style>'+style+'</style>'+body)
for name,s in result['databases'].items():
    print(name,json.dumps({k:s[k] for k in ('guard_serialized_characters','message_count','checks','core_nodes','node_count','edge_count','predicate_count','shown_business_nodes','authoritative_business_nodes','node_types','fixed_task_sections','rendered_graph_characters','same_graph_compact_json_characters','identical_description_definition_nodes','identical_description_definition_extra_characters','duplicate_node_ids','duplicate_edge_ids')},ensure_ascii=False))
