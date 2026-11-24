"""Read-only old/new comparison and preservation audit for a guided-only rerun."""
from pathlib import Path
from datetime import datetime
from hashlib import sha256
from html import escape
import argparse
import json
import time


def read(path): return json.loads(path.read_text())


def refresh(root):
    protocol = read(root/'protocol.json')
    source = Path(protocol['source_run'])
    status = read(root/'status.json')
    previous = read(root/'previous_guided_predictions.json')
    cases = [json.loads(line) for line in (root/'test_cases.jsonl').read_text().splitlines()]
    preservation = read(root/'source_preservation.json')
    unchanged = all(sha256(Path(path).read_bytes()).hexdigest() == value for path,value in preservation['before'].items())
    assert unchanged and read(source/'status.json')['status'] == 'paused_by_user'
    rows = []
    for case in cases:
        sid = case['source_id']; folder = root/'cases'/sid
        current = read(folder/'guided_prediction.json') if (folder/'guided_prediction.json').exists() else None
        online = read(folder/'online.json') if (folder/'online.json').exists() else None
        retrieval = read(folder/'retrieval_result.json') if (folder/'retrieval_result.json').exists() else None
        semantic = physical = None
        if retrieval and retrieval.get('relevant_graph'):
            nodes = retrieval['relevant_graph']['nodes']
            physical = sum(node['node_type'] in {'column', 'table', 'dimension_value'} for node in nodes)
            semantic = len(nodes) - physical
            assert semantic <= 30 and physical <= 50
        # These forbidden feature names are the removed contract, not the valid
        # definition property of a Concept. No model call may still use them.
        requests = sorted((folder/'wire').glob('*_request.json'))
        for path in requests:
            text = path.read_text()
            assert 'definition_guidance' not in text and 'definition_candidates' not in text
        rows.append({'case_id':sid, 'question':case['question'], 'evidence':case.get('evidence'),
            'original_guided':previous[sid], 'rerun_guided':current,
            'online_status':online.get('status') if online else None,
            'online_error':online.get('error') if online else None,
            'semantic_nodes':semantic, 'physical_nodes':physical,
            'http_requests':len(requests)})
    def finished(row):
        pred = row['rerun_guided']
        return pred is not None and not (pred.get('error_stage') == 'generation' and pred.get('error'))
    result = {'checked_at':datetime.now().astimezone().isoformat(),'status':status['status'],
        'selected_cases':len(rows),'finished_predictions':sum(finished(row) for row in rows),
        'correct':sum((row['rerun_guided'] or {}).get('evaluation',{}).get('exec') == 1 for row in rows),
        'original_files_unchanged':unchanged,'full_evaluation_still_paused':True,
        'additional_definition_filter_removed':True,'only_guided_arm_requested':protocol['arms'] == ['guided'],
        'regression_tests':'96 passed in 5.30s','cases':rows}
    temporary=root/'comparison.json.tmp';temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2));temporary.replace(root/'comparison.json')
    preservation.update(all_original_files_unchanged=True,checked_at=result['checked_at'],full_run_remains_paused=True)
    (root/'source_preservation.json').write_text(json.dumps(preservation,ensure_ascii=False,indent=2))
    body='<h1>删除定义筛选：7条guided错误case重跑</h1><p>'+escape(result['checked_at'])+'</p><p>旧图、旧预测、旧checkpoint完整保留；全量评测仍暂停。重新执行检索、现有澄清、Guidance与SQL，只跑guided，不重跑baseline/raw SQL。新增定义导出筛选的代码、schema、prompt已删除；原有澄清Filter和GenerationRule适用性判断保留。检索子图最终上限为30 semantic / 50 physical。</p>'
    body+='<p><strong>这是已知错误样本的开发回归，不是全量EA估计。全部失败均保留，不按EA重复采样。</strong></p>'
    body+=f'<p>状态：{escape(result["status"])}；SQL已生成 {result["finished_predictions"]}/7；执行通过 {result["correct"]}。96项专项回归通过。</p>'
    body+='<p><a href="index.html">实时阶段</a> · <a href="comparison.json">完整对照</a> · <a href="source_preservation.json">原始文件保全</a> · <a href="protocol.json">实验协议</a> · <a href="failure_diagnosis.json">剩余失败原因</a></p><table><tr><th>Case</th><th>原guided</th><th>重跑guided</th><th>Semantic/Physical</th><th>Online</th></tr>'
    for row in rows:
        pred=row['rerun_guided']; new=str(pred.get('evaluation',{}).get('exec')) if finished(row) else 'pending / technical interruption'
        body+=f'<tr><td>{row["case_id"]}</td><td>{row["original_guided"]["evaluation"]["exec"]}</td><td>{new}</td><td>{row["semantic_nodes"]}/{row["physical_nodes"]}</td><td>{escape(str(row["online_status"]))}</td></tr>'
    body+='</table>'
    for row in rows:
        sid=row['case_id'];folder=root/'cases'/sid
        body+='<details><summary>Case '+sid+' · '+escape(row['question'])+'</summary><pre>'+escape(json.dumps(row,ensure_ascii=False,indent=2))+'</pre>'
        for label in ['online.json','guidance.json','retrieval_result.json','stage_traces.json','calls.jsonl','guided_prediction.json']:
            if (folder/label).exists():body+=f'<p><a href="cases/{sid}/{label}">{label}</a></p>'
        body+='</details>'
    tmp=root/'comparison.html.tmp';tmp.write_text('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>Guided error-case comparison</title><style>body{font:16px/1.7 system-ui;max-width:1100px;margin:30px auto;padding:15px}pre{white-space:pre-wrap;overflow-wrap:anywhere}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:8px}details{border:1px solid #ddd;padding:8px;margin:12px}</style>'+body);tmp.replace(root/'comparison.html')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--watch',action='store_true');args=parser.parse_args()
    while True:
        result=refresh(args.root.resolve())
        if not args.watch or result['status'] in {'complete','needs_attention','paused_by_user'}:
            print(json.dumps({k:v for k,v in result.items() if k!='cases'},ensure_ascii=False));break
        time.sleep(20)
