import json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,'scripts')
from reconciliation_probe_fixtures import write
from cortex.schema import GraphState,GraphSpace,load_root_schema
from graph_merge_archive import migrate_relation_notes
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.reconciliation.relation_merger import relation_groups,consolidate_relations,policy_fingerprint
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.capabilities.graph_merge.models import LocalTask
from cortex.capabilities.graph_merge.reconciliation.context import relation_context
from cortex.capabilities.graph_merge.reconciliation.permissions import ReconciliationRuntime
from cortex.capabilities.sql_training.graph_merge_evidence import MappingEvidenceRepository,SqlTrainingGraphMergeEvidenceResolver
from cortex.capabilities.graph_merge.reconciliation.policy import reconciliation_system
root=Path('output/single_graph_merge_fix_20261121');schema=load_root_schema()
for db in ('thrombosis_prediction','toxicology'):
 folder=root/db;folder.mkdir(exist_ok=True)
 p=root/'snapshots'/db/'workspace.sqlite'
 with sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) as c:journal=json.loads(c.execute('select payload from merge_workspaces order by revision desc limit 1').fetchone()[0])
 state,notes=migrate_relation_notes(journal['current'])
 from cortex.schema import validate_graph_state
 validate_graph_state(state,schema)
 write(folder/'before.json',state.model_dump(mode='json'));write(folder/'migration.json',notes)
 rows=json.loads((Path('output/bird_merge_first10_20261121')/db/'source_evidence.json').read_text())
 sources=tuple(dict.fromkeys([s for n in state.graph.nodes for s in (*n.sources,*n.common_sources,*(s for a in n.alternatives for s in a.sources))]+[s for e in state.graph.edges for s in e.sources]))
 evidence=SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(rows)).resolve(sources)
 context=IntegrationContext(state,GraphState(graph=GraphSpace()),schema,evidence=evidence)
 # Old business descriptions are preserved candidates, explicitly rebound to
 # unchanged relation groups. No completed model result is regenerated.
 old=journal['relation_merger_checkpoint'];groups=relation_groups(state)
 assert old['integrity_hash']==canonical_hash({k:v for k,v in old.items() if k!='integrity_hash'})
 assert old['source_hash']==canonical_hash(journal['current'])
 assert canonical_hash(groups)==old['groups_hash']
 cp={'version':1,'source_hash':canonical_hash(state),'schema_hash':canonical_hash(schema),
     'evidence_hash':canonical_hash(sorted(({'id':e.id,'source':e.source.model_dump(mode='json'),'content':e.content} for e in evidence),key=lambda r:(r['id'],canonical_hash(r)))),
     'scope_hash':canonical_hash(None),'policy':policy_fingerprint(),'groups_hash':canonical_hash(groups),
     'stage':'collecting','results':old['results'],'batches':{},'edge_aliases':{}}
 cp['integrity_hash']=canonical_hash(cp)
 class NoCalls:
  def merge_relations(self,*args):raise AssertionError('Completed descriptions must not be replayed')
 merged=consolidate_relations(NoCalls(),context,state,checkpoint=cp,save=lambda cp:write(folder/'relation_checkpoint.json',cp))
 old_nodes={n['id']:n for n in journal['current']['graph']['nodes']}
 for n in merged.state.graph.nodes:
  assert set(canonical_hash(s) for s in old_nodes[n.id]['sources'])==set(canonical_hash(s.model_dump(mode='json')) for s in n.sources)
 old_edges={e.id:e for e in state.graph.edges};new_edges={e.id:e for e in merged.state.graph.edges}
 for group in groups:
  survivor=new_edges[merged.edge_aliases[group['member_edge_ids'][0]]]
  assert set(survivor.sources)=={source for ident in group['member_edge_ids'] for source in old_edges[ident].sources}
 write(folder/'after.json',merged.state.model_dump(mode='json'))
 write(folder/'evidence.json',[e.model_dump(mode='json') for e in evidence])
 # Rebuild the same broad task for a fair input-size comparison. It no longer
 # expands physical tables or reads any original case graph.
 row=journal['open_tasks'][0];targets=set(row['targets']);targets={merged.edge_aliases.get(i,i) for i in targets}
 check={'id':'verify-real-merged-relations','kind':'node_relation_followup','mode':'required',
        'node_ids':sorted({x['node_id'] for x in notes}),
        'required_outcome':'Check the current consolidated relations and owning node definitions. Preserve supported roles, limits, matching methods and evidence; valid content may remain unchanged.'}
 task=LocalTask('single-graph-preservation:'+db,'reconcile',targets,checks=[check])
 ctx=IntegrationContext(merged.state,GraphState(graph=GraphSpace()),schema,evidence=evidence)
 ctx.task_before=ctx.candidate=merged.state;ctx.active_task=task
 payload=relation_context(ctx,merged.state,task)
 rt=ReconciliationRuntime(ctx,payload,'reconcile');rt.system=reconciliation_system('writer',[])
 from cortex.capabilities.graph_merge.llm.budget import check_request
 from cortex.capabilities.graph_merge.reconciliation.contract import TOOLS
 from langchain_core.tools import StructuredTool
 tools=list(rt.available_read_tools(rt.build_read_tools()).values())
 tools.append(StructuredTool.from_function(lambda **kw:kw,name='edit_draft',description='Edit the current local draft.',args_schema=TOOLS['edit_draft']))
 size=check_request(rt.messages(),tools,210000)
 write(folder/'reconstructed_messages.json',[m.model_dump(mode='json') for m in rt.messages()])
 report={'database':db,'saved_node_tasks_preserved':len(journal['history']),'migrated_groups':len(notes),
    'relation_results_reused':len(old['results']),'relation_model_calls':0,'node_sources_unchanged':True,'edge_source_unions_exact':True,'old_checkpoint_integrity_verified':True,'schema_valid':True,
    'after_counts':{'nodes':len(merged.state.graph.nodes),'edges':len(merged.state.graph.edges)},
    'replayed_broad_scope_characters':size,'context_nodes':len(rt.working['before']['nodes']),
    'context_edges':len(rt.working['before']['edges']), 'context_columns':sum(n['node_type']=='column' for n in rt.working['before']['nodes'].values()),
    'under_unchanged_limit':size<=210000,'no_historical_graph_reads':not hasattr(rt,'source_snapshot')}
 write(folder/'offline_result.json',report);print(json.dumps(report),flush=True)
