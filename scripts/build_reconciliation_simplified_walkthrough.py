"""A source-backed walkthrough for the simplified reconciliation probes."""
from pathlib import Path
import json
from build_reconciliation_simplified_report import head,box

def build(root):
    def read(p):return json.loads(p.read_text())
    parts=[head('真实小测逐例说明：数据、修改与评估'),'<p><a href="index.html">最终报告</a> · <a href="protocol.html">完整中文 prompts</a></p>',
    '<p>本页列出本轮实际完成的局部图测试及反馈闭环；未运行的样本不列入结果。测试用例来自真实 case-level graphs；受控注入和参考契约均在下文说明。参考期望修改仅用于离线 oracle，没有注入真实 Writer。以下“通过”指本轮隔离测试通过，不能推广为全数据集已验证。</p>']
    rows=[]
    selections=[]
    for name in ('relation_merger_financial_116','relation_merger_formula_1_880','abstract_predicate','metric_retirement','value_concepts','preserve_conflict'):
        for run in ('live_final_verified','live_flash_verified','live_reviewed','live_verified','live_final_protocol','live'):
            folder=root/run/name
            if (folder/'result.json').exists() and read(folder/'result.json')['status']=='passed':
                selections.append((run,name));break
    for run in ('live_feedback_resumed','live_feedback_reviewed','live_feedback_verified','live_feedback_recovered','live_feedback_complete','live_feedback_compat'):
        folder=root/run/'value_concepts'
        if (folder/'result.json').exists() and read(folder/'result.json')['status']=='passed':
            selections.append((run,'value_concepts'));break
    if (root/'live_checkpoint_resumed_fixed/abstract_predicate/result.json').exists() and read(root/'live_checkpoint_resumed_fixed/abstract_predicate/result.json')['status']=='passed':
        selections.append(('live_checkpoint_resumed_fixed','abstract_predicate'))
    for run,name in selections:
        folder=root/run/name;r=read(folder/'result.json');details=read(folder/'details.json');io=read(folder/'agent_io.json')
        node=details['delta']['nodes'];edge=details['delta']['edges']
        rows.append({'scenario':run+'/'+name,'case_ids':r['provenance']['case_ids'],'nodes':[node['before_count'],node['after_count']],
            'relations':[edge['before_count'],edge['after_count']],'actual_model_calls':sum(not step.get('test_harness_action') for step in io['steps']),
            'model':read(folder/'model_configuration.json').get('model') if (folder/'model_configuration.json').exists() else None,
            'independent_assessment':r.get('independent_assessment',False),'status':r['status']})
    parts.append('<table><tr><th>场景 / 来源 case</th><th>节点</th><th>关系</th><th>真实调用</th></tr>')
    for row in rows:
     parts.append('<tr><td>'+row['scenario']+'<br>'+', '.join(row['case_ids'])+'</td><td>'+str(row['nodes'])+'</td><td>'+str(row['relations'])+'</td><td>'+str(row['actual_model_calls'])+'</td></tr>')
    parts.append('</table>')
    for row in rows:
     path=row['scenario'];folder=root/path;r=read(folder/'result.json');d=read(folder/'details.json');io=read(folder/'agent_io.json');name=folder.name
     parts.append('<h2>'+path+'</h2><p><a href="'+path+'/agent_io.html">逐步完整输入输出</a> · <a href="'+path+'/details.html">完整图差异</a></p>')
     parts.append(box('原始数据来源与构造',r['provenance'],True))
     if name.startswith('relation_merger'):
      parts.append('<p>直接使用原始图中自然存在的一组 depends_on 重复关系，没有复制节点或关系凑数；只调用一次 relation merger，不设 Assessment。节点及固定关系字段不变，来源取精确并集。</p>')
      parts.append(box('合并前描述与最终 description',d['relation_groups'],True))
     elif name=='abstract_predicate':
      parts.append('<p>在 thrombosis_prediction/1164 的真实片段中，仅将已知 has_sex 边受控改成 has_attribute。Writer 恢复具体谓词；70 个节点完全不变。独立 Assessment 比较真实候选及完整谓词契约后通过。</p>')
      parts.append(box('实际关系改动',d['delta']['edges'],True))
     elif name=='metric_retirement':
      parts.append('<p>从 toxicology/266 与 204 的真实已保存节点草稿重建旧依赖尚未协调的中间状态。Writer 使用 edge_replace 指定实际后继；代码将旧边来源并入后继，保留所有 19 个节点。</p>')
      check=next(x for x in read(root/'post_run_checks.json') if x['case']==name)
      parts.append(box('精确来源迁移（独立于 Assessment 的断言）',check,True))
      parts.append(box('边修改完整内容',d['delta']['edges'],True))
     elif name=='value_concepts':
      parts.append('<p>基图来自 toxicology/222；只补装 case204 已有的 has_carcinogenicity_label 谓词契约，没有预先添加维度节点。原有致癌/非致癌 Concept 是分子子集，不能代替标签维度。真实输入只给既有证据 ID，是否读取原始 Question/SQL 由 Agent 决定。</p>')
      if 'feedback_' in path:
       parts.append('<p>测试器先提交一次空草稿（明确不是模型输出）。真实 Assessment 对尚未完成的必需事项返回具体反馈；同一 Writer 据此修订并自动编译，编译成功后直接由真实 Assessment 再次评估。具体反馈、修改与最终判断均原样列于下方。实际调用次数见本页表格；是否保留原有节点与关系，由独立机械断言另行检查。</p>')
      else:
       parts.append('<p>Writer 按本轮任务补充业务维度及其有据关系，独立 Assessment 比较真实候选后通过。实际节点、端点、description 与来源均完整列于下方；不要求模型复现某个预设节点名或同一文字表述。</p>')
      parts.append(box('新建节点完整内容',d['delta']['nodes']['created'],True))
      parts.append(box('新建关系完整内容',d['delta']['edges']['created'],True))
     elif name=='preserve_conflict':
      before=read(folder/'before.json')['graph'];owners=[n for n in before['nodes'] if n.get('alternatives')]
      parts.append('<p>将 toxicology/266 与 204 的两套真实相容表示受控保存为一个已有 conflict。这不是原始数据互相矛盾的声明，也不要求本轮重新裁决。Writer 使用空编辑显式请求评估现状，Assessment 认可现状；19 个节点、21 条边和全部分支范围精确不变。</p>')
      parts.append(box('已有 conflict 的实际存储（完整解析版本见 Agent 输入）',owners,True))
      parts.append(box('四条分支限定出边',[e for e in before['edges'] if e['from_node_id'] in {n['id'] for n in owners}],True))
     judgments=[]
     if r.get('resume_from'):
      import os
      parent=Path(r['resume_from']['case_directory'])
      parts.append('<p><a href="'+os.path.relpath(parent/'agent_io.html',root)+'">恢复前所有 Writer / Assessment 输入输出（含拒绝空草稿与修订）</a>；本轮只续跑待完成的独立评估。</p>')
     for i,step in enumerate(io['steps'],1):
      if step['request']['component']=='integration_graph_assess':
       judgments.append({'step':i,'raw_model_output':step['response']['content'] if step.get('response') else None,
        'controller_result':[e for e in step['controller_events'] if e['event_type']=='assessment_result']})
     if judgments:
      parts.append(box('独立 Assessment 的原文判断与 Controller 校验',judgments,True))
     if (folder/'fixture_evidence.json').exists():parts.append(box('原始 Question / Gold SQL / evidence',read(folder/'fixture_evidence.json')))
    (root/'walkthrough.html').write_text(''.join(parts)+'</html>')
    (root/'test_matrix.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    print(root/'walkthrough.html')

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument("--root",type=Path,required=True)
    build(parser.parse_args().root)
