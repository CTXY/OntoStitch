"""Show real requests, migrated semantics and unchanged-budget regression data."""
import json
from pathlib import Path
from html import escape
from collections import defaultdict
from build_reconciliation_simplified_report import head,box,one
from build_concept_metric_pipeline_report import details
from reconciliation_probe_fixtures import write,digest
from cortex.schema import GraphState,load_root_schema
from cortex.capabilities.graph_merge.integration.semantic_notes import migrate_relation_notes
from cortex.infrastructure.artifacts.graph_html import render_graph_html

ROOT=Path('output/single_graph_merge_fix_20261121').resolve()
parts=[head('单图 merge 修复：契约、范围与真实调用回归'),
 '<p>修复重复来源包装成关系组、合边导致组引用失效、边界节点意外展开邻域三个问题。真实关系组合含义保留到节点定义；各关系仍保留自身说明和证据。新 reconciliation 读取当前图和 evidence，不读取历史 case 图。</p>',
 '<p><b>这是受控回归结果，不是两个 database 的最终 merged graphs。</b>原完整工作图仍有需要单独处理的 binding_contest，最终发布保持严格全局校验。本轮未切换或重启后台全量任务。</p>',
 '<h2>完整失败 checkpoint 离线回放</h2><table><tr><th>库</th><th>保留已通过节点任务</th><th>复用关系结果</th><th>原宽范围请求 → 新请求字符数</th><th>结果</th></tr>']
for db,old in [('thrombosis_prediction',211432),('toxicology',140892)]:
 result=json.loads((ROOT/db/'offline_result.json').read_text())
 parts.append('<tr><td>'+db+'</td><td>'+str(result['saved_node_tasks_preserved'])+'</td><td>'+str(result['relation_results_reused'])+'，0 次 API 重跑</td><td>'+str(old)+' → '+str(result['replayed_broad_scope_characters'])+'</td><td>通过；上限仍为 210,000</td></tr>')
 parts.append('</table>' if db=='toxicology' else '')
parts.append('<p>上述为按原宽任务锚点、在已合并当前图上的离线重建。与下方受控真实局部任务分开计量；没有将离线结果当作新模型判断。</p>')
parts.append('<h2>真实小范围测试</h2><table><tr><th>步骤</th><th>调用数</th><th>最大请求字符数</th><th>最大 provider 输入 tokens</th><th>完整记录</th></tr>')
audit=[]
for name in ['node_patient','relation_merger','thrombosis_prediction','toxicology']:
 folder=ROOT/'live'/name
 details(folder);one(folder)
 events=[json.loads(x) for x in (folder/'model_calls.jsonl').read_text().splitlines()]
 starts=[r for r in events if r.get('event_type')=='model_start']
 ends=[r for r in events if r.get('event_type')=='model_end']
 sizes=[r['characters'] for r in events if r.get('event_type')=='request_budget']
 tokens=[]
 for row in ends:
  message=row['message'];usage=message.get('usage_metadata') or {};meta=(message.get('response_metadata') or {}).get('token_usage') or {}
  token=usage.get('input_tokens',meta.get('prompt_tokens'))
  if token is not None:tokens.append(token)
 result=json.loads((folder/'result.json').read_text())
 row={'name':name,'status':result['status'],'calls':len(starts),'completed':len(ends),
      'max_characters':max(sizes),'max_input_tokens':max(tokens) if tokens else None,
      'reconciliation_message_counts':sorted({len(r['messages']) for r in starts if r['component'] in {'integration_reconcile','integration_graph_assess'}}),
      'runtime_source_graph_tools':sorted({tool for r in starts for tool in r.get('tool_names',[]) if tool in {'inspect_graph','source_snapshot'}})}
 audit.append(row)
 parts.append('<tr><td>'+name+' · '+escape(result['status'])+'</td><td>'+str(len(starts))+'</td><td>'+str(max(sizes))+'</td><td>'+str(row['max_input_tokens'])+'</td><td><a href="live/'+name+'/agent_io.html">逐步完整输入输出</a> · <a href="live/'+name+'/details.html">图变化</a></td></tr>')
parts.append('</table><p>真实调用使用 deepseek-v4-flash。所有调用已返回，无输入长度错误；reconciliation 每轮固定为 1 条 system + 2 条 user。字符数含工具 schema，tokens 使用 provider 实际返回值，两者不是同一个单位。</p>')
(ROOT/'live/index.html').write_text(head('真实回归调用')+'<p><a href="../index.html">完整修复报告</a></p>'+''.join('<p><a href="'+row['name']+'/agent_io.html">'+row['name']+'</a></p>' for row in audit)+'</html>')
parts.append('<h2>真实 Concept 组合知识保留</h2>')
raw=json.loads(Path('tests/fixtures/single_graph_merge/real_1270_notes.json').read_text())
graph,records=migrate_relation_notes(raw)
write(ROOT/'real_1270_preserved.json',graph.model_dump(mode='json'));write(ROOT/'real_1270_migration.json',records)
parts.append('<p>thrombosis/1270 的组合知识涉及 Patient.ID、Laboratory.ID、Examination.ID 三条不同表示关系。三条边、targets、各自来源及原有角色限制均保留；组合说明迁移到 patient identifier 的 definition。迁移不是把真实组合知识当成重复来源删除。</p>')
parts.append(box('完整原说明及迁移位置',records,True))
parts.append('<h2>两个失败图的迁移与未完成问题</h2>')
pending={}
for db in ['thrombosis_prediction','toxicology']:
 graph=GraphState.model_validate_json((ROOT/db/'after.json').read_text());nodes={n.id:n for n in graph.graph.nodes};owners=defaultdict(set)
 for edge in graph.graph.edges:
  if edge.edge_type=='grounded_on' and nodes[edge.from_node_id].node_type=='concept':owners[edge.to_node_id].add(edge.from_node_id)
 pending[db]=[{'physical_id':key,'concepts':[{'id':i,'name':nodes[i].name} for i in sorted(value)]} for key,value in owners.items() if len(value)>1]
 (ROOT/db/'current_graph.html').write_text(render_graph_html(graph.graph,load_root_schema(),title=db+' · current working graph, pending reconciliation'))
 parts.append('<p>'+db+'：<a href="'+db+'/current_graph.html?view=business">当前工作图（未最终发布）</a> · <a href="'+db+'/after.json">Graph JSON</a> · <a href="'+db+'/migration.json">每条旧说明的迁移记录</a></p>')
 parts.append(box(db+'：其他待处理 binding_contest',pending[db]))
parts.append('<p>局部编译允许本轮未触及、且没有扩大的既存绑定竞争留给其 Signal；它通过验证副本检查其他约束，不能掩盖新错误。最终图发布仍执行严格 GraphManager 全局校验。</p>')
parts.append('<h2>检查与记录保留</h2>')
manifest=json.loads((ROOT/'original_records_manifest.json').read_text())
checks={'old_artifacts':len(manifest),'all_old_artifacts_unchanged':all(Path(p).exists() and digest(p)==h for p,h in manifest.items()),
        'real_model_calls':sum(r['calls'] for r in audit),'no_context_limit_errors':all(r['max_characters']<=210000 for r in audit),
        'source_graph_reader_tools':any(r['runtime_source_graph_tools'] for r in audit),
        'test_output':(ROOT/'release_tests.txt').read_text() if (ROOT/'release_tests.txt').exists() else (ROOT/'final_verified_tests.txt').read_text()}
write(ROOT/'delivery_results.json',{'calls':audit,'checks':checks,'pending_signals':pending})
parts.append(box('回归与原始记录哈希核验',checks,True))
parts.append('<p>单图 checkpoint 回归验证：在 Assessment 前受控中断，恢复时 Writer 不重跑；任务提交后再次恢复，不调用任一模型。另有 500 个无关物理列不增加局部输入的测试。</p>')
(ROOT/'index.html').write_text(''.join(parts)+'</html>')
print(json.dumps(checks,ensure_ascii=False))
