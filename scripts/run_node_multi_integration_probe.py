"""Two bounded real-model node tests; source checkpoints are read-only.

Replay the real five-member toxicology grouping failure and a two-Metric
all-singleton control. No source node is cloned and no accepted task is resumed.
"""
import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
import time

from resume_first10_current_graph import model_for, read, write
from run_resilient_training import transport_error_details
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.incremental import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.node_context import build_node_packet
from cortex.capabilities.graph_merge.llm.task_agent import IncrementalIntegrationAgent
from cortex.capabilities.sql_training.graph_merge_evidence import MappingEvidenceRepository, SqlTrainingGraphMergeEvidenceResolver
from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.schema import GraphSpace, GraphState, load_root_schema

REPO=Path(__file__).resolve().parents[1]
SOURCE=REPO/'output/toxicology_remaining_merge_pro_signal_subgraphs_20261122'
CHECKPOINT=SOURCE/'recoveries/node_partition_20261122_201849/workspace.original.sqlite'


def run(root,case,slot):
    folder=root/'live'/case
    if folder.exists():raise ValueError('Use a new folder; old live records are immutable')
    folder.mkdir(parents=True)
    source_hash=sha256(CHECKPOINT.read_bytes()).hexdigest()
    with sqlite3.connect(f'file:{CHECKPOINT}?mode=ro',uri=True) as con:
        saved=json.loads(con.execute('select payload from merge_workspaces').fetchone()[0])
    base=GraphState.model_validate(saved['task_state']['base'])
    if case=='real_five_member_partition':
        group=deepcopy(saved['content_plan'][4][0])
    else:
        group={'members':['input::n207','input::n208'],
               'meaning':'Proposed identity of distinct element atom counts',
               'reason':'Controlled grouping hypothesis using two different real source Metrics; inspect whether their counted populations have the same identity.'}
    members=set(group['members']);nodes={n.id:n for n in base.graph.nodes}
    sources=tuple(dict.fromkeys(s for obj in (*base.graph.nodes,*base.graph.edges) for s in obj.sources))
    resolver=SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(read(SOURCE/'source_evidence.json')))
    ctx=IntegrationContext(base,GraphState(graph=GraphSpace()),load_root_schema(),evidence=resolver.resolve(sources))
    ctx.sql_dialect='sqlite'
    call_slots=[slot]
    keys=read_key_slots(REPO/'configs/sql_training.keys')
    def model(selected):
        chat=model_for(folder,selected)
        chat.reasoning_effort='none'
        chat.extra_body={**(chat.extra_body or {}),'thinking':{'type':'disabled'}}
        cfg=read(folder/'model_configuration.json')
        cfg.update(thinking='disabled',reasoning_effort='none')
        write(folder/'model_configuration.json',cfg)
        return chat
    agent=IncrementalIntegrationAgent(model(slot),max_calls=8,request_max_characters=500000,
        trace_sink=FileSystemGraphMergeTraceSink(folder/'agents.jsonl'))
    agent.call_budget={'limit':16,'used':0}
    def recover(error):
        from continue_reviewed_merge import credential_failure,transport_failure
        if not (credential_failure(error) or transport_failure(error)) or len(call_slots)>=3:return None
        selected=(call_slots[-1]+2)%len(keys);call_slots.append(selected)
        return model(selected)
    agent.recover_transport=recover
    eng=IncrementalGraphIntegrationEngine(agent,max_rounds=4,model_call_budget=16)
    eng.ctx,eng.current,eng.lineage=ctx,base,LineageIndex(ctx)
    eng.history=[];eng.reviewed_sources={};eng._revision=0;eng._storage_revision=None
    eng.identity_decision={};eng.open_tasks=[];eng.pending_relation_work=eng.pending_review=None
    eng.relation_followups=[];eng.workspace_path=folder/'workspace.sqlite'
    owner=min(members);aliases={key:owner for key in members}
    task=LocalTask(case,'consolidate',{owner},groups=[group])
    ctx.task_before=base;ctx.task_identity_groups=[{'owner_id':owner,'member_ids':sorted(members)}]
    packet=build_node_packet(ctx,{'phase':'consolidate','task_id':case},before=base)
    write(folder/'initial_local_input.json',packet)
    write(folder/'before.json',base.model_dump(mode='json'))
    write(folder/'source_scope.json',{'source_checkpoint':str(CHECKPOINT),'source_sha256':source_hash,
        'fixture_kind':'historical unchanged five-member group' if case=='real_five_member_partition' else 'controlled wrong group of two distinct real Metrics',
        'members':sorted(members),'case_ids':sorted({s.case_id for k in members for s in nodes[k].sources if s.case_id}),
        'original_group':group,'no_cloned_nodes':True,'source_checkpoint_read_only':True})
    result={'case':case,'status':'running','started_at':datetime.now().isoformat()};write(folder/'result.json',result)
    start=time.monotonic()
    try:
        eng._run_task(task,'probe:'+case,aliases=aliases)
        final=WorkspaceStore(eng.workspace_path).load('probe:'+case);draft=final['task_state']['draft']
        actual={frozenset(row['member_ids']) for row in draft.get('integrate_groups',[])}
        if case=='real_five_member_partition':
            expected={frozenset(['incoming.concept.09d209b4c717de41ef03','input::input::n108']),
                      frozenset(['incoming.concept.5a98722664e893184416','input::input::n190','input::input::n40'])}
            assert actual==expected, {'expected_groups':[sorted(g) for g in expected],'actual_groups':[sorted(g) for g in actual]}
        else:
            assert not actual and set(draft['keep_separate_ids'])==members
        result.update(status='passed',independent_real_assessment=True,draft=draft,review=final['task_state']['review'],
            accepted_tasks=len(final['history']),checkpoint_stage=final['task_state']['stage'])
        # Verify exact node provenance unions for every subgroup, independently
        # of the model's judgment. Relationship mapping/provenance is tested too.
        after={n.id:n for n in eng.current.graph.nodes};mapping={k:k for k in members}
        for row in draft.get('integrate_groups',[]):
            for k in row['member_ids']:mapping[k]=row['owner_id']
            expected_sources={s for k in row['member_ids'] for s in nodes[k].sources}
            assert expected_sources<=set(after[row['owner_id']].sources)
        oldedges={edge.id:edge for edge in base.graph.edges if edge.from_node_id in members or edge.to_node_id in members}
        actualedges={edge.id:edge for edge in eng.current.graph.edges}
        dispositions=[]
        for ident,edge in oldedges.items():
            current=actualedges.get(ident)
            if current is not None:
                assert current.from_node_id==mapping.get(edge.from_node_id,edge.from_node_id)
                assert current.to_node_id==mapping.get(edge.to_node_id,edge.to_node_id)
                assert set(edge.sources)<=set(current.sources)
            else:
                successor=[e for e in actualedges.values() if e.edge_type==edge.edge_type
                    and e.from_node_id==mapping.get(edge.from_node_id,edge.from_node_id)
                    and e.to_node_id==mapping.get(edge.to_node_id,edge.to_node_id)
                    and set(edge.sources)<=set(e.sources)]
                assert successor,'Lost edge without provenance successor: '+ident
                current=successor[0]
            dispositions.append({'before':edge.model_dump(mode='json'),'after':current.model_dump(mode='json')})
        write(folder/'relation_dispositions.json',dispositions)
        write(folder/'local_diff.json',{'before_nodes':[nodes[k].model_dump(mode='json') for k in sorted(members)],
            'after_nodes':[after[k].model_dump(mode='json') for k in sorted(set(mapping.values()))],
            'member_mapping':mapping,'relations_before':len(oldedges),'relations_after_distinct':len({r['after']['id'] for r in dispositions}),
            'source_union_checks_passed':True})
    except Exception as error:
        result.update(status='failed',errors=transport_error_details(error,[k.api_key.get_secret_value() for k in keys]))
    result.update(seconds=round(time.monotonic()-start,2),model_calls=agent.call_budget['used'],anonymous_key_slots=call_slots,
                  source_checkpoint_unchanged=source_hash==sha256(CHECKPOINT.read_bytes()).hexdigest())
    write(folder/'after.json',eng.current.model_dump(mode='json'));write(folder/'result.json',result)
    print(json.dumps({k:result.get(k) for k in ('case','status','seconds','model_calls','source_checkpoint_unchanged')}),flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--case',choices=['real_five_member_partition','metric_singletons'],required=True);p.add_argument('--slot',type=int,required=True);a=p.parse_args()
    run(a.root.resolve(),a.case,a.slot)
