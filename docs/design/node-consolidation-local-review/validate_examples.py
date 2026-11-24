"""Check review examples with real validators; does not call a model or touch jobs."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch, _integrate
from cortex.capabilities.graph_merge.consolidation.review import Review, validate_review
from cortex.capabilities.graph_merge.graph.source_units import (
    SourceDestination,
    source_unit_manifest,
)
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    load_root_schema,
    validate_graph_state,
)
from cortex.schema.relation_metadata import validate_metadata_items
from cortex.schema.semantic_validation import validate_semantic_properties

HERE = Path(__file__).resolve().parent


def validate_examples():
    examples = json.loads((HERE / 'output_examples.json').read_text())
    schema = load_root_schema()
    counts = {'public_output_examples': 0, 'local_integrations': 0,
              'additional_type_properties': 0, 'rejected_invalid_examples': 0}
    for name, value in examples.items():
        if name.startswith('writer_'):
            MergePatch.model_validate(value)
            counts['public_output_examples'] += 1
        if name.startswith('assessment_'):
            Review.model_validate(value)
            counts['public_output_examples'] += 1

    physical = [
        GraphNode(id='table-register', node_type='table', name='register', properties={'table_name': 'register'}),
        GraphNode(id='table-detail', node_type='table', name='detail', properties={'table_name': 'detail'}),
        GraphNode(id='table-patient', node_type='table', name='patient', properties={'table_name': 'patient'}),
        GraphNode(id='table.orders', node_type='table', name='orders', properties={'table_name': 'orders'}),
        GraphNode(id='column-register-date', node_type='column', name='date', properties={'table_ref': 'table-register', 'column_name': 'date'}),
        GraphNode(id='column-detail-date', node_type='column', name='date', properties={'table_ref': 'table-detail', 'column_name': 'date'}),
        GraphNode(id='column.patient.temperature', node_type='column', name='temperature', properties={'table_ref': 'table-patient', 'column_name': 'temperature'}),
        GraphNode(id='column.orders.amount', node_type='column', name='amount', properties={'table_ref': 'table.orders', 'column_name': 'amount'}),
    ]

    def fixture(name, node_type):
        patch = MergePatch.model_validate(examples[name])
        group = patch.integrate_groups[0]
        contents = [a.content.model_dump(mode='json') for a in group.alternatives]
        if not contents:
            contents = [deepcopy(group.content.model_dump(mode='json')) for _ in group.member_ids]
            if node_type == 'concept':
                for index, content in enumerate(contents):
                    content['properties']['targets'] = [group.content.properties['targets'][index]]
            if node_type == 'generation_rule':
                for value, content in zip((3, 5), contents):
                    for key in ('condition', 'action'):
                        content['properties'][key] = content['properties'][key].replace('K', str(value))
        members = [GraphNode(id=key, node_type=node_type, **content)
                   for key, content in zip(group.member_ids, contents)]
        base = GraphState(graph=GraphSpace(nodes=(*physical, *members), edges=()))
        obligations = [{'owner_id': group.owner_id, 'member_ids': list(group.member_ids)}]
        units = source_unit_manifest(base, obligations)[0]['source_units']
        # Example handles are illustrative; real calls must copy the real manifest.
        routes = [route.model_copy(update={'source_unit_id': unit['unit_id']})
                  for route, unit in zip(group.source_routes, units)]
        return base, group.model_copy(update={'source_routes': tuple(routes)}), obligations

    fixtures = {}
    for name, kind in (('writer_ready', 'concept'), ('writer_concept_conflict', 'concept'),
                       ('writer_rule_generalized', 'generation_rule'),
                       ('writer_metric_complete', 'metric'), ('writer_event_complete', 'event')):
        base, group, obligations = fixture(name, kind)
        validate_graph_state(base, schema)
        integrated, *_ = _integrate(base, [group], obligations, SimpleNamespace(evidence=()))
        validate_graph_state(integrated, schema)
        owner = next(n for n in integrated.graph.nodes if n.id == group.owner_id)
        if group.alternatives:
            for branch in owner.alternatives:
                resolved = GraphManager.resolve_alternative(owner, branch)
                validate_semantic_properties(kind, resolved.properties)
        else:
            validate_semantic_properties(kind, owner.properties)
        fixtures[name] = (base, group, obligations)
        counts['local_integrations'] += 1

    # Each type guidance contains at least one complete NodeContent illustration.
    # Validate these exact JSON blocks so the prose examples cannot silently drift.
    import re

    from cortex.schema import NodeContent

    counts['type_guidance_content_examples'] = 0
    for kind in ('concept', 'metric', 'event', 'generation_rule'):
        found = 0
        for block in re.findall(r'```json\n(.*?)\n```', (HERE / 'types' / (kind + '.zh.md')).read_text(), re.DOTALL):
            value = json.loads(block)
            if isinstance(value, dict) and set(value) == {'name', 'description', 'aliases', 'tags', 'properties'}:
                content = NodeContent.model_validate(value)
                validate_semantic_properties(kind, content.properties)
                counts['type_guidance_content_examples'] += 1
                found += 1
        assert found >= 1, f'Missing complete guidance content example: {kind}'

    conflict_base, conflict_group, conflict_plans = fixtures['writer_concept_conflict']
    state, *_ = _integrate(conflict_base, [conflict_group], conflict_plans, SimpleNamespace(evidence=()))
    owner = next(n for n in state.graph.nodes if n.id == conflict_group.owner_id)
    branches = []
    for branch in owner.alternatives:
        content = GraphManager.resolve_alternative(owner, branch)
        if branch.alternative_id == 'threshold-38':
            content = content.model_copy(update={'properties': {**content.properties,
                'target_expressions': {'0': 'patient.temperature >= 37.5'}}})
        branches.append((branch.alternative_id, content, branch.sources))
    wrong, _ = GraphManager.compact_conflict(owner, branches, conflict_points=owner.conflict_points)
    wrong_state = state.model_copy(update={'graph': state.graph.model_copy(update={
        'nodes': tuple(wrong if n.id == wrong.id else n for n in state.graph.nodes)})})
    validate_graph_state(wrong_state, schema)
    validate_review(examples['assessment_alternative_reference'], SimpleNamespace(schema=schema, candidate=wrong_state),
                    before=conflict_base, candidate=wrong_state)
    counts['branch_review_real_reference_validation'] = 1

    for kind, props in (
        ('metric', {'expression': {'expr': 'SUM(${amount})', 'refs': {'amount': 'column.orders.amount'}}}),
        ('event', {'definition': '客户提交订单这一业务发生类型；以订单粒度保留参与角色。'}),
    ):
        validate_semantic_properties(kind, props)
        counts['additional_type_properties'] += 1

    rejected = []

    def expect_rejection(name, callback, *, expected_code=None):
        try:
            callback()
        except (ValueError, TypeError) as error:
            if expected_code is not None:
                assert getattr(error, 'details', {}).get('code') == expected_code
            rejected.append(name)
            return
        raise AssertionError(f'The actual validator unexpectedly accepted: {name}')

    bad = deepcopy(examples['writer_ready'])
    del bad['integrate_groups'][0]['content']['aliases']
    expect_rejection('NodeContent 缺少 aliases', lambda: MergePatch.model_validate(bad))
    expect_rejection('错误 patch 包装', lambda: MergePatch.model_validate({'patch': examples['writer_ready']}))
    bad_dual = deepcopy(examples['writer_concept_conflict'])
    bad_dual['integrate_groups'][0]['content'] = examples['writer_ready']['integrate_groups'][0]['content']
    expect_rejection('content 与 alternatives 同时提交', lambda: MergePatch.model_validate(bad_dual))

    base, group, obligations = fixtures['writer_ready']

    def integrate_group(value, source=base, plans=obligations):
        return _integrate(source, [value], plans, SimpleNamespace(evidence=()))

    expect_rejection('来源单元遗漏', lambda: integrate_group(group.model_copy(update={'source_routes': group.source_routes[:1]})))
    expect_rejection('来源单元重复', lambda: integrate_group(group.model_copy(update={'source_routes': (*group.source_routes, group.source_routes[0])})))
    route = group.source_routes[0].model_copy(update={'destination': SourceDestination(kind='discard')})
    expect_rejection('普通来源单元被 discard', lambda: integrate_group(group.model_copy(update={'source_routes': (route, *group.source_routes[1:])})))
    expect_rejection('错误 member 集合', lambda: integrate_group(group.model_copy(update={'member_ids': (group.owner_id, 'unknown-member')})))

    conflict_base, conflict_group, conflict_plans = fixtures['writer_concept_conflict']
    unknown_route = conflict_group.source_routes[0].model_copy(update={
        'destination': conflict_group.source_routes[0].destination.model_copy(update={'alternative_ids': ('unknown-branch',)})})
    unknown_group = conflict_group.model_copy(update={'source_routes': (unknown_route, *conflict_group.source_routes[1:])})
    expect_rejection('路由引用不存在的输出分支', lambda: integrate_group(unknown_group, conflict_base, conflict_plans))
    point = conflict_group.conflict_points[0]
    wrong_choice = point.choices[0].model_copy(update={'alternative_ids': ('unknown-branch',)})
    wrong_point = point.model_copy(update={'choices': (wrong_choice, *point.choices[1:])})
    bad_group = conflict_group.model_copy(update={'conflict_points': (wrong_point,)})
    expect_rejection('conflict_points 引用不存在分支', lambda: integrate_group(bad_group, conflict_base, conflict_plans))

    from cortex.schema import SourceReference

    third = conflict_group.alternatives[0].model_copy(update={'alternative_id': 'unsupported-third'})
    only_common = conflict_group.model_copy(update={
        'alternatives': (*conflict_group.alternatives, third), 'common_evidence_ids': ('teaching-common',)})
    common_context = SimpleNamespace(evidence=(SimpleNamespace(id='teaching-common', source=SourceReference(source_id='teaching-common')),))
    expect_rejection('仅 common_evidence_ids 不能支持无路由新分支',
                     lambda: _integrate(conflict_base, [only_common], conflict_plans, common_context),
                     expected_code='unsupported_result_alternative')

    expect_rejection('targets 组内重复', lambda: validate_semantic_properties('concept', {'targets': [['a', 'a']]}))
    expect_rejection('target_expressions 下标越界', lambda: validate_semantic_properties('concept', {'targets': [['a']], 'target_expressions': {'1': 't.a > 0'}}))
    expect_rejection('重复完整匹配组', lambda: validate_semantic_properties('concept', {'targets': [['a'], ['a']]}))
    expect_rejection('Metric refs 与占位符不匹配', lambda: validate_semantic_properties('metric', {'expression': {'expr': 'SUM(${amount})', 'refs': {'other': 'column.orders.amount'}}}))
    expect_rejection('Metric 裸列', lambda: validate_semantic_properties('metric', {'expression': {'expr': '${amount}', 'refs': {'amount': 'column.orders.amount'}}}))
    expect_rejection('Metric 恒等加零包装', lambda: validate_semantic_properties('metric', {'expression': {'expr': '${amount}+0', 'refs': {'amount': 'column.orders.amount'}}}))
    expect_rejection('Event 无 targets 却保留表达式', lambda: validate_semantic_properties('event', {'definition': '订单提交', 'target_expressions': {'0': 'orders.id IS NOT NULL'}}))
    expect_rejection('Event 完整匹配组重复', lambda: validate_semantic_properties('event', {'definition': '订单提交', 'targets': [['a'], ['a']]}))
    rule = deepcopy(examples['writer_rule_generalized']['integrate_groups'][0]['content']['properties'])
    rule['triggers'] = {'question': {'keywords': ['前']}}
    expect_rejection('Rule trigger 使用错误 keywords 字段', lambda: validate_semantic_properties('generation_rule', rule))
    missing_reason = deepcopy(examples['writer_rule_generalized']['integrate_groups'][0]['content']['properties'])
    missing_reason['rationale'] = missing_reason.pop('reason')
    expect_rejection('Rule 把 reason 写成 rationale', lambda: validate_semantic_properties('generation_rule', missing_reason))
    expect_rejection('metadata 只有一条边', lambda: validate_metadata_items([{'id': 'g', 'edges': ['e'], 'description': 'Only one edge.'}]))
    expect_rejection('Review accept 却有 issues', lambda: Review.model_validate({'verdict': 'accept', 'issues': examples['assessment_revise']['issues']}))
    no_basis = deepcopy(examples['assessment_revise'])
    no_basis['issues'][0]['basis'] = []
    expect_rejection('Review revise 没有 basis', lambda: Review.model_validate(no_basis))
    invalid_ref = deepcopy(examples['assessment_alternative_reference'])
    invalid_ref['issues'][0]['refs'][0]['alternative_id'] = 'threshold-38'
    expect_rejection('ReviewReference 非法 alternative_id 字段', lambda: Review.model_validate(invalid_ref))

    bad_edge = GraphEdge(id='bad-edge', edge_type='grounded_on', from_node_id=group.owner_id,
                         to_node_id='column-register-date', properties={'description': 'Direct match.', 'condition': 'Invented property.'})
    bad_state = base.model_copy(update={'graph': base.graph.model_copy(update={'edges': (bad_edge,)})})
    expect_rejection('边新增 condition 属性', lambda: validate_graph_state(bad_state, schema))
    counts['rejected_invalid_examples'] = len(rejected)
    return {'status': 'passed', **counts, 'rejected_examples': rejected,
            'limits': 'No model call; no live graph or checkpoint changes. Full task runner, edge-handle adapter and source-scope deployment are outside this documentation validation.'}


def make_input_example():
    """A fully concrete synthetic Concept packet, using current schema and unit IDs."""
    from cortex.schema import SourceReference

    schema = load_root_schema()
    members = [GraphNode(id='concept-A', node_type='concept', name='登记日期',
                         description='登记记录的日期。', properties={'targets': [['column-register-date']]},
                         sources=(SourceReference(source_id='teaching-case-A'),)),
               GraphNode(id='concept-B', node_type='concept', name='登记日',
                         description='同一登记日期的相容明细表示。', properties={'targets': [['column-detail-date']]},
                         sources=(SourceReference(source_id='teaching-case-B'),))]
    physical = [GraphNode(id='table-register', node_type='table', name='register', properties={'table_name': 'register'}),
                GraphNode(id='table-detail', node_type='table', name='detail', properties={'table_name': 'detail'}),
                GraphNode(id='column-register-date', node_type='column', name='date', properties={'table_ref': 'table-register', 'column_name': 'date'}),
                GraphNode(id='column-detail-date', node_type='column', name='date', properties={'table_ref': 'table-detail', 'column_name': 'date'})]
    edges = [GraphEdge(id='edge-A', edge_type='grounded_on', from_node_id='concept-A', to_node_id='column-register-date', properties={'description': '登记日期的直接物理表示。'}),
             GraphEdge(id='edge-B', edge_type='grounded_on', from_node_id='concept-B', to_node_id='column-detail-date', properties={'description': '登记日期的相容明细表示。'})]
    base = GraphState(graph=GraphSpace(nodes=(*physical, *members), edges=tuple(edges)))
    obligations = [{'owner_id': 'concept-A', 'member_ids': ['concept-A', 'concept-B']}]
    manifest = source_unit_manifest(base, obligations)
    content = {'name': '登记日期', 'description': '登记发生的日期，保留主登记与明细表示。',
               'aliases': ['登记日'], 'tags': [],
               'properties': {'targets': [['column-register-date'], ['column-detail-date']]}}
    patch = MergePatch.model_validate({'integrate_groups': [{**obligations[0], 'content': content,
        'source_routes': [{'source_unit_id': unit['unit_id'], 'destination': {'kind': 'result'},
                           'explanation': '原匹配方法保留为独立目标组。'} for unit in manifest[0]['source_units']],
        'explanation': '同一身份的相容表示合并，出边继承。'}], 'explanation': '保留两种相容表示和关系，不新增边。'})
    integrated, _, aliases, edge_aliases = _integrate(base, list(patch.integrate_groups), obligations, SimpleNamespace(evidence=()))
    validate_graph_state(base, schema)
    validate_graph_state(integrated, schema)

    def node_view(node):
        row = node.model_dump(mode='json', exclude={'sources', 'common_sources', 'merge_status', 'consolidation_status', 'preservation_reasons'})
        row['evidence_ids'] = ['evidence::' + source.source_id for source in node.sources]
        return row

    def edge_view(edge):
        return edge.model_dump(mode='json', exclude={'sources'}) | {'evidence_ids': []}

    group = {'group_id': 'teaching-group', 'node_type': 'concept', **obligations[0], 'members': [node_view(n) for n in members]}
    common = {'task_id': 'teaching-local-merge', 'group': group,
              'node_schema': {'node_type': 'concept', 'fields': {k: v.model_dump(mode='json') for k, v in schema.effective_node_fields('concept').items()}},
              'source_unit_manifest': manifest,
              'endpoint_directory': [node_view(n) for n in physical], 'consumer_constraints': [],
              'relation_contracts': [schema.edge_type_index()['grounded_on'].model_dump(mode='json')]}
    writer = {**common, 'relations': {'outgoing': [edge_view(e) for e in edges], 'incoming': []},
              'completeness': {'member_content': True, 'outgoing_relations': True, 'incoming_relations': True, 'required_dependencies': True}}
    candidate = next(n for n in integrated.graph.nodes if n.id == 'concept-A')
    assessment = {**common, 'before_relations': writer['relations'],
                  'candidate': {'node': node_view(candidate), 'relations': {'outgoing': [edge_view(e) for e in integrated.graph.edges], 'incoming': []}},
                  'change_map': {'node_aliases': aliases, 'edge_aliases': edge_aliases, 'branch_routes': [], 'removed': []},
                  'source_routes': patch.integrate_groups[0].model_dump(mode='json')['source_routes'],
                  'completeness': {'member_content': True, 'before_relations': True, 'candidate_content': True, 'candidate_relations': True, 'required_dependencies': True}}
    return {'writer.concept.user.example.json': writer,
            'assessment.concept.user.example.json': assessment,
            'writer.concept.submit_patch.example.json': patch.model_dump(mode='json', exclude_unset=True)}


if __name__ == '__main__':
    print(json.dumps(validate_examples(), ensure_ascii=False, indent=2))
