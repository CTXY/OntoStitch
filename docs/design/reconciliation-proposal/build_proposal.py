"""Build a complete reviewable design; no proposed protocol is deployed."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from copy import deepcopy
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool

try:
    from markdown_it import MarkdownIt
except ImportError:
    sys.path.append('/Library/Frameworks/Python.framework/Versions/3.7/lib/python3.7/site-packages')
    from markdown_it import MarkdownIt

from review_contracts import (TOOLS, DraftDocument, AssessmentReview, EditDraft, NodeRewriteValue,
    bind_review, bind_node_rewrite, bind_node_retirement, apply_edit_shape, submission_readiness, public_schema)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = REPO / 'output/reconciliation_proposal_20261120'
SIGNALS = ('abstract_predicate', 'binding_contest', 'parallel_assertions', 'triangle',
           'same_role_neighbors', 'related_decisions', 'source_selection_variants', 'value_concepts',
           'metric_direct_inputs', 'concept_metadata_repair', 'target_alternative_scope', 'node_relation_followup')
ROLE_FILES = {'writer': ('common.zh.md', 'writer.zh.md', 'writer-contract.zh.md'),
              'assessment': ('common.zh.md', 'assessment.zh.md', 'assessment-contract.zh.md')}
MODULE_SEPARATOR = '\n\n---\n\n'
DESCRIPTIONS = {
    'read_nodes': '按授权节点ID与before/candidate/source快照读取准确字段；source绑定归档且不提供before来源单元。',
    'read_evidence': '只读取本任务允许的原始证据IDs，controller对正文去重但保留归属。',
    'request_context': '基于已知锚点申请必要关系、准确目录或写契约；申请不自动获得无关读写权限。',
    'edit_draft': '以expected_version原子编辑canonical草稿；put_change/remove_change/put_decision不直接提交图。',
    'submit_draft': '只引用当前草稿版本；完整覆盖后编译实际候选并独立评估，不重发草稿全文。',
    'Review': '评估当前实际候选，逐Signal和逐pending旧issue复核；不修改草稿。',
}
DOCS = [('overview', '完整设计与当前实现差距', 'design.md'),
        ('ontology', 'Ontology结构与Concept契约', 'ontology-contract.md'),
        ('alternatives', 'Alternatives完整处理', 'alternatives.md'),
        ('selection', '粗节点/详关系与选择', 'selection.md'),
        ('contracts', '输入输出与草稿接口', 'contracts.md'),
        ('context', '每轮上下文/缓存/恢复', 'context-and-recovery.md'),
        ('writing', '按需节点写契约', 'conditional-writing.md'),
        ('simplification', '本轮Prompt精简与删除边界', 'prompt-simplification.md'),
        ('tradeoffs', '输入成本与取舍', 'tradeoffs-review.md'),
        ('review', '独立审查与风险', 'review.md')]
OUT.mkdir(parents=True, exist_ok=True)


def save(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else stable(value)).encode()).hexdigest()


def signal_guidance(role, signal):
    """One editable signal file; only shared facts and this role reach System."""
    text = (HERE / 'signals' / (signal + '.md')).read_text().rstrip()
    sections = re.split(r'(?m)^(## (?:Writer|Assessment)(?:：|[ \t])[^\n]+)\n', text)
    if len(sections) != 5 or not re.match(r'## Writer(?:：|[ \t])', sections[1]) or not re.match(r'## Assessment(?:：|[ \t])', sections[3]):
        raise ValueError(f'{signal}: expected shared facts, one Writer section, one Assessment section')
    offset = 1 if role == 'writer' else 3
    shared = sections[0].rstrip()
    title, body = shared.split('\n', 1)
    title += '处理规范' if role == 'writer' else '核查规范'
    return title + '\n' + body + '\n\n' + sections[offset] + '\n' + sections[offset + 1].strip() + '\n'


def system(role, signal):
    parts = [(HERE / 'prompts' / name).read_text().rstrip() for name in ROLE_FILES[role]]
    parts.append(signal_guidance(role, signal).rstrip())
    return MODULE_SEPARATOR.join(parts) + '\n'


def tools(role):
    names = ('read_nodes', 'read_evidence', 'request_context', 'edit_draft', 'submit_draft') if role == 'writer' else ('read_nodes', 'read_evidence', 'request_context', 'Review')
    result = []
    def clean(value):
        if isinstance(value, dict):
            if value.get('default', object()) is None:
                value.pop('default', None)
            for item in value.values():
                clean(item)
        elif isinstance(value, list):
            for item in value:
                clean(item)
    for name in names:
        tool = StructuredTool.from_function(lambda **kwargs: None, name=name,
            description=DESCRIPTIONS[name], args_schema=TOOLS[name])
        wire = convert_to_openai_tool(tool)
        clean(wire['function']['parameters'])
        result.append(wire)
    return result


def fixture():
    old = json.loads((REPO / 'tests/fixtures/node_local_metric_retirement.json').read_text())
    patch = old['coupled_record']['patch']['retirements'][0]
    owner = old['group']['owner_id']
    e2 = patch['before'][0]['id']; e1 = patch['after'][0]['id']
    node_rows = {n['id']: n for n in old['before']['graph']['nodes']}
    edge_rows = {e['id']: deepcopy(e) for e in old['before']['graph']['edges']}
    alias = {ident: owner for ident in old['node_draft']['integrate_groups'][0]['member_ids']}
    for row in edge_rows.values():
        row['from_node_id'] = alias.get(row['from_node_id'], row['from_node_id'])
        row['to_node_id'] = alias.get(row['to_node_id'], row['to_node_id'])
    # Preserve the exact relation records and formula chosen in the real saved
    # node draft; these messages demonstrate the proposed layout, not API logs.
    merged_content = old['node_draft']['integrate_groups'][0]['content']
    owner_row = {**node_rows[owner], **merged_content}
    ids = {owner, edge_rows[e1]['to_node_id'], edge_rows[e2]['to_node_id']}
    initial_nodes = []
    for ident in sorted(ids):
        row = owner_row if ident == owner else node_rows[ident]
        summary = {'id': ident, 'node_type': row['node_type'], 'name': row['name'],
            'summary': {'text': row.get('description') or row['name'], 'is_full_content': False},
            'available_fields': ['content', 'alternatives', 'source_units', *['properties.' + key for key in row['properties']]]}
        if row['node_type'] == 'concept' and row['properties'].get('relation_metadata'):
            summary['properties'] = {'relation_metadata': deepcopy(row['properties']['relation_metadata'])}
        initial_nodes.append(summary)
    assert all(row['from_node_id'] in ids and row['to_node_id'] in ids for row in (edge_rows[e1], edge_rows[e2]))
    evidence_ids = [row['id'] for row in old['evidence']]
    relations = [{key: value for key, value in edge_rows[eid].items() if key not in {'sources', 'reference_origin'}} |
        {'evidence_ids': [row['id'] for row in old['evidence'] if row['source'] in edge_rows[eid].get('sources', [])]}
        for eid in (e1, e2)]
    signal = {'id': 'S1', 'kind': 'metric_direct_inputs', 'mode': 'required',
        'observed': {'node_id': owner, 'formula_ref_ids': list(merged_content['properties']['expression']['refs'].values()),
                     'stale_edge_ids': [e2]},
        'check_question': '保留真实公式直接输入，并为多余旧依赖提供有依据的保义后继。',
        'anchor_ids': [owner, e1, e2]}
    task = {'task_id': 'proposal-metric-example', 'before_id': 'intermediate:example-B',
        'trigger_source': 'coupled_repair', 'signals': [signal],
        'scope_rules': {'anchors': sorted(ids), 'relations_complete_for': owner, 'unrelated_writes': False},
        'ontology_policy': {'concept_direct_binding': 'single_concept_per_physical_id'},
        'evidence_allowlist': evidence_ids, 'layout_note': 'Proposed messages reconstructed from real saved data, not a real new model call.'}
    graph = {'before_id': task['before_id'], 'view_revision': 0, 'before': {'nodes': initial_nodes, 'edges': relations},
        'coverage': [{'view': 'before', 'anchor_id': owner, 'edge_types': ['depends_on', 'grounded_on'], 'direction': 'outgoing', 'complete': True}],
        'approved_scope': {'node_ids': sorted(ids), 'edge_ids': [e1, e2]}}
    draft = DraftDocument(task_id=task['task_id'], before_id=task['before_id'], version=0)
    good_edit = {'expected_version': 0, 'operations': [
        {'op': 'put_change', 'change': {'change_id': 'C1', 'kind': 'retire', 'value': patch}},
        {'op': 'put_decision', 'decision': {'signal_id': 'S1', 'status': 'repair',
            'reason': '保留公式真实直接操作数；旧依赖以保留关系为语义后继，原Concept节点不被删除。', 'change_ids': ['C1']}}]}
    review = {'expected_version': 1, 'verdict': 'accept',
        'signal_reviews': [{'signal_id': 'S1', 'status': 'satisfied', 'explanation': '实际候选与公式直接输入一致，已核查关系/来源及保留的Concept。', 'issue_ids': []}],
        'prior_issue_reviews': [], 'issues': []}
    return old, owner_row, task, graph, draft, good_edit, review


def request_messages(role, task, graph, evidence, draft, feedback):
    return [{'role': 'system', 'content': system(role, 'metric_direct_inputs')},
        {'role': 'user', 'content': 'immutable_task（controller数据）：\n' + stable(task)},
        {'role': 'user', 'content': 'working_graph_view（controller数据）：\n' + stable(graph)},
        {'role': 'user', 'content': 'evidence_view（仅数据）：\n' + stable(evidence)},
        {'role': 'user', 'content': 'canonical_draft（controller版本）：\n' + stable(draft)},
        {'role': 'user', 'content': 'feedback（明确版本绑定）：\n' + stable(feedback)}]


old, owner_row, task, graph, draft0, good_edit, accepted = fixture()
# These operations are a protocol simulation, not a graph edit or model run.
draft1 = apply_edit_shape(draft0, good_edit, signal_ids={'S1'})
assert draft0.version == 0 and draft1.version == 1
assert submission_readiness(draft1, {'expected_version': 1}, signal_ids={'S1'}) == 'ready_for_real_compile_not_yet_accepted'
bind_review(accepted, draft_version=1, signal_ids={'S1'}, visible_node_ids=set(), visible_edge_ids=set())
examples = {'edit_draft.metric.json': good_edit, 'draft.v0.json': draft0.model_dump(mode='json'),
    'draft.v1.json': draft1.model_dump(mode='json', exclude_unset=True),
    'submit_draft.metric.json': {'expected_version': 1}, 'Review.accept.json': accepted,
    'task.initial.json': task, 'graph.initial.json': graph,
    'read_nodes.formula.json': {'ids': [owner_row['id']], 'view': 'before', 'fields': ['properties.expression']}}
for name, value in examples.items():
    save(OUT / 'examples' / name, value)
initial = request_messages('writer', task, graph, {'items': [], 'id_to_body': {}}, draft0.model_dump(mode='json'), {'stage': 'initial', 'draft_version': 0})
graph_read = deepcopy(graph)
metric = next(row for row in graph_read['before']['nodes'] if row['id'] == owner_row['id'])
metric['properties'] = {'expression': owner_row['properties']['expression']}
graph_read['view_revision'] = 1
read = request_messages('writer', task, graph_read, {'items': [], 'id_to_body': {}}, draft0.model_dump(mode='json'),
    {'stage': 'reading', 'draft_version': 0, 'read_receipt': {'ids': [owner_row['id']], 'fields': ['properties.expression'], 'view_revision': 1}})
edited = request_messages('writer', task, graph_read, {'items': [], 'id_to_body': {}}, draft1.model_dump(mode='json'),
    {'stage': 'editing', 'draft_version': 1, 'latest_receipt': {'status': 'saved_not_accepted'}})
for name, body in [('writer.initial.messages.json', initial), ('writer.after_read.messages.json', read), ('writer.after_edit.messages.json', edited)]:
    save(OUT / 'examples' / name, {'kind': 'proposed_layout_not_actual_model_request', 'messages': body})
assert len(initial) == len(read) == len(edited) == 6
assert initial[:2] == read[:2] == edited[:2]
assert read[2:4] == edited[2:4]
candidate_view = deepcopy(graph_read)
candidate_view['view_revision'] = 2
retirement = old['coupled_record']['patch']['retirements'][0]
old_edge_id = retirement['before'][0]['id']
survivor_id = retirement['after'][0]['id']
saved_after = REPO / 'output/node_consolidation_smoke_20261120_retry_live/carcinogenic_molecule_count/after.json'
if saved_after.exists():
    from cortex.schema import GraphState, load_root_schema
    from cortex.application.ontology_graph import GraphManager
    actual_candidate = GraphState.model_validate_json(saved_after.read_text())
    GraphManager(load_root_schema()).validate_state(actual_candidate)
    edge = next(item for item in actual_candidate.graph.edges if item.id == survivor_id)
    row = edge.model_dump(mode='json', exclude={'sources', 'reference_origin'})
    row['evidence_ids'] = [item['id'] for item in old['evidence'] if item['source'] in edge.model_dump(mode='json')['sources']]
    candidate_view['candidate'] = {'for_draft_version': 1,
        'unchanged_node_ids': [item['id'] for item in graph['before']['nodes']],
        'updated_edges': [row], 'deleted_edge_ids': [old_edge_id],
        'provenance': 'Candidate records from an earlier saved validated result; this message layout is proposed, not an actual request.'}
    assessment = request_messages('assessment', task, candidate_view, {'items': [], 'id_to_body': {}}, draft1.model_dump(mode='json'),
        {'stage':'assessing','draft_version':1,'candidate_for_version':1,'pending_recheck':[]})
    assert len(assessment) == 6
    save(OUT/'examples/assessment.initial.messages.json',{'kind':'proposed_layout_with_previously_saved_candidate_not_new_model_request','messages':assessment})

# Normalization of an old conflict into one new complete compatible Concept.
content = {'name': 'School', 'description': '同一学校实体，保留代码标识与名称显示两种匹配角色。',
           'aliases': ['学校'], 'tags': [], 'properties': {'targets': [['column.school.code'], ['column.school.name']]}}
manifest = {'owner_id': 'School', 'member_ids': ['School'], 'source_units': [
    {'unit_id': 'example-unit-code', 'node_id': 'School', 'kind': 'alternative', 'alternative_id': 'by-code'},
    {'unit_id': 'example-unit-name', 'node_id': 'School', 'kind': 'alternative', 'alternative_id': 'by-name'}]}
rewrite = {'node_id': 'School', 'content': content, 'source_routes': [
    {'source_unit_id': row['unit_id'], 'destination': {'kind': 'result'}, 'explanation': '该完整表示角色及知识保留在普通结果。'} for row in manifest['source_units']],
    'explanation': '已核实为同一实体的相容匹配方法；不是选择一个分支丢弃另一个。'}
bind_node_rewrite(rewrite, manifest=manifest, node_type='concept', allowed_evidence_ids=set())
save(OUT / 'examples/node_rewrite.compatible.json', {'kind': 'synthetic_review_example_not_actual_unit_handles', 'manifest': manifest, 'value': rewrite})

rejected = []
def rejects(name, operation):
    try:
        operation()
    except (ValueError, TypeError, KeyError):
        rejected.append(name)
    else:
        raise AssertionError('Expected rejection: ' + name)
rejects('过期编辑版本', lambda: apply_edit_shape(draft1, good_edit, signal_ids={'S1'}))
rejects('过期提交版本', lambda: submission_readiness(draft1, {'expected_version': 0}, signal_ids={'S1'}))
rejects('未知Signal不能伪造decision', lambda: apply_edit_shape(draft0, {'expected_version': 0, 'operations': [{'op':'put_decision','decision':{'signal_id':'OTHER','status':'keep','reason':'not authorized'}}]}, signal_ids={'S1'}))
rejects('remove_change不接受未知句柄', lambda: apply_edit_shape(draft0, {'expected_version': 0,'operations':[{'op':'remove_change','change_id':'missing'}]}, signal_ids={'S1'}))
rejects('同原子操作重复目标', lambda: apply_edit_shape(draft0, {'expected_version':0,'operations':[good_edit['operations'][0],good_edit['operations'][0]]}, signal_ids={'S1'}))
rejects('未覆盖Signal不能提交', lambda: submission_readiness(draft0, {'expected_version':0}, signal_ids={'S1'}))
rejects('额外包装层', lambda: EditDraft.model_validate({'patch':good_edit}))
rejects('repair必须引用change', lambda: TOOLS['edit_draft'].model_validate({'expected_version':0,'operations':[{'op':'put_decision','decision':{'signal_id':'S1','status':'repair','reason':'repair'}}]}))
rejects('退役元素不允许alternative_id', lambda: EditDraft.model_validate({'expected_version':0,'operations':[{'op':'put_change','change':{'change_id':'x','kind':'retire','value':{'before':[{'kind':'node','id':'N','alternative_id':'a'}],'after':[],'explanation':'bad scope'}}}]}))
rejects('keep不能引用修复', lambda: TOOLS['edit_draft'].model_validate({'expected_version':0,'operations':[{'op':'put_decision','decision':{'signal_id':'S1','status':'keep','reason':'keep','change_ids':['C1']}}]}))
invalid_review = deepcopy(accepted); invalid_review['expected_version'] = 0
rejects('评估过期候选', lambda: bind_review(invalid_review,draft_version=1,signal_ids={'S1'},visible_node_ids=set(),visible_edge_ids=set()))
rejects('漏评当前Signal', lambda: bind_review(accepted,draft_version=1,signal_ids={'S1','S2'},visible_node_ids=set(),visible_edge_ids=set()))
rejects('旧问题省略不能当解决', lambda: bind_review(accepted,draft_version=1,signal_ids={'S1'},visible_node_ids=set(),visible_edge_ids=set(),pending_issue_ids={'I1'}))
invalid_review = deepcopy(accepted); invalid_review['issues']=[{'issue_id':'I1','signal_ids':['S1'],'refs':[{'kind':'node','id':'N','view':'candidate'}],'problem':'bad','required_outcome':'fix'}]
rejects('accept不能带issue',lambda: AssessmentReview.model_validate(invalid_review))
invalid_rewrite = deepcopy(rewrite); invalid_rewrite['source_routes'].pop()
rejects('单owner重写不能漏来源单元',lambda: bind_node_rewrite(invalid_rewrite,manifest=manifest,node_type='concept',allowed_evidence_ids=set()))
invalid_rewrite = deepcopy(rewrite); invalid_rewrite['source_routes'][0]['destination']={'kind':'common'}
rejects('普通结果不能common去向',lambda: NodeRewriteValue.model_validate(invalid_rewrite))
invalid_rewrite = deepcopy(rewrite); invalid_rewrite['source_routes'][0]['destination']={'kind':'discard'}
normal_manifest=deepcopy(manifest);normal_manifest['source_units'][0]['kind']='normal_content'
rejects('normal单元不可discard',lambda: bind_node_rewrite(invalid_rewrite,manifest=normal_manifest,node_type='concept',allowed_evidence_ids=set()))
rejects('candidate读必须精确已编译版本',lambda: TOOLS['read_nodes'].model_validate({'ids':['N'],'view':'candidate','fields':['content']}))
rejects('source旧操作不在新kind',lambda: EditDraft.model_validate({'expected_version':0,'operations':[{'op':'put_change','change':{'change_id':'C','kind':'conflict_resolve','value':{}}}]}))
# A valid atomic edit followed by a bad operation cannot mutate the original.
prior=draft1.model_dump(mode='json')
rejects('原子编辑无部分应用',lambda: apply_edit_shape(draft1, {'expected_version':1,'operations':[{'op':'remove_change','change_id':'C1'},{'op':'remove_change','change_id':'unknown'}]},signal_ids={'S1'}))
assert draft1.model_dump(mode='json')==prior
rejects('提交评估阶段不接受迟到编辑',lambda:apply_edit_shape(draft1,{'expected_version':1,'operations':[{'op':'remove_change','change_id':'C1'}]},signal_ids={'S1'},current_stage='assessing'))
rejects('既有节点写契约必须anchor',lambda:TOOLS['request_context'].model_validate({'kind':'write_contract','node_type':'concept','operation_kind':'node_rewrite','reason':'rewrite'}))
conflict = {'node_id':'School','alternatives':[
    {'alternative_id':'A','content':content}, {'alternative_id':'B','content':{**content,'name':'School representation'}}],
    'conflict_points':[{'point_id':'P','aspect':'representation','description':'待比较表示','choices':[{'alternative_ids':['A'],'statement':'A'},{'alternative_ids':['B'],'statement':'B'}]}],
    'source_routes':[{'source_unit_id':unit['unit_id'],'destination':{'kind':'alternatives','alternative_ids':[branch]},'explanation':'保留完整来源'} for unit,branch in zip(manifest['source_units'],('A','B'))],
    'explanation':'仅shape示例，不证明语义真的冲突'}
bind_node_rewrite(conflict,manifest=manifest,node_type='concept',allowed_evidence_ids=set())
bad_conflict=deepcopy(conflict);bad_conflict['conflict_points'][0]['choices'][0]['alternative_ids']=['GHOST']
rejects('已有节点重写差异点不能指向假分支',lambda:NodeRewriteValue.model_validate(bad_conflict))
new_support={**rewrite,'evidence_ids':['E-new']}
bind_node_rewrite(new_support,manifest=manifest,node_type='concept',allowed_evidence_ids={'E-new'})
rejects('普通重写新证据也必须已授权',lambda:bind_node_rewrite(new_support,manifest=manifest,node_type='concept',allowed_evidence_ids=set()))
rejects('禁止空after退役整个节点绕过保义',lambda:TOOLS['edit_draft'].model_validate({'expected_version':0,'operations':[{'op':'put_change','change':{'change_id':'R','kind':'retire','value':{'before':[{'kind':'node','id':'School'}],'after':[],'explanation':'unsupported removal'}}}]}))
node_retirement={'before':[{'kind':'node','id':'School'}],'after':[{'kind':'node','id':'Successor'}],
    'explanation':'完整知识移至明确后继；这里只验证路由shape',
    'node_unit_routes':[{'source_node_id':'School','source_unit_id':row['unit_id'],'successors':[{'kind':'node','id':'Successor'}],'explanation':'原知识保留在后继'} for row in manifest['source_units']]}
bind_node_retirement(node_retirement,manifests={'School':manifest},available_successors={('node','Successor'):set()})
bad_node_retirement=deepcopy(node_retirement);bad_node_retirement['node_unit_routes'].pop()
rejects('整节点退役不能漏输入知识单元',lambda:bind_node_retirement(bad_node_retirement,manifests={'School':manifest},available_successors={('node','Successor'):set()}))
ref={'kind':'node','id':'N','view':'candidate','fields':['properties.targets']}
regression={'expected_version':1,'verdict':'needs_context','signal_reviews':[accepted['signal_reviews'][0]],
    'issues':[{'issue_id':'I-new','kind':'missing_context','signal_ids':[],'refs':[ref],'problem':'当前修改影响的额外节点缺决定性前提','required_outcome':'补齐当前影响范围内的前提'}]}
bind_review(regression,draft_version=1,signal_ids={'S1'},visible_node_ids={'N'},visible_edge_ids=set())
bad_review=deepcopy(regression);bad_review['signal_reviews'][0]['issue_ids']=['I-new']
rejects('satisfied不能挂其他阻塞issue',lambda:AssessmentReview.model_validate(bad_review))
claim_probe=deepcopy(regression);claim_probe['issues'][0]['claim']={'kind':'relation_absent','edge_type':'depends_on','from_node_id':'N','to_node_id':'GLOBAL','view':'candidate'}
rejects('claim不能借本地refs探测全局端点',lambda:bind_review(claim_probe,draft_version=1,signal_ids={'S1'},visible_node_ids={'N'},visible_edge_ids=set()))
rejects('归档source读取必须快照ID',lambda:TOOLS['read_nodes'].model_validate({'ids':['old-N'],'view':'source','fields':['content']}))
rejects('归档不能冒充当前来源路由单元',lambda:TOOLS['read_nodes'].model_validate({'ids':['old-N'],'view':'source','source_snapshot_id':'archive-1','fields':['source_units']}))
TOOLS['read_nodes'].model_validate({'ids':['old-N'],'view':'source','source_snapshot_id':'archive-1','fields':['content']})
historical_review={'expected_version':1,'verdict':'revise',
    'signal_reviews':[{'signal_id':'S1','status':'needs_revision','explanation':'与授权原始版本比较发现本次丢失','issue_ids':['I1']}],
    'issues':[{'issue_id':'I1','kind':'defect','signal_ids':['S1'],'refs':[ref],
        'basis':[{'kind':'node','id':'old-N','view':'source','source_snapshot_id':'archive-1','fields':['properties.targets']}],
        'problem':'来源完整匹配角色丢失','required_outcome':'保留原角色'}]}
bind_review(historical_review,draft_version=1,signal_ids={'S1'},visible_node_ids={'candidate':{'N'},'source':{'archive-1':{'old-N'}}},visible_edge_ids=set())
rejects('历史basis快照也要授权',lambda:bind_review(historical_review,draft_version=1,signal_ids={'S1'},visible_node_ids={'candidate':{'N'},'source':{'wrong-archive':{'old-N'}}},visible_edge_ids=set()))
closed=deepcopy(accepted);closed['prior_issue_reviews']=[{'issue_id':'I-old','status':'resolved','explanation':'已针对当前候选复核关闭','issue_ids':[]}]
bind_review(closed,draft_version=1,signal_ids={'S1'},visible_node_ids=set(),visible_edge_ids=set(),pending_issue_ids={'I-old'})
for name,value in [('node_rewrite.new_support.json',new_support),('node_retirement.preservation.json',node_retirement),('Review.new_regression.json',regression),('Review.old_issue_closed.json',closed),('Review.historical_basis.json',historical_review)]:
    save(OUT/'examples'/name,value)

for role in ROLE_FILES:
    save(OUT / (role+'.tools.proposed.json'), tools(role))
    base=MODULE_SEPARATOR.join((HERE/'prompts'/name).read_text().rstrip() for name in ROLE_FILES[role])+MODULE_SEPARATOR
    for signal in SIGNALS:
        value=system(role,signal)
        assert value.startswith(base)
        assert value[len(base):] == signal_guidance(role, signal)
        opposite = 'Assessment' if role == 'writer' else 'Writer'
        assert not re.search(rf'(?m)^## {opposite}(?:：|[ \t])', value)
        assert not re.search(r'\b(?:submit_patch|MergePatch)\b', value)
        if role == 'writer':
            assert not re.search(r'\b(?:Review|accept|revise|revisit_identity|signal_reviews|prior_issue_reviews)\b', value)
        (OUT/(role+'.'+signal+'.system.zh.md')).write_text(value)
    assert all(stable(tools(role))==stable(tools(role)) for _ in SIGNALS)
for name, model in TOOLS.items():
    save(OUT / 'schemas' / (name + '.model.schema.json'), public_schema(model))

# Compare like-for-like SDK-converted parameters, not model-definition schemas
# on one side and SDK-normalized schemas on the other.
cost_path = HERE / 'tradeoffs-review.md'
old_tools = {role: json.loads((REPO / 'output/reconciliation_review_20261120' / (role + '.tools.actual.json')).read_text()) for role in ROLE_FILES}
new_tools = {role: tools(role) for role in ROLE_FILES}
parameter_size = lambda items: sum(len(stable(item['function']['parameters'])) for item in items)
per_tool = {item['function']['name']: len(stable(item['function']['parameters'])) for rows in new_tools.values() for item in rows}
comparison = [('Writer 全部工具', parameter_size(old_tools['writer']), parameter_size(new_tools['writer'])),
              ('Assessment 全部工具', parameter_size(old_tools['assessment']), parameter_size(new_tools['assessment'])),
              ('单独写结果/编辑工具：MergePatch → edit_draft', len(stable(old_tools['writer'][-1]['function']['parameters'])), per_tool['edit_draft']),
              ('单独评估结果：Review → 新 Review', len(stable(old_tools['assessment'][-1]['function']['parameters'])), per_tool['Review'])]
cost_text = cost_path.read_text()
start, end = cost_text.index('| Parameters schema |'), cost_text.index('\n\n拟议读取/提交')
table = '| Parameters schema | 原接口字符数 | 拟议终稿字符数 | 差额 |\n| --- | ---: | ---: | ---: |\n'
for title, old_size, new_size in comparison:
    table += f'| {title} | {old_size:,} | {new_size:,} | {new_size-old_size:+,}（{(new_size/old_size-1)*100:+.1f}%） |\n'
cost_text = cost_text[:start] + table.rstrip() + cost_text[end:]
cost_text = re.sub(r'拟议读取/提交 schema 分别为 .*?字符。', '拟议读取/提交 schema 分别为 ' + '、'.join(f'{name} {per_tool[name]:,}' for name in ('read_nodes','read_evidence','request_context','submit_draft')) + ' 字符。', cost_text, count=1)
prefixes = {role: len(MODULE_SEPARATOR.join((HERE/'prompts'/name).read_text().rstrip() for name in ROLE_FILES[role])+MODULE_SEPARATOR) for role in ROLE_FILES}
ranges = {role: (min(len(system(role, signal)) for signal in SIGNALS), max(len(system(role, signal)) for signal in SIGNALS)) for role in ROLE_FILES}
before_path = OUT / 'prompt_simplification.before.json'
before_metrics = json.loads(before_path.read_text()) if before_path.exists() else {}
prompt_changes = {}
for role in ROLE_FILES:
    for signal in SIGNALS:
        key = role + ':' + signal
        current = len(system(role, signal))
        previous = before_metrics.get(key, {}).get('characters')
        prompt_changes[key] = {'before_characters': previous, 'after_characters': current,
                              'reduction_percent': round(100 * (previous-current) / previous, 1) if previous else None}
save(OUT / 'prompt_simplification.json', {
    'scope': 'proposal System text only; not runtime or token/performance measurement',
    'variants': prompt_changes, 'role_isolation_checked': True,
    'tool_names': {role: [item['function']['name'] for item in tools(role)] for role in ROLE_FILES},
    'new_model_calls': 0})
simplification_path = HERE / 'prompt-simplification.md'
simplification = simplification_path.read_text()
summary = '| 完整 System | 精简前字符 | 精简后字符 | 减少 |\n| --- | ---: | ---: | ---: |\n'
for role in ROLE_FILES:
    for signal in ('abstract_predicate', 'source_selection_variants'):
        row = prompt_changes[role + ':' + signal]
        summary += f'| {role} / {signal} | {row["before_characters"] or "未记录"} | {row["after_characters"]} | {str(row["reduction_percent"])+"%" if row["reduction_percent"] is not None else "未记录"} |\n'
simplification = re.sub(r'(?s)(<!-- METRICS START -->).*?(<!-- METRICS END -->)',
                        lambda match: match[1] + '\n\n' + summary + '\n' + match[2], simplification)
simplification_path.write_text(simplification)
start, end = cost_text.index('| 单 Signal 组合 |'), cost_text.index('\n\n范围包含')
w, a = ranges['writer'], ranges['assessment']
table = f'''| 单 Signal 组合 | Writer 字符范围 | Assessment 字符范围 |
| --- | ---: | ---: |
| 共用+角色+输出协议前缀 | {prefixes['writer']:,} | {prefixes['assessment']:,} |
| 加一个当前 guidance 后完整 System | {w[0]:,}–{w[1]:,} | {a[0]:,}–{a[1]:,} |
| 再加该角色全部工具 parameters | {w[0]+parameter_size(new_tools['writer']):,}–{w[1]+parameter_size(new_tools['writer']):,} | {a[0]+parameter_size(new_tools['assessment']):,}–{a[1]+parameter_size(new_tools['assessment']):,} |'''
cost_text = cost_text[:start] + table + cost_text[end:]
cost_text = cost_text.replace('统计口径为每个工具 parameters 的紧凑 JSON 字符数', '统计口径为双方均经当前 LangChain convert_to_openai_tool 转换后的 parameters 紧凑 JSON 字符数（新协议移除空默认元数据）')
cost_text = cost_text.replace('旧 Writer 对照为当前六个读取工具与 MergePatch，旧 Assessment 为同一读取工具集与 Review', '旧接口基线取已保存的实际工具定义 writer.tools.actual.json / assessment.tools.actual.json；Writer为六个读取工具与MergePatch，Assessment为同一读取工具集与Review')
cost_text = cost_text.replace('最终 schema **确实略大于原接口**', '最终 schema **明显大于原接口**')
cost_text = cost_text.replace('九份 Signal/强制义务 guidance', '十二份 Signal/强制义务任务 guidance（另有父总览仅作参考）').replace('不能把九份全部', '不能把全部模块').replace('八类 signal 和强制义务模块中的一个', '八类 signal 和四类强制义务叶子模块中的一个')
params = {role: [item['function']['parameters'] for item in rows] for role, rows in new_tools.items()}
cost_text = re.sub(r'参数集合的规范化 JSON SHA256 为 `[^`]+`；review_contracts.py 源码 SHA256 为 `[^`]+`', f'参数集合的规范化 JSON SHA256 为 `{digest(params)}`；review_contracts.py 源码 SHA256 为 `{hashlib.sha256((HERE/"review_contracts.py").read_bytes()).hexdigest()}`', cost_text)
cost_path.write_text(cost_text)
save(OUT/'cost_metrics.json', {'parameter_character_comparison':comparison, 'per_tool':per_tool,
    'system_ranges':ranges, 'scope':'same SDK-converted parameter schema; not tokens or provider performance', 'new_model_calls':0})
validation={'status':'passed','scope':'proposal_shapes_examples_and_layout_only_not_runtime',
    'negative_examples':rejected,'negative_example_count':len(rejected),
    'single_owner_normal_rewrite_example':'validated', 'fixed_six_message_layout':True,
    'system_signal_guidance_last':True,'same_role_tool_schema_stable':True,
    'role_specific_signal_guidance': True, 'legacy_interfaces_in_prompts': False,
    'writer_receives_assessment_output_contract': False,
    'new_model_calls':0,'limitations':'No new runtime editor, permission gate, compiler adapter, durable checkpoint migration or model behavior validation was implemented.'}
save(OUT/'validation.json',validation)

md=MarkdownIt('commonmark').enable('table')
links={name:'#'+ident for ident,_,name in DOCS}
def render(name):
    text=(HERE/name).read_text()
    for target,anchor in links.items():text=text.replace(']('+target+')',']('+anchor+')')
    return md.render(text)
style='''body{font:16px/1.85 system-ui,"PingFang SC",sans-serif;color:#203147;background:#f5f7fa;margin:0}main{max-width:1180px;margin:auto;padding:24px}nav{position:sticky;top:0;background:#fff;border-bottom:1px solid #d5deea;padding:12px 20px;display:flex;gap:14px;flex-wrap:wrap;z-index:3}a{color:#175fb2}section{background:#fff;border:1px solid #d5deea;border-radius:10px;margin:22px 0;padding:24px;scroll-margin-top:110px}h1,h2,h3{line-height:1.5}h1{font-size:26px}h2{font-size:22px}h3{font-size:18px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:9px;border:1px solid #d5deea;vertical-align:top;text-align:left}th{background:#eef3f8}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef3f8;padding:16px;font:13px/1.7 ui-monospace,monospace}details{border:1px solid #d5deea;border-radius:7px;margin:14px 0;padding:14px}summary{cursor:pointer;font-weight:650}.status{background:#fff3d4;border:1px solid #d5b96e;padding:14px;border-radius:8px}.muted{color:#54677a}@media(max-width:700px){main{padding:12px}section{padding:14px}table{display:block;overflow:auto}}'''
page=['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reconciliation 完整设计方案 · 待 Review</title><style>'+style+'</style></head><body><nav>']
for ident,title,_ in DOCS:page.append(f'<a href="#{ident}">{escape(title)}</a>')
page+=['<a href="#signals">逐Signal Guidance</a><a href="#prompts">两Agent完整Prompt</a><a href="#examples">工具与六条上下文示例</a></nav><main><h1>图关系协调 Reconciliation：完整拟议方案</h1>',
 '<p class="status">仅设计与复阅：粗节点、详关系；Concept 内的多种 targets 匹配与 relation_metadata；完整 alternatives；版本化草稿；逐 Signal 独立评估。新协议未接 runtime，未迁移 checkpoint，未调用模型。</p>']
for ident,_,name in DOCS:page.append(f'<section id="{ident}">{render(name)}</section>')
page.append('<section id="signals"><h1>八类 Signal 与强制义务：逐类 Guidance</h1><p>每个文件分为共用判断依据、Writer处理、Assessment核查。此处展示完整可编辑来源；实际System只包含当前角色的部分，Writer不会收到Assessment核查清单。</p>')
for signal in SIGNALS:
    page.append(f'<details id="signal-{signal}"><summary>{signal}</summary>{md.render((HERE/"signals"/(signal+".md")).read_text())}</details>')
page.append('</section><section id="prompts"><h1>两个 Agent 的完整拟议 System</h1><p>共用图契约 → 当前角色 → 当前输出/工具协议 → 当前Signal的共用依据与当前角色指导。下表每项都是完整拼接文本；不同Signal仅尾部变化，工具schema固定，不混入另一角色协议。</p><table><tr><th>Signal</th><th>Writer字符/全文</th><th>Assessment字符/全文</th></tr>')
metrics={}
for signal in SIGNALS:
    page.append('<tr><td>'+signal+'</td>')
    for role in ROLE_FILES:
        body=system(role,signal);name=role+'.'+signal+'.system.zh.md'
        metrics[role+':'+signal]={'characters':len(body),'sha256':digest(body),'file':name}
        page.append(f'<td>{len(body)} · <a href="{name}">完整中文System</a></td>')
    page.append('</tr>')
page.append('</table><p><a href="prompt_simplification.json">本轮精简前后对照与校验</a>。以下展开 abstract_predicate 的两份完整System，便于与用户提供的示例对照。</p>')
for role in ROLE_FILES:
    page.append(f'<details open><summary>{role} / abstract_predicate 完整Prompt</summary>{md.render(system(role,"abstract_predicate"))}</details>')
page.append('<h2>源模块</h2>')
for name in ('common.zh.md','writer.zh.md','writer-contract.zh.md','assessment.zh.md','assessment-contract.zh.md'):
    page.append(f'<details><summary>{name}</summary>{md.render((HERE/"prompts"/name).read_text())}</details>')
page.append('</section><section id="examples"><h1>拟议工具与上下文示例</h1><p>本节是从既有Metric记录构造的新协议示意，不是新模型请求或真实执行成功。工具shape/schema与版本行为在本地校验；真实权限/编译/恢复尚待实现。</p>')
for role in ROLE_FILES:page.append(f'<p><a href="{role}.tools.proposed.json">{role} 完整工具JSON Schema</a></p>')
for path in sorted((OUT/'examples').glob('*.json')):
    page.append(f'<details><summary>{escape(path.name)}</summary><p><a href="examples/{path.name}">JSON</a></p><pre>{escape(path.read_text())}</pre></details>')
page.append('</section><section id="validation"><h1>方案校验与范围</h1><pre>'+escape(json.dumps(validation,ensure_ascii=False,indent=2))+'</pre></section></main></body></html>')
(OUT/'index.html').write_text(''.join(page))
save(OUT/'manifest.json',{'created_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
    'status':'design_proposal_not_implemented','assembled_systems':metrics,'validation':validation,
    'source_documents':{str(path.relative_to(HERE)):digest(path.read_text()) for path in HERE.rglob('*.md')}})
print(json.dumps({'html':str(OUT/'index.html'),'systems':len(metrics),'negative_checks':len(rejected),'new_model_calls':0},ensure_ascii=False))
