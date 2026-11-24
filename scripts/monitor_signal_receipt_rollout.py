"""Background HTML and read-only receipt validation for the two migrated jobs."""
import argparse
from datetime import datetime
from html import escape
import json
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

from audit_signal_receipt_reuse import audit


def refresh(report, roots):
    rows={}
    for root in roots:
        status=json.loads((root/'status.json').read_text())
        plan=json.loads((root/'plan.json').read_text())
        batch=status.get('batch')
        if batch is None:
            available=[p for p in (root/'merge').glob('batch-*') if (p/'workspace.sqlite').exists()]
            batch=int(sorted(available)[-1].name.split('-')[-1])
        folder=root/'merge'/f'batch-{batch:04d}'
        with sqlite3.connect(folder/'workspace.sqlite') as connection:
            cp=json.loads(connection.execute('select payload from merge_workspaces').fetchone()[0])
        receipts=cp.get('signal_receipts',[])
        task=cp.get('task_state') or {}
        audit_file=report/(plan['database']+f'_batch_{batch:04d}_receipt_audit.json')
        prior=json.loads(audit_file.read_text()) if audit_file.exists() else {}
        if (receipts and (prior.get('status')!='passed' or prior.get('audit_protocol')!=2
                or prior.get('accepted_receipts')!=len(receipts)) and status.get('batch') is not None):
            try:
                prior=audit(root)
                audit_file.write_text(json.dumps(prior,ensure_ascii=False,indent=2)+'\n')
            except Exception as error:
                prior={'status':'audit_error','type':type(error).__name__,'error':str(error)}
                audit_file.write_text(json.dumps(prior,ensure_ascii=False,indent=2)+'\n')
        rows[plan['database']]={'status':status['status'],'batch':batch,
            'merged_cases':status.get('merged_cases'),'total_cases':status.get('total_cases'),
            'accepted_tasks':len(cp['history']),'pending_task':task.get('task_id'),
            'stage':task.get('stage'),'accepted_receipts':len(receipts),'scope_compatibility_proofs':len(cp.get('signal_scope_migrations',[])),
            'distinct_receipt_checks':len({r['check_key'] for r in receipts}),
            'credential_pool':status.get('credential_pool'),
            'active_key_slot':(status.get('used_anonymous_key_slots') or [None])[-1],
            'errors':status.get('errors',[]),'receipt_reuse_audit':prior,
            'output':str(root),'model':status.get('model',plan['model']),'batch_size':plan['batch_size']}
        rows[plan['database']]['runtime_limits']=status.get('runtime_limits',{})
    now=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    result={'checked_at':now,'runtime_applied':True,'databases':rows}
    temporary=report/'latest.tmp'
    temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(report/'latest.json')
    body='<h1>单 Signal 通过记录：主工作区应用与后台续跑</h1><p>更新：'+escape(now)+'</p><p>已应用审阅代码；原始运行目录保持冻结，新目录从迁移checkpoint继续。每批10条，deepseek-v4-pro，两个凭据池互不重叠。原已接受history前缀、完整批次及图均已核验保留。</p><p>旧 receipt 与 history 保留；范围收窄后，仅对旧前提仍有效、且新前提是原有事实准确子集的记录附加兼容证明，不虚构新 Assessment。其余变化仍需检查。下方回放检查只读新模型实际接受的checkpoint，重分组不进入任务，无关变化命中，相关变化失效；不产生额外API调用。</p><table><tr><th>库</th><th>完成cases</th><th>批次/状态</th><th>本批receipt数</th><th>receipt复用验证</th></tr>'
    for name,row in rows.items():
        link='../'+Path(row['output']).name+'/index.html'
        body+='<tr><td><a href="'+link+'">'+name+'</a></td><td>'+str(row['merged_cases'])+'/'+str(row['total_cases'])+'</td><td>'+str(row['batch'])+' / '+escape(row['status'])+'</td><td>'+str(row['accepted_receipts'])+'</td><td>'+escape(row['receipt_reuse_audit'].get('status','等待首条真实接受记录'))+'</td></tr>'
    body+='</table><p><a href="../signal_subgraphs_20261122/index.html">按 Signal 子图及完整真实输入输出</a> · <a href="../signal_receipts_rollout_20261122/input_size_analysis/index.html">500k阈值与真实输入体量分析</a> · <a href="../signal_receipts_rollout_20261122/accepted_manifest_bug/index.html">运行速度比较、两次额外复查及修复记录</a> · <a href="../signal_subgraphs_20261122/rollout.json">迁移前后图与history保全核验</a> · <a href="../signal_subgraphs_20261122/pytest_main.xml">216项主工作区检查及新增迁移回归通过</a> · <a href="../reconciliation_signal_receipts_20261122/index.html">详细设计及审阅记录</a></p><details><summary>完整实时记录</summary><pre>'+escape(json.dumps(result,ensure_ascii=False,indent=2))+'</pre></details>'
    assets=report/'review_assets.json'
    if assets.exists():
        body+='<p>'+' · '.join('<a href="'+escape(url,quote=True)+'">'+escape(label)+'</a>' for label,url in json.loads(assets.read_text()).items())+'</p>'
    document='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>Per-signal receipt rollout</title><style>body{font:16px/1.7 system-ui;max-width:1100px;margin:32px auto;padding:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:10px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#175db6}</style>'+body
    (report/'index.html.tmp').write_text(document)
    (report/'index.html.tmp').replace(report/'index.html')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--root',type=Path,action='append',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    while True:
        result=refresh(args.report.resolve(),[r.resolve() for r in args.root])
        if args.once or all(row['status'] in {'complete','needs_attention','stopped'} for row in result['databases'].values()):break
        time.sleep(20)
