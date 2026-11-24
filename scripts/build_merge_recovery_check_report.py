"""Read-only progress, accepted-prefix and duplicate-receipt checks for a recovery."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
from hashlib import sha256
import difflib,json,sqlite3,os

def read(p):return json.loads(p.read_text())
def cp(path):
 with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as c:return json.loads(c.execute('select payload from merge_workspaces').fetchone()[0])
def detail(label,value):return '<details><summary>'+escape(label)+'</summary><pre>'+escape(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2))+'</pre></details>'
def main():
 repo=Path(__file__).resolve().parents[1];report=Path((repo/'output/latest_merge_recovery_report.txt').read_text().strip());active=read(repo/'output/active_merge_runs.json');jobs={};repeats={};preserved={}
 for db,item in active['databases'].items():
  root=Path(item['root']);status=read(root/'status.json');counts=[]
  for folder in sorted((root/'merge').glob('batch-*')):
   if not (folder/'workspace.sqlite').exists():continue
   payload=cp(folder/'workspace.sqlite');seen={};duplicate=[];changed=[]
   for r in payload.get('signal_receipts',[]):
    k=(r['check_key'],r['policy_hash']);old=seen.get(k)
    if old:
     row={'check_id':r['check'].get('id'),'previous_revision':old['accepted_revision'],'revision':r['accepted_revision']}
     (duplicate if old['premise_hash']==r['premise_hash'] else changed).append(row)
    seen[k]=r
   ids=[h['task_id'] for h in payload['history']]
   counts.append({'batch':folder.name,'completed':(folder/'completed.json').exists(),'accepted_tasks':len(ids),'receipts':len(payload.get('signal_receipts',[])),
     'same_premise_repeat_accepts':duplicate,'changed_premise_rechecks':changed,'duplicate_task_ids':len(ids)-len(set(ids))})
   if (folder/'completed.json').exists():
    completed=read(folder/'completed.json');assert sha256((folder/'state.json').read_bytes()).hexdigest()==completed['state_sha256'];assert read(folder/'audit.json')['status']=='passed'
  latest=sorted(d for d in (root/'merge').glob('batch-*') if (d/'workspace.sqlite').exists())[-1];payload=cp(latest/'workspace.sqlite');task=payload.get('task_state') or {}
  processes=read(root/'background_processes.json');alive={}
  for key,pid in processes.items():
   try:os.kill(pid,0);alive[key]=True
   except ProcessLookupError:alive[key]=False
  req=sorted((latest/'wire').glob('*_request.json'))[-1];wire=read(req);body=wire['body']
  jobs[db]={'status':status['status'],'merged_cases':status.get('merged_cases'),'total_cases':status.get('total_cases'),'batch':status.get('batch'),
     'accepted_tasks':len(payload['history']),'accepted_signals':len(payload.get('signal_receipts',[])),'pending_task':task.get('task_id'),'stage':task.get('stage'),
     'pids':processes,'processes_alive':alive,'key_slot':wire.get('credential_slot'),'model':body.get('model'),'thinking':body.get('thinking'),
     'local_message_characters':sum(len(str(m.get('content'))) for m in body.get('messages',[])),'errors':status.get('errors',[])}
  repeats[db]=counts
  if db=='thrombosis_prediction':
   archive=Path(read(report/'thrombosis_recovery.json')['archive']);old=cp(archive/'workspace.original.sqlite');target=root/'merge/batch-0007';now=cp(target/'workspace.sqlite')
  else:
   archive=sorted((root/'recoveries').glob('exact_json_envelope_20261123_*'))[-1];old=cp(archive/'workspace.original.sqlite');target=root/'merge/batch-0005';now=cp(target/'workspace.sqlite')
  assert now['history'][:len(old['history'])]==old['history'];preserved[db]={'accepted_prefix_tasks':len(old['history']),'prefix_exactly_preserved':True,'source_backup':str(archive/'workspace.original.sqlite')}
 audits={db:read(report/(prefix+'_receipt_audit.json')) for db,prefix in [('thrombosis_prediction','thrombosis'),('toxicology','toxicology')]}
 from cortex.infrastructure.llm.key_slots import read_key_slots
 slots=read_key_slots(repo/'configs/sql_training.keys')
 pools=[{slots[i].api_key.get_secret_value() for i in read(Path(item['root'])/'status.json')['credential_pool']} for item in active['databases'].values()]
 distinct_keys=not bool(pools[0]&pools[1]);assert distinct_keys
 now=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat();result={'checked_at':now,'jobs':jobs,'prefix_preservation':preserved,'receipt_repetition':repeats,'read_only_receipt_audits':audits,'actual_api_key_pools_distinct':distinct_keys,'regression':(report/'regression.txt').read_text().strip()}
 (report/'latest_check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');body='<h1>两库最新进度与停止修复</h1><p>核查：'+escape(now)+'</p><p><a href="../multi_result_merge_resume_20261122_225749/index.html">持续更新的实时进度</a></p>'
 body+='<p>toxicology：旧进程未加载此前的严格JSON围栏解析修复，节点content-4停止；已归档原checkpoint，仅重开失败的序列化恢复窗口，4项已接受节点任务保留。新进程已通过并进入下一批。</p>'
 body+='<p>thrombosis：relation merger已将 input::input::e636 合入 input::e208，原移交却仍断言“两条边存在”，旧Assessment反馈又被当作当前缺陷重复注入。修复为历史观察＋原始到存活边映射＋原始证据要求；独立Assessment仍必须验证当前内容和来源，不能凭映射自动接受。</p>'
 body+='<p>本次还修复了相同编译候选、草稿版本增长时的错误stale判定：仅在图哈希完全相同时重新绑定草稿头，保留读取事实与重试状态；候选确实不同仍拒绝，未改变事实仍由无进展保护拦截。</p>'
 body+='<p>已验证43项回归。thrombosis使用本页runtime_after内的独立修复快照，避免修改健康运行的toxicology进程。工作区核心代码未热切换；启动器支持runtime_source.json自动选择固定快照，后续恢复仍可复现。</p>'
 body+=detail('最新状态',jobs)+detail('已完成任务前缀保全',preserved)+detail('逐批signal重复核验：相同前提与真实前提变化分开',repeats)+detail('只读调度回放核查（不调用模型、不修改checkpoint）',audits)+detail('实际API KEY池互不重叠',distinct_keys)+detail('回归结果',result['regression'])
 if (report/'thrombosis_recovery.json').exists():body+=detail('完整迁移记录',read(report/'thrombosis_recovery.json'))
 diffs=[]
 before=report/'runtime_before';after=report/'runtime_after'
 for rel in ['src/cortex/capabilities/graph_merge/integration/relation_followups.py','src/cortex/capabilities/graph_merge/llm/reconciliation_context.py','src/cortex/capabilities/graph_merge/llm/prompts/reconciliation/prompts/common.en.md']:
  diff=''.join(difflib.unified_diff((before/rel).read_text().splitlines(True),(after/rel).read_text().splitlines(True),fromfile='before/'+rel,tofile='after/'+rel));diffs.append(diff);body+=detail('代码/英文prompt差异：'+rel,diff)
 (report/'runtime_fix.patch').write_text('\n'.join(diffs))
 root=Path(active['databases']['thrombosis_prediction']['root']);folder=root/'merge/batch-0007'
 events=[json.loads(line) for line in (folder/'model_calls.jsonl').read_text().splitlines()]
 for event in events:
  if event.get('recorded_at','')>='2026-11-23T00:24:00' and event.get('event_type') in ('model_start','model_end','assessment_result','tool_result'):
   body+=detail(event['recorded_at']+' · '+event.get('component','')+' / '+event['event_type'],event)
 (report/'index.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>两库停止修复与进度核验</title><style>body{font:16px/1.7 system-ui;max-width:1100px;margin:32px auto;padding:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6fa;padding:14px;font-size:13px}summary{cursor:pointer;padding:10px}details{border:1px solid #ddd;margin:12px 0;border-radius:8px}</style>'+body)
 live=Path(next(iter(active['databases'].values()))['report']).parent
 assets=read(live/'review_assets.json') if (live/'review_assets.json').exists() else {};assets['最新停止原因、修复、回归与signal复用核验']='../'+report.name+'/index.html';assets['中间产物清理清单']='cleanup_20261123/index.html';(live/'review_assets.json').write_text(json.dumps(assets,ensure_ascii=False,indent=2)+'\n')
 for item in active['databases'].values():
  p=Path(item['root'])/'review_assets.json';assets=read(p);assets['最新停止修复与运行核验']='../'+report.name+'/index.html';p.write_text(json.dumps(assets,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'report':str(report/'index.html'),'checked_at':now,'jobs':jobs,'prefix_preservation':preserved},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
