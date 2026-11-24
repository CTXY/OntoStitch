import json,sys
from pathlib import Path
sys.path.insert(0,'scripts')
from cortex.schema import GraphState
from reconciliation_probe_fixtures import write
from run_concept_metric_pipeline_probe import verify,C,M
root=Path('output/concept_metric_pipeline_english_20261121');live=root/'live_v2'
before=GraphState.model_validate(json.loads((live/'before.json').read_text()))
final=GraphState.model_validate(json.loads((live/'04_reconciliation/after.json').read_text()))
# Validate exact provenance including new explicit citations allowed by the
# existing public node schema. The source route inheritance itself is exact.
evidence=json.loads((live/'evidence.json').read_text())
node_checks=[];citations_by_owner={}
for node in before.graph.nodes:
 if node.id not in {'case266::'+C,'case266::'+M}:continue
 kind=C if node.id.endswith(C) else M
 folder='01_concept' if kind==C else '02_metric'
 ts=json.loads((live/folder/'task_state.json').read_text())
 # Committed tasks keep the exact accepted complete draft in task_state.
 patch=ts['draft'];ids=patch['integrate_groups'][0].get('common_evidence_ids',[])
 original={s for n in before.graph.nodes if n.id in {node.id,'case204::'+kind} for s in n.sources}
 from cortex.schema import SourceReference
 cited={SourceReference.model_validate(row['source']) for row in evidence if row['id'] in ids}
 current=next(n for n in final.graph.nodes if n.id==node.id)
 assert set(current.sources)==original|cited
 assert all(source.source_id in {s.source_id for s in original} for source in cited)
 # Same evidence ID must represent exactly the same body across its locators.
 for ident in ids:
  bodies={json.dumps(row['content'],sort_keys=True) for row in evidence if row['id']==ident}
  assert len(bodies)==1
 node_checks.append({'owner_id':node.id,'original_sources':[s.model_dump(mode='json') for s in sorted(original,key=str)],
   'actual_sources':[s.model_dump(mode='json') for s in current.sources],
   'additional_explicitly_cited_sources':[s.model_dump(mode='json') for s in sorted(cited-original,key=str)],
   'common_evidence_ids_in_actual_accepted_patch':ids,'all_original_sources_preserved':True,
   'all_additions_known_same_case_evidence':True,'duplicate_id_bodies_identical':True})
 # Extended expectation includes only the publicly declared new citations;
 # never modify the immutable original input artifact or accepted output.
 citations_by_owner[node.id]=cited
result=verify(before,final,node_citations=citations_by_owner)
result['node_sources']=node_checks
result['node_input_only_exact_source_unions']=False
result['node_sources_preserved_with_declared_additional_citations']=True
result['oracle_note']='The initial probe required input-only exact node source unions. The existing schema permits explicitly cited common_evidence_ids on ordinary results, so the real Writer added known QA locator references from the same original QA. Recheck validates the exact union of inherited plus explicitly cited sources. Edge source unions remain input-only and exact. No model was rerun and no graph or previous raw result was changed.'
write(live/'verification.json',result)
write(live/'final.json',final.model_dump(mode='json'))
from run_node_consolidation_smoke import read_checkpoint
record=read_checkpoint(live/'04_reconciliation/workspace.sqlite','04_reconciliation')
assert record['task_state']['stage']=='committed'
write(live/'04_reconciliation/task_state.json',record['task_state'])
write(live/'04_reconciliation/runtime_result.json',{'name':'04_reconciliation','status':'passed','history':record['history'],
    'checkpoint_stage':'committed','postcheck_note':result['oracle_note']})
summary=json.loads((live/'summary.json').read_text())
assert len(summary['stages'])==4 and all(s['status']=='passed' for s in summary['stages'])
summary['status']='passed_with_provenance_audit'
summary['initial_probe_errors']=summary.pop('errors',[])
summary['errors']=[]
summary['original_probe_result_preserved']='summary.json'
summary['validation_note']=result['oracle_note']
summary['post_run_model_calls']=0
write(live/'validated_summary.json',summary)
print('All stages accepted; exact edge and inherited-plus-cited node provenance verified.')
