"""Bounded v2 probes from real case graphs; never update production workspaces."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from html import escape
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.integration.task_state import canonical_hash
from cortex.capabilities.graph_merge.llm.task_agent import IncrementalIntegrationAgent
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState, load_root_schema
from reconciliation_probe_fixtures import (Fixture, case_data, preserve_conflict_fixture,
    content, digest, evidence_for, live_invariant_oracle, real_metric_fixture,
    select_abstract, semantic_oracle, write)
from run_node_consolidation_smoke import resolve_input_root, trace_summary

REPO = Path(__file__).resolve().parents[1]
MERGER_CASES = {
    'relation_merger_financial_116': ('financial', '116'),
    'relation_merger_formula_1_880': ('formula_1', '880'),
}


def natural_relation_fixture(args, name):
    """Use an untouched real graph and its original QA, never replicated edges."""
    from cortex.capabilities.graph_merge.integration.relation_merger import relation_groups
    database, case_id = MERGER_CASES[name]
    graph_path = args.graph_root / database / 'case_level_graphs' / case_id / 'state.json'
    before = GraphState.model_validate(json.loads(graph_path.read_text()))
    GraphManager(load_root_schema()).validate_state(before)
    groups = relation_groups(before)
    if not groups:
        raise ValueError(f'No natural relation group in {database}/{case_id}')
    source_path = args.source_input_root / database / 'cases' / case_id / 'input.json'
    if source_path.exists():
        source = json.loads(source_path.read_text())
        rows = {source['source_id']: {key: source[key] for key in ('question', 'sql', 'evidence') if key in source}}
    else:
        source_path = REPO / 'datasets/bird/dev/dev.json'
        matches = [row for row in json.loads(source_path.read_text())
                   if row['db_id'] == database and str(row['question_id']) == case_id]
        if len(matches) != 1:
            raise ValueError(f'Original BIRD QA must be unique for {database}/{case_id}')
        row = matches[0]
        rows = {'bird-' + database + '-' + case_id: {
            'question': row['question'], 'sql': row['SQL'], 'evidence': row.get('evidence', '')}}
    evidence = evidence_for(before, rows)
    if not evidence:
        raise ValueError(f'Original QA does not match graph source IDs for {database}/{case_id}')
    endpoints = sorted({ident for group in groups for ident in (group['from_node_id'], group['to_node_id'])})
    return Fixture(name, before, before, evidence, {}, [],
        {'source_sha256': {str(graph_path.resolve()): digest(graph_path), str(source_path.resolve()): digest(source_path)},
         'case_ids': [database + '/' + case_id], 'controlled_changes': [],
         'controlled_metric_replicas': 0, 'selection': 'Natural repeated relation groups in an unchanged real case-level graph.',
         'original_evidence': {'path': str(source_path.resolve()), 'database': database, 'case_id': case_id},
         'expected_outcome': 'One consolidated description per natural group, preserving distinct roles, fixed properties, nodes and exact source union.'},
        endpoints)


def fixtures(args):
    abstract = select_abstract(args)
    metric = real_metric_fixture(args.metric_record)
    conflict = preserve_conflict_fixture(args)
    conflict.signal = {'id': 'preserve-existing-conflict', 'kind': 'parallel_assertions',
        'mode': 'suspect', 'node_ids': [conflict.read_node_ids[0]],
        'edge_ids': [edge.id for edge in conflict.before.graph.edges
                     if edge.from_node_id == conflict.read_node_ids[0]],
        'observed': {'existing_branch_scopes': 'Retain every accepted complete branch and its valid relations'},
        'check_question': '确认现有分支关系正确即可keep；不消除conflict，不将alternatives普通化。'}
    conflict.provenance['expected_outcome'] = 'Preserve the existing valid conflict and every original branch scope.'
    value = value_fixture(args)
    return {'abstract_predicate': abstract, 'metric_direct_inputs': metric,
            'preserve_conflict': conflict, 'value_concepts': value}


def value_fixture(args):
    before, rows, hashes = case_data(args, 'toxicology', '222')
    reference, _, reference_hashes = case_data(args, 'toxicology', '204')
    hashes.update(reference_hashes)
    contract = next(item for item in reference.graph.relation_contracts if item.id == 'has_carcinogenicity_label')
    before = before.model_copy(update={'graph': before.graph.model_copy(update={
        'relation_contracts': (*before.graph.relation_contracts, contract)})})
    GraphManager(load_root_schema()).validate_state(before)
    original = next(item for item in reference.graph.nodes if item.id == 'concept.carcinogenicity_label')
    source = next(item for item in before.graph.nodes if item.id == 'concept.molecule').sources
    node = GraphNode(id=original.id, node_type='concept', **content(original).model_dump(), sources=source)
    edges = (
        GraphEdge(id='smoke.label_grounding', edge_type='grounded_on', from_node_id=node.id,
            to_node_id='column.molecule.label', properties={'description': 'The label column stores the molecule carcinogenicity assessment.'}, sources=source),
        GraphEdge(id='smoke.molecule_has_label', edge_type=contract.id, from_node_id='concept.molecule',
            to_node_id=node.id, properties={'description': 'The molecule has a stored carcinogenicity assessment represented by its label.'}, sources=source))
    expected = before.model_copy(update={'graph': before.graph.model_copy(update={
        'nodes': (*before.graph.nodes, node), 'edges': (*before.graph.edges, *edges)})})
    GraphManager(load_root_schema()).validate_state(expected)
    evidence = evidence_for(before, rows)
    ids = [item.id for item in evidence]
    changes = [{'change_id': 'create-dimension', 'kind': 'node_create',
        'node_id': node.id, 'node_type': node.node_type, 'content': content(node).model_dump(mode='json'),
        'evidence_ids': ids, 'reason': 'Supply the missing business dimension, preserving the existing molecule subsets.'}]
    for i, edge in enumerate(edges):
        changes.append({'change_id': 'connect-dimension-' + str(i), 'kind': 'edge_create',
            'edge_id': edge.id, 'edge_type': edge.edge_type, 'from_node_id': edge.from_node_id,
            'to_node_id': edge.to_node_id, 'properties': edge.properties, 'evidence_ids': ids,
            'reason': 'Connect the explicit dimension to its physical representation and business object.'})
    signal = {'id': 'missing-label-dimension', 'kind': 'value_concepts', 'mode': 'required',
        'required_outcome': '本受控测试明确要求业务层存在致癌性标签维度 Concept，targets 为 [[column.molecule.label]]，以 grounded_on 表示该列，并有据连接到分子业务概念。物理 Column/DimensionValue 目录不替代这个业务 Concept；原有分子子集及公式保持。',
        'node_ids': ['concept.molecule', 'concept.carcinogenic_molecule', 'concept.non_carcinogenic_molecule'],
        'column_id': 'column.molecule.label',
        'observed': {'values': ['dimension_value.molecule.label.%2B', 'dimension_value.molecule.label.-'],
            'missing_dimension': 'No Concept currently represents the stored carcinogenicity label column'},
        'check_question': '补齐明确缺失的致癌性标签维度Concept、列表示与分子属性关系；现有致癌/非致癌分子是对象子集，保留其条件和公式，不冒充取值类别。'}
    return Fixture('value_concepts', before, expected, evidence, signal, changes,
        {'source_sha256': hashes, 'case_ids': ['toxicology/222', 'toxicology/204'],
         'controlled_changes': ['Install the real has_carcinogenicity_label contract from case204; case222 already lacks the semantic dimension.'],
         'expected_outcome': 'Create the business label dimension and its legitimate physical/business connections, keeping both subsets.'},
        signal['node_ids'])


class ScriptedChat:
    """Offline transaction test, explicitly not a real semantic model review."""
    def __init__(self, fixture, context):
        self.fixture, self.context, self.counts = fixture, context, Counter()

    def bind(self, **kwargs):
        self.output_options=kwargs
        return self

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        return self

    def invoke(self, messages, config=None, **kwargs):
        from langchain_core.messages import AIMessage
        runtime = self.context.reconciliation_runtime
        if any(tool.name == 'edit_draft' for tool in self.tools):
            self.counts['writer'] += 1
            if self.fixture.changes and not runtime.draft.changes:
                name, args = 'edit_draft', {'put_changes': self.fixture.changes}
            else:
                name, args = 'edit_draft', {}
        else:
            self.counts['assessment'] += 1
            semantic_oracle(self.context.candidate, self.fixture)
            args = {'explanation': 'The compiled local graph is reasonable and original knowledge and sources are preserved.', 'feedback': []}
            return AIMessage(content=json.dumps(args, ensure_ascii=False))
        return AIMessage(content='', tool_calls=[{'name': name, 'id': 'offline-' + name, 'args': args}])


def model_for(args, folder, slot):
    from dotenv import load_dotenv
    from cortex.config import load_config
    from cortex.config.models import LLMConfig
    from cortex.infrastructure.llm.key_slots import read_key_slots
    from cortex.infrastructure.llm.providers import create_chat_model
    from run_resilient_training import observe
    load_dotenv(REPO / '.env')
    args.slot = slot
    resolved = load_config(args.config).llm.resolve('sql_training')
    settings = resolved.model_copy(update={'model': args.model or resolved.model,
        'api_key': read_key_slots(args.keys)[slot].api_key, 'timeout': args.timeout, 'max_retries': 0})
    model = create_chat_model(LLMConfig.model_validate(settings.model_dump()))
    model.streaming = model.stream_usage = False
    model.metadata = {**(getattr(model, 'metadata', None) or {}),
        'anonymous_key_slot': slot, 'credential_slot': slot}
    write(folder / 'model_configuration.json', {'anonymous_key_slot': slot,
        'model': model.model_name, 'read_timeout_seconds': args.timeout, 'sdk_retries': 0,
        'client_key_matches_selected_slot': model.openai_api_key.get_secret_value() == read_key_slots(args.keys)[slot].api_key.get_secret_value()})
    timeout_type = type(model.root_client._client.timeout)
    timeout = timeout_type(connect=30, read=args.timeout, write=60, pool=30)
    model.request_timeout = args.timeout
    for name in ('root_client', 'root_async_client'):
        client = getattr(model, name, None)
        if client is not None:
            client.timeout = client._client.timeout = timeout
    observe(model, folder)
    return model


def bind_recovery(args, agent, folder):
    from cortex.capabilities.graph_merge.llm.transport import _connection_failure
    from run_resilient_training import transport_error_details
    def recover(error):
        if not _connection_failure(error):
            return None
        if len(args.used_slots) >= min(3, len(args.slots)):
            return None
        nxt = next(slot for slot in args.slots if slot not in args.used_slots)
        args.used_slots.append(nxt)
        with (folder / 'transport_recovery.jsonl').open('a') as stream:
            stream.write(json.dumps({'next_anonymous_slot': nxt,
                'errors': transport_error_details(error, args.secrets)}, ensure_ascii=False) + '\n')
        return model_for(args, folder, nxt)
    agent.recover_transport = recover


def run_merger(args, budget, fixture):
    from cortex.capabilities.graph_merge.integration.relation_merger import consolidate_relations, relation_groups
    from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
    from run_resilient_training import transport_error_details
    folder = args.output / fixture.name
    folder.mkdir(parents=True)
    before = fixture.before
    GraphManager(load_root_schema()).validate_state(before)
    groups = relation_groups(before)
    context = IntegrationContext(before, GraphState(graph=GraphSpace()), load_root_schema(), evidence=fixture.evidence)
    class MergerChat:
        def __init__(self): self.calls = 0
        def bind_tools(self, tools, **kwargs):
            self.tools = tools
            return self
        def invoke(self, messages, config=None, **kwargs):
            from langchain_core.messages import AIMessage
            self.calls += 1
            assert {tool.name for tool in self.tools} == {'read_evidence', 'submit_relations'}
            packet = json.loads(messages[1].content)
            index = {edge['id']: edge for edge in packet['relations']}
            return AIMessage(content='', tool_calls=[{'name': 'submit_relations', 'id': 'offline-relations',
                'args': {'results': [{'group_id': group['group_id'], 'description': ' '.join(dict.fromkeys(
                    index[ident]['properties']['description'] for ident in group['member_edge_ids']))}
                    for group in packet['groups']]}}])
    model = model_for(args, folder, args.used_slots[-1]) if args.live else MergerChat()
    agent = IncrementalIntegrationAgent(model, max_calls=args.max_calls, request_max_characters=args.input_limit,
        trace_sink=FileSystemGraphMergeTraceSink(folder / 'trace.jsonl'))
    agent.call_budget = budget if args.live else None
    if args.live:
        bind_recovery(args, agent, folder)
    result = {'name': fixture.name, 'mode': 'live' if args.live else 'deterministic',
        'status': 'running', 'assessment': 'not_required', 'batch_size_limit': 10,
        'actual_batch_sizes': [min(10, len(groups)-offset) for offset in range(0, len(groups), 10)],
        'groups': len(groups),
        'provenance': fixture.provenance}
    write(folder / 'before.json', before.model_dump(mode='json'))
    write(folder / 'groups.json', groups)
    started = time.monotonic()
    try:
        merged = consolidate_relations(agent, context, before, save=lambda cp: write(folder / 'checkpoint.json', cp), batch_size=10)
        if merged.state.graph.nodes != before.graph.nodes:
            raise AssertionError('Relation merger changed business nodes')
        by_id = {edge.id: edge for edge in merged.state.graph.edges}
        before_edges = {edge.id: edge for edge in before.graph.edges}
        for group in groups:
            survivors = {merged.edge_aliases[ident] for ident in group['member_edge_ids']}
            if len(survivors) != 1:
                raise AssertionError('One relation group must have one survivor')
            survivor = by_id[next(iter(survivors))]
            expected_sources = {source for ident in group['member_edge_ids'] for source in before_edges[ident].sources}
            if set(survivor.sources) != expected_sources:
                raise AssertionError('Relation source union differs')
        calls_before = budget['used'] if args.live else model.calls
        resumed = consolidate_relations(agent, context, merged.state, checkpoint=merged.checkpoint)
        if canonical_hash(resumed.state) != canonical_hash(merged.state):
            raise AssertionError('Saved relation result changed on resume')
        if (budget['used'] if args.live else model.calls) != calls_before:
            raise AssertionError('Completed relation batch was regenerated')
        write(folder / 'after.json', merged.state.model_dump(mode='json'))
        result.update(status='passed', checkpoint_stage=merged.checkpoint['stage'],
            node_contents_preserved=True, source_union_preserved=True, completed_resume_model_calls=0)
    except Exception as error:
        result.update(status='unfinished', errors=transport_error_details(error, args.secrets if args.live else ()))
    result.update(elapsed_seconds=round(time.monotonic() - started, 3), trace=trace_summary(folder),
        source_files_unchanged=all(digest(path) == value for path, value in fixture.provenance['source_sha256'].items()))
    if not args.live:
        result['offline_model_calls'] = model.calls
    write(folder / 'result.json', result)
    return result


def run_reconciliation(args, fixture, budget):
    from cortex.capabilities.graph_merge.integration.incremental import IncrementalGraphIntegrationEngine
    from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
    from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
    from run_resilient_training import transport_error_details
    folder = args.output / fixture.name
    folder.mkdir(parents=True)
    context = IntegrationContext(fixture.before, GraphState(graph=GraphSpace()), load_root_schema(), evidence=fixture.evidence)
    context.candidate = context.task_before = fixture.before
    model = model_for(args, folder, args.used_slots[-1]) if args.live else ScriptedChat(fixture, context)
    if args.live and getattr(args,'seed_empty_first',False):
        base_model=model
        class SeedFirstSubmission:
            used=False
            def bind(self,**kwargs):
                self.bound=self.bound.bind(**kwargs)
                return self
            def bind_tools(self,tools,**kwargs):
                self.names={tool.name for tool in tools}
                self.bound=base_model.bind_tools(tools,**kwargs)
                return self
            def invoke(self,messages,config=None):
                if not self.used and 'edit_draft' in self.names:
                    from langchain_core.messages import AIMessage
                    self.used=True
                    response=AIMessage(content='',tool_calls=[{'name':'edit_draft','id':'controlled-empty-seed','args':{}}])
                    write(folder/'seed_submission.json',{'test_harness_action':'Submit an empty compiled local draft to the real Assessment.',
                        'purpose':'Verify the independent model rejects the still-missing required dimension and returns actionable feedback to the same Writer.',
                        'not_a_model_response':True,'controller_messages':[row.model_dump(mode='json') for row in messages],
                        'seed_response':response.model_dump(mode='json')})
                    return response
                return self.bound.invoke(messages,config=config)
        model=SeedFirstSubmission()
    if args.live and getattr(args,'pause_before_assessment',False):
        base_model=model
        class PauseBeforeAssessment:
            def bind_tools(self,tools,**kwargs):
                self.assessment=not any(tool.name=='edit_draft' for tool in tools)
                self.bound=base_model.bind_tools(tools,**kwargs)
                return self
            def bind(self,**kwargs):
                self.bound=self.bound.bind(**kwargs)
                return self
            def invoke(self,messages,config=None):
                if self.assessment:
                    write(folder/'controlled_stop.json',{'purpose':'Verify exact pending Assessment recovery without any Writer replay.',
                        'not_a_provider_failure':True,'no_assessment_request_sent':True,
                        'controller_messages':[row.model_dump(mode='json') for row in messages]})
                    raise RuntimeError('CONTROLLED_CHECKPOINT_STOP_BEFORE_ASSESSMENT')
                return self.bound.invoke(messages,config=config)
        model=PauseBeforeAssessment()
    agent = IncrementalIntegrationAgent(model, max_calls=args.max_calls, request_max_characters=args.input_limit,
        trace_sink=FileSystemGraphMergeTraceSink(folder / 'trace.jsonl'))
    agent.call_budget = budget if args.live else None
    if args.live:
        bind_recovery(args, agent, folder)
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=3)
    engine.ctx, engine.current, engine.lineage = context, fixture.before, LineageIndex(context)
    engine.history, engine.reviewed_sources, engine._revision = [], {}, 0
    engine.identity_decision, engine.open_tasks = {}, []
    engine.workspace_path, engine._storage_revision = folder / 'workspace.sqlite', None
    engine.pending_relation_work = engine.pending_review = None
    task = LocalTask('v2-smoke:' + fixture.name, 'reconcile', fixture.targets, checks=[fixture.signal])
    result = {'name': fixture.name, 'mode': 'live' if args.live else 'deterministic', 'status': 'running',
              'provenance': fixture.provenance}
    write(folder / 'before.json', fixture.before.model_dump(mode='json'))
    write(folder / 'signal.json', fixture.signal)
    write(folder / 'fixture_evidence.json', [row.model_dump(mode='json') for row in fixture.evidence])
    write(folder / 'fixture_recipe.json', {'provenance':fixture.provenance,'offline_expected_changes':fixture.changes,
        'note':'Expected changes are an offline oracle only; they are never injected into live Agent requests.'})
    resume_case=getattr(args,'resume_case',None)
    if resume_case is not None:
        import sqlite3
        from cortex.capabilities.graph_merge.integration.task_state import TaskState, build_contract_versions
        source=Path(resume_case)/'workspace.sqlite'
        with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as origin, sqlite3.connect(engine.workspace_path) as destination:
            origin.backup(destination)
        recovered=WorkspaceStore(engine.workspace_path).load(task.id)
        restored=TaskState.from_dict(recovered['task_state'])
        if restored.versions!=build_contract_versions(context.schema):
            raise ValueError('Probe checkpoint policy differs; do not silently migrate it')
        accepted=GraphState.model_validate(recovered['current'])
        if canonical_hash(accepted)!=canonical_hash(fixture.before):
            raise ValueError('This bounded probe resumes only its unchanged accepted baseline')
        engine.task_state=restored
        engine.contract_versions=restored.versions
        engine._storage_revision=recovered['revision']
        engine._revision=recovered['accepted_revision']
        engine.history=list(recovered['history'])
        result['resume_from']={'case_directory':str(Path(resume_case).resolve()),
            'stage':restored.stage,'draft':recovered['task_state'].get('draft'),
            'candidate_hash':restored.candidate_hash,'original_checkpoint_preserved':True}
    started = time.monotonic()
    try:
        engine._run_task(task, task.id)
        if args.live and fixture.name == 'value_concepts':
            GraphManager(context.schema).validate_state(engine.current)
            before_ids = {node.id for node in fixture.before.graph.nodes}
            new = [node for node in engine.current.graph.nodes if node.id not in before_ids]
            dimension = [node for node in new if node.node_type == 'concept'
                and ['column.molecule.label'] in node.properties.get('targets', [])]
            if len(dimension) != 1:
                raise AssertionError('The missing semantic label dimension was not created exactly once')
            if not any(edge.from_node_id == dimension[0].id and edge.to_node_id == 'column.molecule.label'
                       and edge.edge_type == 'grounded_on' for edge in engine.current.graph.edges):
                raise AssertionError('The new dimension has no physical representation')
            oracle = {'strict_graph_validation': True, 'dimension_created': True}
        else:
            oracle = live_invariant_oracle(engine.current, fixture) if args.live else semantic_oracle(engine.current, fixture)
        if not engine.history or engine.history[-1]['assessment'] != 'graph_assess':
            raise AssertionError('Missing actual independent Assessment commit')
        saved = WorkspaceStore(engine.workspace_path).load(task.id)
        write(folder / 'task_state.json', saved['task_state'])
        result.update(status='passed', oracle=oracle, independent_assessment=True,
            assessment_output_protocol='structured_json',
            checkpoint_stage='committed', history=engine.history)
    except Exception as error:
        result.update(status='unfinished', errors=transport_error_details(error, args.secrets if args.live else ()))
        if (folder/'controlled_stop.json').exists():
            result.update(status='controlled_stop',not_a_provider_failure=True)
        if getattr(engine, 'task_state', None):
            result['checkpoint_stage'] = engine.task_state.stage
            write(folder / 'task_state.json', engine.task_state.to_dict())
        elif engine.history:
            # A post-commit probe oracle can reject something Assessment missed.
            # Preserve that accepted test-workspace state; never label it passed.
            saved = WorkspaceStore(engine.workspace_path).load(task.id)
            write(folder / 'task_state.json', saved['task_state'])
            result.update(checkpoint_stage='committed_but_probe_failed',
                runtime_committed=True, history=engine.history)
    result.update(elapsed_seconds=round(time.monotonic() - started, 3),
        source_files_unchanged=all(digest(path) == value for path, value in fixture.provenance['source_sha256'].items()),
        trace=trace_summary(folder))
    if not args.live:
        result['offline_model_calls'] = dict(model.counts)
    write(folder / 'after.json', engine.current.model_dump(mode='json'))
    write(folder / 'result.json', result)
    return result


def render(args, results, budget):
    summary = {'created_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
        'mode': 'live' if args.live else 'deterministic', 'results': results, 'budget': budget,
        'timeout_seconds': args.timeout, 'background_runtime_switched': False}
    write(args.output / 'summary.json', summary)
    for row in results:
        render_probe_details(args.output, row)
    rows = ''.join('<tr><td>' + escape(row['name']) + '</td><td>' + escape(row['status'])
        + '</td><td><a href="' + row['name'] + '/result.json">结果/错误</a> · <a href="'
        + row['name'] + '/details.html">图差异/description/Assessment</a></td></tr>' for row in results)
    (args.output / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Relation / Reconciliation v2</title>'
        '<style>body{font:16px/1.8 system-ui;max-width:1100px;margin:30px auto}td,th{border:1px solid #ccc;padding:10px}table{border-collapse:collapse}pre{white-space:pre-wrap}</style>'
        '<h1>Relation Merger / Reconciliation 小范围测试</h1><p>原graphs及旧checkpoint只读保留；未切换后台。离线代理不代表真实模型通过。</p>'
        '<p>Relation merger 使用 financial/116 与 formula_1/880 的自然关系组，各自独立测试，不复制节点或关系凑满十组。Assessment 直接返回结构化 JSON，不提供 Review 工具。</p>'
        '<table><tr><th>局部样本</th><th>状态</th><th>记录</th></tr>' + rows + '</table><pre>'
        + escape(json.dumps({key: val for key, val in summary.items() if key != 'results'}, ensure_ascii=False, indent=2)) + '</pre>')


def render_probe_details(root, result):
    """Explain saved snapshots and real responses without rewriting raw records."""
    folder = root / result['name']
    if not (folder/'before.json').exists():
        return
    before = json.loads((folder/'before.json').read_text())['graph']
    after = json.loads((folder/'after.json').read_text())['graph'] if (folder/'after.json').exists() else None
    delta = {}
    for kind in ('nodes', 'edges'):
        old = {item['id']: item for item in before[kind]}
        if after is None:
            delta[kind] = {'before_count': len(old), 'after_count': None,
                           'note': 'No output snapshot was produced; no before/after claim is available.'}
            continue
        new = {item['id']: item for item in after[kind]}
        delta[kind] = {'before_count': len(old), 'after_count': len(new),
            'created': [new[key] for key in sorted(new.keys()-old.keys())],
            'deleted': [old[key] for key in sorted(old.keys()-new.keys())],
            'updated': [{'id': key, 'before': old[key], 'after': new[key]} for key in sorted(old.keys()&new.keys())
                        if old[key] != new[key]]}
    details = {'name': result['name'], 'mode': result.get('mode'), 'status': result['status'],
        'provenance': {key: value for key, value in result.get('provenance', {}).items() if key != 'archived_original'},
        'delta': delta, 'whole_graph_equal': before == after if after is not None else None,
        'errors': result.get('errors', []),
        'snapshot_note': 'Reconciliation after.json is the accepted engine state; unpublished candidates and diagnostics remain in task_state.json. Merger after.json is written only after successful application.'}
    if (folder/'groups.json').exists() and after is not None:
        old, new = ({item['id']: item for item in graph['edges']} for graph in (before, after))
        details['relation_groups'] = [{**group,
            'before_members': [old[key] for key in group['member_edge_ids']],
            'after_survivors': [new[key] for key in group['member_edge_ids'] if key in new]}
            for group in json.loads((folder/'groups.json').read_text())]
    if (folder/'task_state.json').exists():
        task = json.loads((folder/'task_state.json').read_text())
        details['draft'], details['assessment'] = task.get('draft'), task.get('review')
    if (folder/'model_calls.jsonl').exists():
        events = [json.loads(line) for line in (folder/'model_calls.jsonl').read_text().splitlines()]
        details['responses'] = [{'component': row.get('component'), 'content': row['message'].get('content'),
            'tool_calls': row['message'].get('tool_calls', [])} for row in events if row.get('event_type') == 'model_end']
        details['feedback_events'] = [row for row in events if row.get('event_type') in {
            'tool_result', 'assessment_result', 'draft_compile_result', 'compile_result', 'draft_compiled'}]
    write(folder/'details.json', details)
    call_link='<a href="model_calls.jsonl">真实调用记录</a>' if (folder/'model_calls.jsonl').exists() else '未发送模型请求（无调用日志）'
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>'+escape(result['name'])+'</title><style>body{font:16px/1.8 system-ui;max-width:1100px;margin:30px auto;padding:0 20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f9;padding:16px}summary{cursor:pointer;font-weight:bold}</style>',
        '<h1>'+escape(result['name'])+'</h1><p><a href="../index.html">返回本轮报告</a> · <a href="details.json">结构化明细</a> · <a href="before.json">before</a> · <a href="after.json">after</a> · '+call_link+'</p>',
        '<p>此页从保存的输入/输出重建；离线代理只验证流程，不能作为真实语义模型通过的证据。</p>']
    for key, value in details.items():
        parts.append('<details'+(' open' if key in {'delta', 'relation_groups', 'assessment', 'provenance'} else '')
            +'><summary>'+escape(key)+'</summary><pre>'+escape(json.dumps(value, ensure_ascii=False, indent=2))+'</pre></details>')
    (folder/'details.html').write_text(''.join(parts)+'</html>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--resume-case',type=Path,help='Read an isolated probe checkpoint and copy it to this fresh output; never edit the original.')
    parser.add_argument('--pause-before-assessment',action='store_true',help='Controlled stop after real Writer compilation; resume a fresh copy separately.')
    parser.add_argument('--seed-empty-first',action='store_true',help='One disclosed test-harness no-op submission before the real independent Assessment; value_concepts only.')
    parser.add_argument('--cases', nargs='+', default=['relation_merger', 'abstract_predicate', 'value_concepts', 'preserve_conflict'])
    parser.add_argument('--graph-root', type=Path, default=REPO / 'output/latest_graphs')
    parser.add_argument('--source-input-root', type=Path)
    parser.add_argument('--metric-record', type=Path, default=REPO / 'tests/fixtures/node_local_metric_retirement.json')
    parser.add_argument('--config', type=Path, default=REPO / 'configs/cortex.yaml')
    parser.add_argument('--keys', type=Path, default=REPO / 'configs/sql_training.keys')
    parser.add_argument('--slots', type=int, nargs='+')
    parser.add_argument('--model')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--max-calls', type=int, default=12)
    parser.add_argument('--total-call-budget', type=int, default=48)
    parser.add_argument('--input-limit', type=int, default=100000)
    args = parser.parse_args()
    if args.pause_before_assessment and (not args.live or len(args.cases)!=1 or args.seed_empty_first or args.resume_case or 'relation_merger' in args.cases):
        parser.error('Controlled checkpoint stop requires one live reconciliation case, without seed or resume')
    if args.seed_empty_first and (not args.live or args.cases!=['value_concepts']):
        parser.error('Controlled empty seed requires --live --cases value_concepts')
    if args.resume_case is not None and (not args.live or len(args.cases)!=1 or 'relation_merger' in args.cases):
        parser.error('Checkpoint continuation requires one live reconciliation case')
    if args.resume_case is not None and (args.seed_empty_first or not (args.resume_case/'workspace.sqlite').is_file()):
        parser.error('Resume requires an existing readonly source checkpoint and cannot seed another empty submission')
    if args.output.exists():
        parser.error('Choose a fresh output directory; previous records are immutable')
    if not 1 <= args.timeout <= 600 or not 1 <= args.total_call_budget <= 64 or not 1 <= args.max_calls <= 16 or not 1 <= args.input_limit <= 200000:
        parser.error('Bounded probe limits: timeout<=600, total calls<=64, per role<=16')
    args.source_input_root = resolve_input_root(args.source_input_root)
    args.discovery_limit = 32
    args.secrets, args.used_slots = (), []
    if args.live:
        from dotenv import load_dotenv
        from cortex.config import load_config
        from cortex.infrastructure.llm.key_slots import read_key_slots
        load_dotenv(REPO / '.env')
        slots = read_key_slots(args.keys)
        args.slots = args.slots or list(range(len(slots) - 1, max(-1, len(slots) - 4), -1))
        if any(slot < 0 or slot >= len(slots) for slot in args.slots):
            parser.error('Anonymous key slot is out of range')
        args.used_slots = [args.slots[0]]
        args.secrets = tuple(slot.api_key.get_secret_value() for slot in slots)
        args.model = args.model or load_config(args.config).llm.resolve('sql_training').model
        args.streaming, args.sdk_retries, args.merge_transport = False, 0, False
    args.output.mkdir(parents=True)
    selected = fixtures(args)
    names = []
    for name in args.cases:
        names.extend(MERGER_CASES if name == 'relation_merger' else [name])
    if len(names) != len(set(names)):
        parser.error('Choose every isolated probe at most once')
    if set(names) - (set(selected) | set(MERGER_CASES)):
        parser.error('Unknown probe: ' + ', '.join(sorted(set(names) - (set(selected) | set(MERGER_CASES)))))
    selected.update({name: natural_relation_fixture(args, name) for name in names if name in MERGER_CASES})
    budget = {'used': 0, 'limit': args.total_call_budget}
    results = []
    for name in names:
        results.append({'name': name, 'status': 'running'})
        render(args, results, budget)
        result = run_merger(args, budget, selected[name]) if name in MERGER_CASES else run_reconciliation(args, selected[name], budget)
        results[-1] = result
        render(args, results, budget)
        print(json.dumps({'name': name, 'status': result['status'], 'calls': budget['used']}, ensure_ascii=False), flush=True)
    print(str(args.output / 'index.html'))


if __name__ == '__main__':
    main()
