"""Read-only replay of saved real recall/relevance under final 30/50 caps."""
from pathlib import Path
import sys
REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO/'src'), str(REPO/'scripts')]

import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
from html import escape
import json

from cortex.application.retrieve_db import RetrieveDbResult, _validate_result
from cortex.capabilities.retrieve_db.engine import RetrieveDbCapability
from cortex.capabilities.online_context.discovery.evidence import _analysis_graph_payload
from cortex.schema import GraphState, load_root_schema


def read(p): return json.loads(p.read_text())
def write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
def digest(p): return sha256(p.read_bytes()).hexdigest()


def run(parent, output):
    output.mkdir(parents=True, exist_ok=True)
    assert read(parent/'status.json')['status'] == 'paused_by_user'
    source = GraphState.model_validate(read(parent/'merged_state.json'))
    saved = sorted(parent.glob('cases/*/retrieval_result.json'))
    watched = [parent/'merged_state.json', parent/'protocol.json', *saved]
    watched += list(parent.glob('cases/*/*_prediction.json')) + list(parent.glob('cases/*/online.json'))
    before_hashes = {str(p): digest(p) for p in watched}
    engine = RetrieveDbCapability(relevance_filter=False, semantic_node_limit=30, physical_node_limit=50)
    rows = []
    for path in saved:
        original = read(path)
        state = {'request': {'question': original['question'], 'graph_state': source.model_dump(mode='json'),
            'graph_fingerprint': original['graph_fingerprint'], 'run_id': 'read-only-budget-'+path.parent.name},
            'initial_sql': original['initial_sql'], 'representations': original['representations'],
            'relevance': original['relevance']}
        result = RetrieveDbResult.model_validate(engine._workflow.nodes.assemble(state)['result'])
        # Public validation checks unchanged node/edge contents, complete refs,
        # alternative scopes and exact formula-input relations, not just counts.
        _validate_result(result, source.graph, original['question'], original['graph_fingerprint'], load_root_schema())
        graph = result.retrieved_graph_state
        old_types = Counter(n['node_type'] for n in original['relevant_graph']['nodes'])
        new_types = Counter(n.node_type for n in graph.graph.nodes) if graph else Counter()
        physical = {'table', 'column', 'dimension_value'}
        old_physical = sum(count for kind,count in old_types.items() if kind in physical)
        new_physical = sum(count for kind,count in new_types.items() if kind in physical)
        new_semantic = sum(new_types.values())-new_physical
        assert new_semantic <= 30 and new_physical <= 50
        budget = json.loads(next(row.observation for row in result.trace if row.action == 'final_node_budget'))
        row = {'case_id': path.parent.name, 'raw_candidate_nodes': len(original['raw_retrieved_node_ids']),
            'before_semantic_nodes': sum(old_types.values())-old_physical, 'before_physical_nodes': old_physical,
            'after_semantic_nodes': new_semantic, 'after_physical_nodes': new_physical,
            'before_inline_characters': len(json.dumps(original['relevant_graph'], ensure_ascii=False, separators=(',', ':'))),
            'after_inline_characters': len(json.dumps(result.relevant_graph, ensure_ascii=False, separators=(',', ':'))),
            'after_analysis_characters': len(json.dumps(_analysis_graph_payload(graph), ensure_ascii=False, separators=(',', ':'))) if graph else 0,
            'after_types': dict(new_types), 'full_public_validation': 'passed',
            'skipped_complete_units': budget['skipped_complete_units']}
        write(output/'cases'/path.parent.name/'before_graph.json', original['relevant_graph'])
        write(output/'cases'/path.parent.name/'after_graph.json', result.relevant_graph)
        write(output/'cases'/path.parent.name/'selection_audit.json', row)
        rows.append(row)
    assert all(digest(Path(p)) == value for p,value in before_hashes.items())
    result = {'checked_at': datetime.now().astimezone().isoformat(), 'cases': rows,
        'semantic_node_limit': 30, 'physical_node_limit': 50, 'provider_calls': 0,
        'source_files_unchanged': True, 'live_checkpoints_modified': False, 'full_evaluation_still_paused': True,
        'note': 'Only deterministic subgraph assembly is replayed; this does not claim EA improvement.'}
    write(output/'results.json', result)
    write(output/'source_preservation.json', {'before': before_hashes, 'all_unchanged': True})
    body = '<h1>最终检索子图：30 semantic / 50 physical</h1><p>'+escape(result['checked_at'])+'</p><p>真实已保存召回与相关性判断的只读重放；0次模型调用，不修改旧checkpoint、原图或预测，全量EA继续暂停。候选池大小单独保留审计，实际送入下游的子图必须满足上限。</p>'
    body += '<p>Semantic包括Concept、Metric、GenerationRule、Event；physical包括Table、Column、DimensionValue。一跳、完整公式、targets、完整conflict各分支、JOIN column_pairs全部计入；完整单元放不下则整体跳过，不改原节点内容。</p><table><tr><th>Case</th><th>Before semantic/physical</th><th>After semantic/physical</th><th>Inline chars before→after</th><th>Public validation</th></tr>'
    for row in rows:
        sid = row['case_id']
        body += f'<tr><td>{sid}</td><td>{row["before_semantic_nodes"]}/{row["before_physical_nodes"]}</td><td>{row["after_semantic_nodes"]}/{row["after_physical_nodes"]}</td><td>{row["before_inline_characters"]:,} → {row["after_inline_characters"]:,}</td><td>{row["full_public_validation"]}</td></tr>'
    body += '</table><h2>已删除新增的定义筛选</h2><p>按用户要求删除Concept/Metric/Event的任务级定义导出筛选及其schema、prompt与辅助代码。以30/50最终子图上限控制规模，保留原有澄清与GenerationRule适用性判断。</p>'
    for row in rows:
        sid = row['case_id']
        body += f'<details><summary>Case {sid}：完整前后子图和排除原因</summary><p><a href="cases/{sid}/before_graph.json">Before</a> · <a href="cases/{sid}/after_graph.json">After</a> · <a href="cases/{sid}/selection_audit.json">Selection audit</a></p><pre>'+escape(json.dumps(row, ensure_ascii=False, indent=2))+'</pre></details>'
    document = '<!doctype html><meta charset="utf-8"><title>Final retrieval node caps</title><style>body{font:16px/1.7 system-ui;max-width:1200px;margin:30px auto;padding:15px}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{border-bottom:1px solid #ddd;padding:8px}</style>'+body
    (output/'index.html').write_text(document)
    print(json.dumps({'output':str(output/'index.html'), 'cases':len(rows), 'max_semantic_nodes':max(r['after_semantic_nodes'] for r in rows),
        'max_physical_nodes':max(r['after_physical_nodes'] for r in rows), 'provider_calls':0,
        'examples':[{k:r[k] for k in ('case_id','before_semantic_nodes','before_physical_nodes','after_semantic_nodes','after_physical_nodes','before_inline_characters','after_inline_characters')} for r in rows[:4]]}, ensure_ascii=False))


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--parent',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();run(args.parent.resolve(),args.output.resolve())
