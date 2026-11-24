"""One bounded live Concept -> Metric -> relation merger -> reconciliation probe.

Identity groups are explicitly supplied by the test, not model-inferred. Source
case graphs are read-only. Every stage uses the production compiler and gates.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.graph_merge.graph.publication import Preview
from cortex.capabilities.graph_merge.graph.operations import deduplicate_edges
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.graph.operations import remap_node
from cortex.capabilities.graph_merge.graph.contracts import isolate_contracts
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.reconciliation.relation_merger import consolidate_relations, relation_groups
from cortex.capabilities.graph_merge.reconciliation.inspection import inspection_signals
from cortex.capabilities.graph_merge.models import LocalTask
from cortex.capabilities.graph_merge.consolidation.presentation import build_node_packet
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
from cortex.schema import GraphSpace, GraphState, load_root_schema, validate_graph_state
from cortex.capabilities.graph_merge.evidence import EvidenceItem
from reconciliation_probe_fixtures import digest, write
from run_node_consolidation_smoke import project_case, trace_summary
from run_relation_reconciliation_v2_smoke import model_for

REPO = Path(__file__).resolve().parents[1]
C = 'concept.carcinogenic_molecule'
M = 'metric.carcinogenic_molecule_count'


class EnglishGuard:
    """Reject non-English instructions before any probe request reaches the API."""
    def __init__(self, model, audit, tools=()):
        self.model, self.audit, self.tools = model, audit, tools

    def __getattr__(self, name):
        return getattr(self.model, name)

    def bind(self, **kwargs):
        return EnglishGuard(self.model.bind(**kwargs), self.audit, self.tools)

    def bind_tools(self, tools, **kwargs):
        return EnglishGuard(self.model.bind_tools(tools, **kwargs), self.audit, tools)

    def invoke(self, messages, config=None, **kwargs):
        from langchain_core.utils.function_calling import convert_to_openai_tool
        bodies = [message.content for message in messages]
        bodies.extend(json.dumps(convert_to_openai_tool(tool), ensure_ascii=False) for tool in self.tools)
        violations = [re.findall(r'.{0,45}[\u4e00-\u9fff].{0,45}', body)[:5]
                      for body in bodies if isinstance(body, str) and re.search('[\u4e00-\u9fff]', body)]
        if violations:
            write(self.audit.with_name('english_guard_failure.json'), violations)
            raise ValueError('English prompt guard rejected non-English model-visible text; no request sent')
        with self.audit.open('a') as stream:
            stream.write(json.dumps({'message_count': len(messages), 'tool_count': len(self.tools),
                                     'cjk_characters': 0, 'checked_before_api': True})+'\n')
        return self.model.invoke(messages, config=config, **kwargs)


def prepare(root):
    schema = load_root_schema()
    nodes, edges, contracts, hashes = {}, {}, (), {}
    for case in ('266', '204'):
        path = REPO / f'output/latest_graphs/toxicology/case_level_graphs/{case}/state.json'
        hashes[str(path)] = digest(path)
        original = GraphState.model_validate(json.loads(path.read_text()))
        GraphManager(schema).validate_state(original)
        for selected in (C, M):
            fragment, _ = project_case(original, selected, case)
            contracts, predicates = isolate_contracts(contracts, fragment.graph.relation_contracts,
                                                       schema.edge_type_index(), prefix=f'case{case}__')
            for node in fragment.graph.nodes:
                nodes.setdefault(node.id, node)
            for edge in fragment.graph.edges:
                edges[edge.id] = edge.model_copy(update={'edge_type': predicates.get(edge.edge_type, edge.edge_type)})
    combined, aliases = deduplicate_edges(edges.values())
    before = GraphState(graph=GraphSpace(nodes=tuple(remap_node(node, {}, aliases) for node in nodes.values()),
                                        edges=tuple(combined.values()), relation_contracts=contracts))
    validate_graph_state(before, schema)
    try:
        GraphManager(schema).validate_state(before)
    except GraphRejected as error:
        if any(issue.code != 'DUPLICATE_CONCEPT_GROUNDING' for issue in error.issues):
            raise
        # This is the intended, not-yet-consolidated identity group, not an
        # unrelated global collision hidden by the fixture constructor.
        by_edge = {edge.id: edge for edge in before.graph.edges}
        members = {f'case{case}::{C}' for case in ('266', '204')}
        assert all(by_edge[issue.path.removeprefix('edges.')].from_node_id in members
                   and issue.details['existing_concept_id'] in members for issue in error.issues)
    ep = REPO / 'output/node_consolidation_smoke_20261120_live/carcinogenic_molecule_count/evidence.json'
    evidence = tuple(EvidenceItem.model_validate(row) for row in json.loads(ep.read_text()))
    hashes[str(ep)] = digest(ep)
    write(root/'before.json', before.model_dump(mode='json'))
    write(root/'evidence.json', [row.model_dump(mode='json') for row in evidence])
    write(root/'provenance.json', {'case_ids': ['toxicology/266', 'toxicology/204'],
        'source_sha256': hashes, 'construction': 'Union of the real Concept and Metric relation/ref closures, with case namespaces and shared read-only physical catalog. No relation copies or injected defects.',
        'identity': 'Two same-identity groups explicitly approved by the user; no live Identity call.',
        'groups': [[f'case{case}::{kind}' for case in ('266', '204')] for kind in (C, M)]})
    return before, evidence, hashes


def make_engine(args, folder, state, evidence, budget):
    folder.mkdir(parents=True, exist_ok=True)
    model = EnglishGuard(model_for(args, folder, args.slot), folder/'english_requests.jsonl')
    agent = IncrementalIntegrationAgent(model, max_calls=args.max_calls,
        request_max_characters=args.input_limit, trace_sink=FileSystemGraphMergeTraceSink(folder/'trace.jsonl'))
    agent.call_budget = budget
    context = IntegrationContext(state, GraphState(graph=GraphSpace()), load_root_schema(), evidence=evidence)
    context.task_before = context.candidate = state
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=3, model_call_budget=budget['limit'])
    engine.session.inputs, engine.session.current, engine.session.lineage = context, state, LineageIndex(context)
    engine.session.history, engine.session.reviewed_sources, engine.session.revision = [], {}, 0
    engine.session.identity_decision, engine.session.open_tasks = {}, []
    engine.workspace_path, engine.session.storage_revision = folder/'workspace.sqlite', None
    engine.session.pending_relation_work = engine.session.pending_review = None
    context.task_visible_ids = {node.id for node in state.graph.nodes} | {edge.id for edge in state.graph.edges}
    write(folder/'before.json', state.model_dump(mode='json'))
    write(folder/'fixture_evidence.json', [row.model_dump(mode='json') for row in evidence])
    return engine


def record_stage(folder, engine, name):
    write(folder/'after.json', engine.session.current.model_dump(mode='json'))
    write(folder/'result.json', {'name': name, 'status': 'passed', 'history': engine.session.history,
        'trace': trace_summary(folder), 'source_files_unchanged': True})
    if getattr(engine.session, 'task_state', None):
        write(folder/'task_state.json', engine.session.task_state.to_dict())
    elif (folder/'workspace.sqlite').exists():
        from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
        saved=WorkspaceStore(folder/'workspace.sqlite').load(name)
        if saved and saved.get('task_state'):write(folder/'task_state.json',saved['task_state'])


def source_key(source):
    return source.model_dump_json()


def verify(initial, final, *, node_citations=None):
    node_citations = node_citations or {}
    node_checks = []
    nodes = {node.id: node for node in final.graph.nodes}
    mapping = {f'case204::{kind}': f'case266::{kind}' for kind in (C, M)}
    for kind in (C, M):
        owner = nodes[f'case266::{kind}']
        assert f'case204::{kind}' not in nodes
        expected = {s for node in initial.graph.nodes if node.id in {owner.id, f'case204::{kind}'} for s in node.sources}
        cited = set(node_citations.get(owner.id, ()))
        assert set(owner.sources) == expected | cited, 'Node provenance differs from inherited plus explicit citations'
        node_checks.append({'owner_id': owner.id, 'all_original_sources_preserved': True,
            'original_sources': [source.model_dump(mode='json') for source in sorted(expected,key=str)],
            'additional_explicit_citations': [source.model_dump(mode='json') for source in sorted(cited-expected,key=str)],
            'actual_sources': [source.model_dump(mode='json') for source in owner.sources]})
    concept = nodes['case266::'+C]
    groups = {tuple(group) for node in initial.graph.nodes if node.id in {'case266::'+C, 'case204::'+C}
              for group in node.properties['targets']}
    assert groups <= {tuple(group) for group in concept.properties['targets']}
    metric = nodes['case266::'+M]
    assert set(metric.properties['expression']['refs'].values()) == {'case266::'+C}
    deps = [edge for edge in final.graph.edges if edge.from_node_id == metric.id and edge.edge_type=='depends_on']
    assert len(deps)==1 and deps[0].to_node_id==concept.id
    expected_edges = defaultdict(set)
    old_descriptions = defaultdict(list)
    def key(edge, remap=False):
        props = {k:v for k,v in edge.properties.items() if k!='description'}
        return json.dumps([mapping.get(edge.from_node_id,edge.from_node_id) if remap else edge.from_node_id,
            mapping.get(edge.to_node_id,edge.to_node_id) if remap else edge.to_node_id,edge.edge_type,props],sort_keys=True)
    for edge in initial.graph.edges:
        expected_edges[key(edge,True)].update(edge.sources)
        old_descriptions[key(edge,True)].append({'id':edge.id,'description':edge.properties.get('description')})
    actual = defaultdict(list)
    for edge in final.graph.edges:
        actual[key(edge)].append(edge)
    assert set(expected_edges)==set(actual), 'Unexpected missing/new structural relation'
    checks=[]
    for structural, sources in expected_edges.items():
        final_sources = {s for edge in actual[structural] for s in edge.sources}
        assert final_sources == sources, 'Exact edge SourceReference union differs'
        checks.append({'before':old_descriptions[structural], 'after':[edge.model_dump(mode='json') for edge in actual[structural]],
                       'exact_source_union':True})
    GraphManager(load_root_schema()).validate_state(final)
    return {'strict_graph_valid':True, 'concept_and_metric_each_one_owner':True,
        'matching_groups_preserved':True, 'metric_ref_and_single_dependency_canonical':True,
        'node_exact_inherited_plus_cited_source_unions':True, 'node_sources':node_checks,
        'all_edge_exact_source_unions':True,
        'relations':checks, 'semantic_description_judgment':'Independently assessed by the real reconciliation Assessment; equality of prose is not required.'}


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output/'summary.json').exists():
        raise ValueError('Use a fresh output directory; previous live records are immutable')
    before, evidence, hashes = prepare(args.output)
    state = before
    budget = {'limit': args.budget, 'used': 0}
    node_citations = {}
    summary = {'status':'running','stages':[],'model':'deepseek-v4-flash','call_budget':budget}
    try:
        for number, kind in enumerate((C, M),1):
            name=f'0{number}_'+('concept' if number==1 else 'metric')
            folder=args.output/name
            engine=make_engine(args,folder,state,evidence,budget)
            members=[f'case{case}::{kind}' for case in ('266','204')]
            group={'owner_id':members[0],'member_ids':members}
            write(folder/'writer.packet.initial.json',build_node_packet(engine.session.inputs, {'group':group,'task_id':name},before=state))
            task=LocalTask(name,'consolidate',set(members),groups=[{'members':members,
                'meaning':'The same carcinogenic molecule identity.' if number==1 else 'The same count of carcinogenic molecules in a requested population.',
                'reason':'This approved identity group is consolidated in a bounded real-case pipeline; preserve compatible matching methods and requested-range parameterization.'}])
            engine._run_task(task,name,aliases={member:members[0] for member in members})
            assert engine.session.history[-1]['assessment']=='node_assess'
            from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
            from cortex.capabilities.graph_merge.evidence import evidence_sources
            committed=WorkspaceStore(engine.workspace_path).load(name)['task_state']
            citations=committed['draft']['integrate_groups'][0].get('common_evidence_ids',[])
            node_citations[members[0]]=evidence_sources(engine.session.inputs,citations)
            write(folder/'task_state.json',committed)
            state=engine.session.current
            record_stage(folder,engine,name)
            summary['stages'].append({'name':name,'status':'passed'})
            write(args.output/'progress.json',summary)
            print(name,'passed',flush=True)
        folder=args.output/'03_relation_merger'
        engine=make_engine(args,folder,state,evidence,budget)
        groups=relation_groups(state)
        assert len(groups)==2, 'Expected two natural groups: grounding and dependency'
        write(folder/'groups.json',groups)
        merged=consolidate_relations(engine.agent,engine.session.inputs,state,batch_size=10,
                                    save=lambda cp:write(folder/'checkpoint.json',cp))
        used=budget['used']
        resumed=consolidate_relations(engine.agent,engine.session.inputs,merged.state,checkpoint=merged.checkpoint)
        assert resumed.state==merged.state and budget['used']==used
        engine.session.current=state=merged.state
        record_stage(folder,engine,'03_relation_merger')
        summary['stages'].append({'name':'03_relation_merger','status':'passed','natural_groups':len(groups),'completed_resume_api_calls':0})
        print('03_relation_merger passed',flush=True)
        folder=args.output/'04_reconciliation'
        engine=make_engine(args,folder,state,evidence,budget)
        preview=Preview(state,{}, {}, [])
        checks=inspection_signals(engine.session.inputs,preview)
        # A real stage handoff, not an injected defect: expose the original
        # relation assertions so the final independent gate can check semantic
        # preservation across description consolidation as well as local health.
        from cortex.capabilities.graph_merge.evidence import EvidencePresentation
        source_context=IntegrationContext(before,GraphState(graph=GraphSpace()),load_root_schema(),evidence=evidence)
        presentation=EvidencePresentation(source_context)
        original_relations=[{**edge.model_dump(mode='json',exclude={'sources','reference_origin'}),
                             'evidence_ids':presentation.ids_for(edge.sources)}
                            for edge in before.graph.edges if edge.from_node_id in {
                                f'case{case}::{kind}' for case in ('266','204') for kind in (C,M)}]
        checks.append({'id':'consolidation-preservation-handoff','kind':'node_relation_followup','mode':'required',
            'node_ids':['case266::'+C,'case266::'+M],
            'required_outcome':'Preserve the original matching methods, formula operand meaning and sourced relation roles. Compare original relation assertions with actual surviving relations using the approved node aliases. Keep unconfirmed context endpoints distinct. A valid unchanged result needs no repair.',
            'observed':{'original_relation_assertions':original_relations,
                        'approved_node_aliases':{f'case204::{kind}':f'case266::{kind}' for kind in (C,M)}}})
        write(folder/'signals.json',checks)
        targets={ident for check in checks for key in ('node_ids','edge_ids') for ident in check.get(key,[])}
        # Include the consolidated nodes and their real adjacency so Assessment
        # can verify description/role/source continuity after the two stages.
        targets.update(['case266::'+C,'case266::'+M])
        task=LocalTask('04_reconciliation','reconcile',targets,checks=checks)
        engine._run_task(task,task.id)
        assert engine.session.history[-1]['assessment']=='graph_assess'
        state=engine.session.current
        record_stage(folder,engine,'04_reconciliation')
        summary['stages'].append({'name':'04_reconciliation','status':'passed','signals':[row['kind'] for row in checks]})
        write(args.output/'verification.json',verify(before,state,node_citations=node_citations))
        write(args.output/'final.json',state.model_dump(mode='json'))
        summary['status']='passed'
        print('04_reconciliation passed',flush=True)
    except Exception as error:
        from run_resilient_training import transport_error_details
        summary['status']='unfinished'
        secret=getattr(getattr(getattr(engine,'agent',None),'llm',None),'openai_api_key',None)
        # Public failures contain no request headers; redact the configured key
        # as well, if a provider embeds it in its exception message.
        summary['errors']=transport_error_details(error,(secret.get_secret_value(),) if hasattr(secret,'get_secret_value') else ())
        if 'folder' in locals() and 'engine' in locals():
            write(folder/'after.json',engine.session.current.model_dump(mode='json'))
            if getattr(engine.session,'task_state',None):write(folder/'task_state.json',engine.session.task_state.to_dict())
            write(folder/'result.json',{'name':folder.name,'status':'unfinished','errors':summary['errors']})
        print('Pipeline unfinished:',type(error).__name__,str(error)[:700],flush=True)
    summary['original_sources_unchanged']=all(digest(p)==h for p,h in hashes.items())
    summary['counts']={'before_nodes':len(before.graph.nodes),'before_edges':len(before.graph.edges),
                       'after_nodes':len(state.graph.nodes),'after_edges':len(state.graph.edges)}
    write(args.output/'summary.json',summary)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=REPO/'output/reconciliation_readable_prompts_20261121/compat_no_thinking.yaml')
    parser.add_argument('--keys',type=Path,default=REPO/'configs/sql_training.keys')
    parser.add_argument('--model',default='deepseek-v4-flash')
    parser.add_argument('--slot',type=int,default=9)
    parser.add_argument('--timeout',type=int,default=180)
    parser.add_argument('--max-calls',type=int,default=8)
    parser.add_argument('--input-limit',type=int,default=200000)
    parser.add_argument('--budget',type=int,default=28)
    arguments=parser.parse_args()
    if not 1<=arguments.budget<=40:parser.error('Budget must be between 1 and 40')
    run(arguments)
